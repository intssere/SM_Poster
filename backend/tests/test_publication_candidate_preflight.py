"""Candidate checks are intentionally confined to test databases and fake media."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, func

from app.core.config import get_settings
from app.models.domain import (
    PinApproval, PinCreative, PinDraft, PinPublication, PinterestBoard, PinterestConnection,
    PublicationStatus,
)
from app.services.media_storage import StorageUnavailable
from app.services.publication_candidate_preflight import preflight_candidate
from app.services.publication_identity import PublicationIdentityService, build_publication_candidate, PublicationIdentityError
from app.services.publication_duplicates import evaluate_publication_duplicates
from test_publication_identity import _prepared, _revision, _activate


PNG = b"\x89PNG\r\n\x1a\nsynthetic test PNG"
DIGEST = hashlib.sha256(PNG).hexdigest()


class FakeMedia:
    def __init__(self, data=PNG, error=None):
        self.data = data
        self.error = error
        self.media = self
        self.calls = 0

    def read(self, object_id, digest):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data


@pytest.fixture
def candidate(monkeypatch):
    monkeypatch.setenv("PUBLIC_MEDIA_BASE_URL", "https://media.example.com")
    get_settings.cache_clear()
    db, proposals, draft, creative = _prepared("candidate-preflight")
    draft.title = "Fragrance gift pick"
    draft.description = "Explore this fragrance gift pick for a polished scent routine."
    draft.alt_text = "A verified product creative for a fragrance gift pick."
    draft.destination_url = "https://diamondshelf.us/products/fragrance-pick"
    draft.utm_url = "https://diamondshelf.us/products/fragrance-pick?utm_source=pinterest&utm_medium=social"
    creative.render_status = "RENDERED"
    creative.sha256 = DIGEST
    creative.size_bytes = len(PNG)
    creative.rendered_url = f"/api/pins/creatives/{creative.id}/image"
    connection = PinterestConnection(
        external_user_id="synthetic-user", access_token_ciphertext="synthetic",
        refresh_token_ciphertext="synthetic", granted_scopes=[],
        status="CONNECTED", boards_last_synced_at=datetime.now(timezone.utc),
    )
    db.add(connection)
    db.flush()
    board = PinterestBoard(
        connection_id=connection.id, external_board_id="synthetic-board",
        name="Synthetic board", is_active=True, is_eligible=True,
        routing_label="fragrance", last_synced_at=connection.boards_last_synced_at,
    )
    db.add(board)
    db.commit()
    proposals.decide(draft.id, "APPROVED", reviewed_creative_id=creative.id)
    db.expire_all()
    approval = db.scalar(select(PinApproval).where(PinApproval.draft_id == draft.id))
    try:
        yield db, proposals, approval, draft, creative, board, connection
    finally:
        db.close()
        get_settings.cache_clear()


def check(db, approval, board, *, storage=None):
    return preflight_candidate(
        db, approval_id=approval.id, pinterest_board_record_id=board.id,
        storage=storage or FakeMedia(),
    )


def test_exact_transient_candidate_parity_and_no_writes(candidate, monkeypatch):
    db, proposals, approval, draft, creative, board, connection = candidate
    before = db.scalar(select(func.count()).select_from(PinPublication))
    built = build_publication_candidate(
        db, approval_id=approval.id, board_id=None,
        pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
    )
    assert built.id is None
    assert built not in db
    assert not db.new
    assert db.scalar(select(func.count()).select_from(PinPublication)) == before
    storage = FakeMedia()
    result = check(db, approval, board, storage=storage)
    assert result == {
        "approval_id": approval.id, "board_record_id": board.id,
        "asset_integrity": "VERIFIED", "quality": "PASS",
        "duplicate": "SAFE_TO_CONTINUE", "routing": "CURRENT",
        "eligible": True, "status": "ELIGIBLE", "reason_codes": [],
    }
    assert storage.calls == 1
    assert not db.new
    assert db.scalar(select(func.count()).select_from(PinPublication)) == before

    # A previously returned preflight result is not authorization: creation
    # reconstructs and rechecks the candidate before its existing commit.
    monkeypatch.setattr(
        "app.services.publication_candidate_preflight.CreativeStorage",
        lambda: FakeMedia(),
    )
    persisted = PublicationIdentityService(proposals.session_factory).create_snapshot(
        approval_id=approval.id, board_id=None,
        pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
    )
    fields = (
        "draft_id", "revision_id", "creative_id", "approval_id", "source_image_id",
        "template_id", "template_key", "template_version", "text_fingerprint",
        "creative_fingerprint", "board_id", "pinterest_board_id",
        "pinterest_connection_id", "pinterest_board_record_id",
        "pinterest_board_id_snapshot", "title_snapshot", "description_snapshot",
        "alt_text_snapshot", "media_url_snapshot", "integration_account_id",
        "destination_url", "utm_url", "publication_fingerprint", "status", "scheduled_for",
    )
    assert {field: getattr(persisted, field) for field in fields} == {
        field: getattr(built, field) for field in fields
    }
    assert db.scalar(select(func.count()).select_from(PinPublication)) == before + 1
    assert check(db, approval, board)["duplicate"] == "DUPLICATE_PUBLICATION"


@pytest.mark.parametrize("data,error,expected", [
    (b"not a png", None, "FAILED"),
    (None, StorageUnavailable("test storage unavailable"), "UNAVAILABLE"),
    (None, FileNotFoundError("test missing"), "FAILED"),
])
def test_asset_errors_fail_closed_without_publications(candidate, data, error, expected):
    db, _, approval, _, _, board, _ = candidate
    before = db.scalar(select(func.count()).select_from(PinPublication))
    result = check(db, approval, board, storage=FakeMedia(data=data, error=error))
    assert result["asset_integrity"] == expected
    assert result["status"] == "BLOCKED"
    assert result["quality"] == "NOT_CHECKED"
    assert db.scalar(select(func.count()).select_from(PinPublication)) == before


def test_routing_and_quality_fail_closed_without_asset_read(candidate):
    db, _, approval, draft, _, board, connection = candidate
    storage = FakeMedia()
    connection.boards_last_synced_at = datetime.now(timezone.utc) - timedelta(hours=25)
    result = check(db, approval, board, storage=storage)
    assert result["routing"] == "STALE"
    assert result["status"] == "BLOCKED"
    assert storage.calls == 0
    board.last_synced_at = connection.boards_last_synced_at
    result = check(db, approval, board, storage=storage)
    assert result["routing"] == "STALE"
    assert "PERSISTED_PINTEREST_ROUTING_TOO_OLD" in result["reason_codes"]
    connection.boards_last_synced_at = board.last_synced_at = datetime.now(timezone.utc)
    draft.title = ""
    result = check(db, approval, board, storage=storage)
    assert result["quality"] == "FAIL"
    assert result["eligible"] is False
    board.is_eligible = False
    result = check(db, approval, board, storage=storage)
    assert result["routing"] == "STALE"
    assert result["status"] == "BLOCKED"


def test_version_mismatch_and_duplicate_blocker(candidate):
    db, _, approval, draft, creative, board, connection = candidate
    revision = _revision(db, draft, creative)
    _activate(db, draft, revision)
    assert check(db, approval, board)["reason_codes"] == ["IDENTITY_PROVENANCE_INVALID"]
    # A second candidate with a matching approval must still compare against
    # every existing row, including one whose ID is explicitly copied.
    existing = build_publication_candidate(
        db, approval_id=approval.id, board_id=None,
        pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
    )
    existing.id = "existing"
    db.add(existing)
    db.commit()
    transient = build_publication_candidate(
        db, approval_id=approval.id, board_id=None,
        pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
    )
    transient.id = "existing"
    assert evaluate_publication_duplicates(db, transient)["status"] == "DUPLICATE_PUBLICATION"
    assert db.scalar(select(func.count()).select_from(PinPublication)) == 1


def test_creation_rechecks_after_preflight(candidate, monkeypatch):
    db, proposals, approval, _, _, board, connection = candidate
    assert check(db, approval, board)["status"] == "ELIGIBLE"
    connection.boards_last_synced_at = datetime.now(timezone.utc) - timedelta(hours=25)
    with pytest.raises(PublicationIdentityError, match="CANDIDATE_PREFLIGHT_BLOCKED"):
        PublicationIdentityService(proposals.session_factory).create_snapshot(
            approval_id=approval.id, board_id=None,
            pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
        )
    assert db.scalar(select(func.count()).select_from(PinPublication)) == 0


def test_verified_bytes_cannot_authorize_unrelated_media_url(candidate):
    db, proposals, approval, _, creative, board, connection = candidate
    creative.rendered_url = "https://media.example.com/unrelated-image.png"
    db.commit()
    storage = FakeMedia()
    result = check(db, approval, board, storage=storage)
    assert result["reason_codes"] == ["MEDIA_IDENTITY_MISMATCH"]
    assert result["status"] == "BLOCKED"
    assert storage.calls == 0
    with pytest.raises(PublicationIdentityError, match="CANDIDATE_PREFLIGHT_BLOCKED"):
        PublicationIdentityService(proposals.session_factory).create_snapshot(
            approval_id=approval.id, board_id=None,
            pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
        )
    assert db.scalar(select(func.count()).select_from(PinPublication)) == 0


@pytest.mark.parametrize("status,text_changed,expected", [
    (PublicationStatus.PUBLISHED, False, "DUPLICATE_PUBLICATION"),
    (PublicationStatus.PUBLISHED, True, "POSSIBLE_DUPLICATE_PIN"),
    (PublicationStatus.PUBLISH_UNKNOWN, False, "UNKNOWN_OUTCOME_BLOCKS_RETRY"),
])
def test_existing_publication_blockers_are_not_certified(candidate, status, text_changed, expected):
    db, _, approval, _, _, board, connection = candidate
    transient = build_publication_candidate(
        db, approval_id=approval.id, board_id=None,
        pinterest_connection_id=connection.id, pinterest_board_record_id=board.id,
    )
    db.add(PinPublication(
        draft_id=transient.draft_id, creative_id=transient.creative_id,
        publication_fingerprint="f" * 64, status=status,
        pinterest_board_id_snapshot=transient.pinterest_board_id_snapshot,
        utm_url=transient.utm_url, creative_fingerprint=transient.creative_fingerprint,
        text_fingerprint="different" if text_changed else transient.text_fingerprint,
    ))
    db.commit()
    result = check(db, approval, board)
    assert result["quality"] == "PASS"
    assert result["duplicate"] == expected
    assert result["status"] == "BLOCKED"
    assert db.scalar(select(func.count()).select_from(PinPublication)) == 1