"""Immutable approval and publication identity snapshots.

This module deliberately contains no provider or publishing integration.  It only
binds reviewed database identities and copies their publish-relevant values into
an audit snapshot while publishing remains disabled.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import (
    Board,
    ContentRevision,
    CreativeTemplate,
    DraftStatus,
    IntegrationAccount,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PublicationStatus,
)
from app.services.fingerprints import publication_identity_fingerprint


class PublicationIdentityError(ValueError):
    pass


def _creative_for_approval(db: Any, draft: PinDraft, revision: ContentRevision | None) -> PinCreative:
    if revision and revision.creative_id:
        creative = db.get(PinCreative, revision.creative_id)
    else:
        creative = db.scalar(
            select(PinCreative)
            .where(PinCreative.draft_id == draft.id)
            .order_by(PinCreative.created_at.asc(), PinCreative.id)
        )
    if not creative or creative.draft_id != draft.id:
        raise PublicationIdentityError("The selected version has no valid creative for this proposal.")
    if revision and revision.creative_id and revision.creative_id != creative.id:
        raise PublicationIdentityError("Revision and creative identities do not match.")
    return creative


def resolve_active_identity(
    db: Any,
    draft: PinDraft,
    reviewed_creative_id: str | None = None,
) -> tuple[ContentRevision | None, PinCreative, str]:
    from app.models.domain import ContentVersionSelection

    selection = db.scalar(
        select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft.id)
    )
    revision = None
    version_id = "original"
    if selection:
        revision = db.get(ContentRevision, selection.revision_id)
        if not revision or revision.draft_id != draft.id or revision.status != "REVIEW":
            raise PublicationIdentityError("The active revision is invalid for this proposal.")
        version_id = revision.id
    if not reviewed_creative_id:
        raise PublicationIdentityError("Approval requires the explicitly reviewed creative identity.")
    creative = db.get(PinCreative, reviewed_creative_id)
    if not creative or creative.draft_id != draft.id:
        raise PublicationIdentityError("Reviewed creative identity is invalid for this proposal.")
    if revision and revision.creative_id and revision.creative_id != creative.id:
        raise PublicationIdentityError("Revision and creative identities do not match.")
    return revision, creative, version_id


class PublicationIdentityService:
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    def create_snapshot(
        self,
        *,
        approval_id: str,
        board_id: str,
        integration_account_id: str | None = None,
        pinterest_connection_id: str | None = None,
        pinterest_board_record_id: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> PinPublication:
        """Create an immutable, non-publishing snapshot for an exact approval."""
        db = self.session_factory()
        try:
            approval = db.get(PinApproval, approval_id)
            if not approval or approval.decision != "APPROVED":
                raise PublicationIdentityError("An APPROVED decision is required.")
            draft = db.get(PinDraft, approval.draft_id)
            creative = db.get(PinCreative, approval.creative_id) if approval.creative_id else None
            revision = db.get(ContentRevision, approval.revision_id) if approval.revision_id else None
            if not draft or not creative or creative.draft_id != draft.id:
                raise PublicationIdentityError("Approval identities are incomplete or mismatched.")
            if approval.revision_id and (
                not revision
                or revision.draft_id != draft.id
                or (revision.creative_id and revision.creative_id != creative.id)
            ):
                raise PublicationIdentityError("Approval revision and creative do not match.")
            expected_version = revision.id if revision else "original"
            if approval.approved_version_id != expected_version:
                raise PublicationIdentityError("Approval version identity is inconsistent.")

            board = db.get(Board, board_id)
            pinterest_board = None
            connection = None
            if bool(pinterest_connection_id) != bool(pinterest_board_record_id):
                raise PublicationIdentityError("Pinterest connection and board identities are both required.")
            if pinterest_connection_id and pinterest_board_record_id:
                from app.models.domain import PinterestBoard, PinterestConnection
                connection = db.get(PinterestConnection, pinterest_connection_id) if pinterest_connection_id else None
                pinterest_board = db.get(PinterestBoard, pinterest_board_record_id) if pinterest_board_record_id else None
                if not connection or connection.status != "CONNECTED" or not pinterest_board or pinterest_board.connection_id != connection.id or not pinterest_board.is_active or not pinterest_board.is_eligible:
                    raise PublicationIdentityError("Pinterest destination is not eligible.")
                board = board  # legacy board may be absent for authoritative Pinterest destinations
            concept = db.get(PinConcept, draft.concept_id)
            template = db.get(CreativeTemplate, creative.template_id)
            if (not board and not pinterest_board) or not concept or (board and board.store_id != concept.store_id):
                raise PublicationIdentityError("Board does not belong to the proposal store.")
            account = None
            if integration_account_id:
                account = db.get(IntegrationAccount, integration_account_id)
                if not account or account.store_id != concept.store_id:
                    raise PublicationIdentityError("Integration account does not belong to the proposal store.")
            if not template or not creative.source_image_id:
                raise PublicationIdentityError("Creative provenance is incomplete.")

            text_hash = revision.text_fingerprint if revision else draft.text_fingerprint
            destination = revision.destination_url if revision else draft.destination_url
            utm_url = revision.utm_url if revision else draft.utm_url
            fingerprint = publication_identity_fingerprint(
                draft_id=draft.id,
                revision_id=revision.id if revision else None,
                creative_id=creative.id,
                source_image_id=creative.source_image_id,
                board_id=board.id if board else None,
                integration_account_id=account.id if account else None,
                destination_url=destination,
                utm_url=utm_url,
                pinterest_connection_id=connection.id if connection else None,
                pinterest_board_record_id=pinterest_board.id if pinterest_board else None,
                pinterest_board_id_snapshot=pinterest_board.external_board_id if pinterest_board else None,
            )
            if db.scalar(
                select(PinPublication).where(
                    PinPublication.publication_fingerprint == fingerprint
                )
            ):
                raise PublicationIdentityError("An identical publication snapshot already exists.")

            publication = PinPublication(
                draft_id=draft.id,
                revision_id=revision.id if revision else None,
                creative_id=creative.id,
                approval_id=approval.id,
                source_image_id=creative.source_image_id,
                template_id=template.id,
                template_key=template.key,
                template_version=template.version,
                text_fingerprint=text_hash,
                creative_fingerprint=creative.creative_fingerprint,
                board_id=board.id if board else None,
                pinterest_board_id=board.pinterest_board_id if board else pinterest_board.external_board_id,
                pinterest_connection_id=connection.id if connection else None,
                pinterest_board_record_id=pinterest_board.id if pinterest_board else None,
                pinterest_board_id_snapshot=pinterest_board.external_board_id if pinterest_board else board.pinterest_board_id,
                title_snapshot=revision.title if revision else draft.title,
                description_snapshot=revision.description if revision else draft.description,
                alt_text_snapshot=revision.alt_text if revision else draft.alt_text,
                media_url_snapshot=creative.rendered_url,
                integration_account_id=account.id if account else None,
                destination_url=destination,
                utm_url=utm_url,
                publication_fingerprint=fingerprint,
                status=PublicationStatus.SCHEDULED if scheduled_for else PublicationStatus.APPROVED,
                scheduled_for=scheduled_for,
            )
            db.add(publication)
            db.commit()
            db.refresh(publication)
            return publication
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
