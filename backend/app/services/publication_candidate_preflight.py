"""Read-only, provider-free candidate checks before a Pinterest snapshot exists.

The production media backend may require an App Storage read. This service never
calls a publishing provider and never adds, flushes, or commits database rows.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.models.domain import (
    ContentRevision,
    ContentVersionSelection,
    DraftStatus,
    PinCreative,
    PinDraft,
    PinPublication,
    PinterestBoard,
    ProductImage,
    PinConcept,
)
from app.services.creative_rendering import CreativeStorage
from app.services.pinterest_publication_quality import validate_publication_quality
from app.services.public_creative_media import public_creative_url, verified_png
from app.services.publication_duplicates import SAFE_TO_CONTINUE, evaluate_publication_duplicates
from app.services.publication_identity import PublicationIdentityError, build_publication_candidate
from app.services.routine_offline_preflight import RoutineOfflinePreflightError, _persisted_route
from app.services.media_storage import StorageUnavailable


def _result(approval_id: str, board_record_id: str) -> dict:
    return {
        "approval_id": approval_id,
        "board_record_id": board_record_id,
        "asset_integrity": "NOT_CHECKED",
        "quality": "NOT_CHECKED",
        "duplicate": "NOT_CHECKED",
        "routing": "NOT_CHECKED",
        "eligible": False,
        "status": "BLOCKED",
        "reason_codes": [],
    }


def evaluate_candidate(db, candidate: PinPublication, *, storage=None, now: datetime | None = None) -> dict:
    """Check an exact transient candidate, including currently stored bytes."""
    result = _result(candidate.approval_id, candidate.pinterest_board_record_id)
    with db.no_autoflush:
        try:
            _persisted_route(db, candidate, now=now or datetime.now(timezone.utc))
        except RoutineOfflinePreflightError as exc:
            result["routing"] = "STALE"
            result["reason_codes"].append(str(exc))
            return result
        result["routing"] = "CURRENT"

        draft = db.get(PinDraft, candidate.draft_id)
        creative = db.get(PinCreative, candidate.creative_id)
        revision = db.get(ContentRevision, candidate.revision_id) if candidate.revision_id else None
        selection = db.scalar(
            select(ContentVersionSelection).where(ContentVersionSelection.draft_id == candidate.draft_id)
        )
        concept = db.get(PinConcept, draft.concept_id) if draft else None
        image = db.get(ProductImage, candidate.source_image_id) if candidate.source_image_id else None
        if (
            not draft or draft.status != DraftStatus.APPROVED
            or not creative or creative.draft_id != draft.id
            or not concept or not image or image.product_id != concept.product_id
            or creative.source_image_id != image.id
            or (revision is not None and revision.source_image_id != image.id)
            or (selection.revision_id if selection else None) != candidate.revision_id
            or (revision is not None and revision.status != "REVIEW")
        ):
            result["reason_codes"].append("IDENTITY_PROVENANCE_INVALID")
            return result

        expected_media = public_creative_url(creative)
        if not expected_media or candidate.media_url_snapshot != expected_media:
            result["reason_codes"].append("MEDIA_IDENTITY_MISMATCH")
            return result

        digest = creative.sha256
        if not isinstance(digest, str):
            result["asset_integrity"] = "FAILED"
            result["reason_codes"].append("ASSET_INTEGRITY_FAILED")
            return result
        try:
            contents = verified_png(creative, digest, storage=storage or CreativeStorage())
        except StorageUnavailable:
            result["asset_integrity"] = "UNAVAILABLE"
            result["reason_codes"].append("ASSET_STORAGE_UNAVAILABLE")
            return result
        except Exception:
            result["asset_integrity"] = "FAILED"
            result["reason_codes"].append("ASSET_INTEGRITY_FAILED")
            return result
        if contents is None or (creative.size_bytes is not None and len(contents) != creative.size_bytes):
            result["asset_integrity"] = "FAILED"
            result["reason_codes"].append("ASSET_INTEGRITY_FAILED")
            return result
        result["asset_integrity"] = "VERIFIED"

        try:
            quality = validate_publication_quality(db, candidate, dispatch_provider="buffer")
            result["quality"] = quality["status"]
        except Exception:
            result["quality"] = "FAIL"
        if result["quality"] != "PASS":
            result["reason_codes"].append("QUALITY_NOT_PASS")
            return result
        try:
            duplicate = evaluate_publication_duplicates(db, candidate)
            result["duplicate"] = duplicate["status"]
        except Exception:
            result["duplicate"] = "FAILED"
        if result["duplicate"] != SAFE_TO_CONTINUE:
            result["reason_codes"].append("DUPLICATE_NOT_SAFE")
            return result
        result["eligible"] = True
        result["status"] = "ELIGIBLE"
        return result


def preflight_candidate(
    db, *, approval_id: str, pinterest_board_record_id: str,
    storage=None, now: datetime | None = None,
) -> dict:
    """Build and inspect, without attaching or persisting the publication."""
    result = _result(approval_id, pinterest_board_record_id)
    with db.no_autoflush:
        board = db.get(PinterestBoard, pinterest_board_record_id)
        if not board:
            result["routing"] = "STALE"
            result["reason_codes"].append("PERSISTED_PINTEREST_ROUTING_STALE")
            return result
        route_candidate = PinPublication(
            pinterest_connection_id=board.connection_id,
            pinterest_board_record_id=board.id,
            pinterest_board_id_snapshot=board.external_board_id,
        )
        try:
            _persisted_route(db, route_candidate, now=now or datetime.now(timezone.utc))
        except RoutineOfflinePreflightError as exc:
            result["routing"] = "STALE"
            result["reason_codes"].append(str(exc))
            return result
        try:
            candidate = build_publication_candidate(
                db,
                approval_id=approval_id,
                board_id=None,
                pinterest_connection_id=board.connection_id,
                pinterest_board_record_id=board.id,
            )
        except PublicationIdentityError:
            result["reason_codes"].append("IDENTITY_INVALID")
            return result
        return evaluate_candidate(db, candidate, storage=storage, now=now)