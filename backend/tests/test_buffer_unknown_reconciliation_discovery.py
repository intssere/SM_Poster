import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.integrations.buffer.gateway import BufferPostResult, BufferPostSnapshot, BufferReadError
from app.models.domain import PinterestBoard, PinterestConnection, PublicationAttempt, PublicationStatus
from app.services.buffer_unknown_reconciliation_discovery import (
    BufferUnknownDiscoveryError,
    MAX_EXACT_READS,
    discover_buffer_unknown,
)
from app.services.publication_scheduler import request_fingerprint_for
from test_manual_publication_dispatch import _db, _ready_publication


SETTINGS = SimpleNamespace(buffer_organization_id="org", buffer_pinterest_channel_id="channel")
SECRET = "provider-secret-must-never-leak"


class Gateway:
    def __init__(self, rows=None, snapshots=None, *, fail_list=False, fail_post=False):
        self.rows = rows or {}
        self.snapshots = snapshots or {}
        self.fail_list = fail_list
        self.fail_post = fail_post
        self.calls = []

    async def recent_posts(self, organization_id, channel_id, *, first, status):
        self.calls.append(("list", organization_id, channel_id, first, status))
        if self.fail_list:
            raise BufferReadError(SECRET)
        return list(self.rows.get(status, []))

    async def post(self, post_id):
        self.calls.append(("post", post_id))
        if self.fail_post:
            raise BufferReadError(SECRET)
        return self.snapshots[post_id]


def _case(*, legacy=False):
    Session, engine = _db()
    db = Session()
    publication = _ready_publication(db, dispatch_provider="buffer" if legacy else "pinterest_direct")
    publication.status = PublicationStatus.PUBLISH_UNKNOWN
    now = datetime.now(timezone.utc)
    if not legacy:
        connection = db.get(PinterestConnection, publication.pinterest_connection_id)
        board = db.get(PinterestBoard, publication.pinterest_board_record_id)
        connection.boards_last_synced_at = now
        board.last_synced_at = now
    attempt = PublicationAttempt(
        publication_id=publication.id,
        attempt_number=1,
        status="UNKNOWN",
        dispatch_provider="buffer",
        request_fingerprint=request_fingerprint_for(publication),
        completed_at=now,
        error_code="PUBLISH_UNKNOWN",
        safe_response_metadata={"buffer_organization_id": "org", "buffer_channel_id": "channel"},
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return db, engine, publication, attempt


def _listed(operation_id, *, status="sending", when=None):
    when = when or datetime.now(timezone.utc)
    return BufferPostResult(
        buffer_post_id=operation_id,
        status=status,
        channel_id="channel",
        created_at=when.isoformat(),
        due_at=None,
        sent_at=None,
        external_link=None,
    )


def _snapshot(publication, operation_id, *, status="sending", mutate=None):
    values = {
        "buffer_post_id": operation_id,
        "status": status,
        "channel_id": "channel",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "due_at": None,
        "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None,
        "external_link": "https://www.pinterest.com/pin/123456789/" if status == "sent" else None,
        "channel_service": "pinterest",
        "text": publication.description_snapshot,
        "pinterest_board_service_id": publication.pinterest_board_id_snapshot,
        "pinterest_title": publication.title_snapshot,
        "pinterest_url": publication.utm_url,
        "image_url": publication.media_url_snapshot,
        "image_alt_text": publication.alt_text_snapshot,
    }
    if mutate:
        mutate(values)
    return BufferPostSnapshot(**values)


def _run(db, publication, gateway):
    return asyncio.run(discover_buffer_unknown(
        db,
        publication.id,
        settings=SETTINGS,
        gateway=gateway,
    ))


def _assert_unchanged(db, publication, attempt):
    db.refresh(publication)
    db.refresh(attempt)
    assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
    assert publication.pinterest_pin_id is None
    assert attempt.status == "UNKNOWN"
    assert attempt.provider_operation_id is None
    assert attempt.provider_pin_id is None


def test_zero_match_preserves_unknown_and_never_authorizes_retry():
    db, engine, publication, attempt = _case()
    try:
        gateway = Gateway()
        result = _run(db, publication, gateway)
        assert result["classification"] == "ZERO_MATCH"
        assert result["match_count"] == 0
        assert result["absence_proves_failure"] is False
        assert result["retry_permitted"] is False
        assert result["state_mutated"] is False
        assert len([call for call in gateway.calls if call[0] == "list"]) == 6
        assert not [call for call in gateway.calls if call[0] == "post"]
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_unique_exact_match_returns_sanitized_evidence_without_mutation():
    db, engine, publication, attempt = _case()
    try:
        gateway = Gateway(
            rows={"sent": [_listed("operation-1", status="sent")]},
            snapshots={"operation-1": _snapshot(publication, "operation-1", status="sent")},
        )
        result = _run(db, publication, gateway)
        assert result["classification"] == "UNIQUE_MATCH"
        assert result["match_count"] == 1
        assert result["matches"] == [{
            "provider_operation_id": "operation-1",
            "provider_operation_status": "sent",
            "provider_pin_id": "123456789",
            "provider_pin_verified": True,
        }]
        encoded = json.dumps(result)
        assert publication.title_snapshot not in encoded
        assert publication.description_snapshot not in encoded
        assert publication.utm_url not in encoded
        assert publication.media_url_snapshot not in encoded
        assert result["retry_permitted"] is False
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_multiple_exact_matches_remain_ambiguous_and_do_not_mutate():
    db, engine, publication, attempt = _case()
    try:
        rows = [_listed("operation-1"), _listed("operation-2")]
        gateway = Gateway(
            rows={"sending": rows},
            snapshots={
                "operation-1": _snapshot(publication, "operation-1"),
                "operation-2": _snapshot(publication, "operation-2"),
            },
        )
        result = _run(db, publication, gateway)
        assert result["classification"] == "MULTIPLE_MATCHES"
        assert result["match_count"] == 2
        assert {match["provider_operation_id"] for match in result["matches"]} == {"operation-1", "operation-2"}
        assert result["retry_permitted"] is False
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_snapshot_mismatch_is_not_promoted_to_match():
    db, engine, publication, attempt = _case()
    try:
        gateway = Gateway(
            rows={"sending": [_listed("operation-1")]},
            snapshots={"operation-1": _snapshot(
                publication,
                "operation-1",
                mutate=lambda values: values.__setitem__("pinterest_title", "different-title"),
            )},
        )
        result = _run(db, publication, gateway)
        assert result["classification"] == "ZERO_MATCH"
        assert result["exact_read_count"] == 1
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_out_of_window_rows_are_not_exact_read():
    db, engine, publication, attempt = _case()
    try:
        gateway = Gateway(rows={"sending": [_listed("old", when=datetime(2020, 1, 1, tzinfo=timezone.utc))]})
        result = _run(db, publication, gateway)
        assert result["classification"] == "ZERO_MATCH"
        assert result["listed_candidate_count"] == 0
        assert not [call for call in gateway.calls if call[0] == "post"]
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_candidate_cap_fails_closed_before_exact_reads():
    db, engine, publication, attempt = _case()
    try:
        rows = [_listed(f"op-{index}") for index in range(MAX_EXACT_READS + 1)]
        gateway = Gateway(rows={"sending": rows})
        with pytest.raises(BufferUnknownDiscoveryError, match="BUFFER_DISCOVERY_CANDIDATE_LIMIT_EXCEEDED"):
            _run(db, publication, gateway)
        assert not [call for call in gateway.calls if call[0] == "post"]
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


@pytest.mark.parametrize("phase", ["list", "post"])
def test_provider_read_failures_are_sanitized_and_preserve_unknown(phase):
    db, engine, publication, attempt = _case()
    try:
        gateway = Gateway(
            rows={"sending": [_listed("operation-1")]},
            snapshots={"operation-1": _snapshot(publication, "operation-1")},
            fail_list=phase == "list",
            fail_post=phase == "post",
        )
        with pytest.raises(BufferUnknownDiscoveryError) as error:
            _run(db, publication, gateway)
        assert str(error.value) == "BUFFER_DISCOVERY_READ_FAILED"
        assert SECRET not in str(error.value)
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_server_owned_routing_drift_fails_before_provider_reads():
    db, engine, publication, attempt = _case()
    try:
        board = db.get(PinterestBoard, publication.pinterest_board_record_id)
        board.external_board_id = "drifted-board"
        db.commit()
        gateway = Gateway()
        with pytest.raises(BufferUnknownDiscoveryError, match="BUFFER_DISCOVERY_ROUTING_MISMATCH"):
            _run(db, publication, gateway)
        assert gateway.calls == []
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_legacy_board_routing_remains_supported():
    db, engine, publication, attempt = _case(legacy=True)
    try:
        gateway = Gateway(
            rows={"sending": [_listed("legacy-operation")]},
            snapshots={"legacy-operation": _snapshot(publication, "legacy-operation")},
        )
        result = _run(db, publication, gateway)
        assert result["classification"] == "UNIQUE_MATCH"
        assert result["matches"][0]["provider_operation_id"] == "legacy-operation"
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_existing_operation_id_is_not_eligible_for_discovery():
    db, engine, publication, attempt = _case()
    try:
        attempt.provider_operation_id = "already-known"
        db.commit()
        gateway = Gateway()
        with pytest.raises(BufferUnknownDiscoveryError, match="BUFFER_DISCOVERY_NO_OPERATION_UNKNOWN_REQUIRED"):
            _run(db, publication, gateway)
        assert gateway.calls == []
    finally:
        db.close(); engine.dispose()


def test_attempt_provider_identity_drift_is_rejected_before_provider_reads():
    db, engine, publication, attempt = _case()
    try:
        attempt.safe_response_metadata = {"buffer_organization_id": "other-org", "buffer_channel_id": "channel"}
        db.commit()
        gateway = Gateway()
        with pytest.raises(BufferUnknownDiscoveryError, match="BUFFER_DISCOVERY_PROVIDER_IDENTITY_MISMATCH"):
            _run(db, publication, gateway)
        assert gateway.calls == []
        _assert_unchanged(db, publication, attempt)
    finally:
        db.close(); engine.dispose()


def test_discovery_route_requires_authenticated_admin(monkeypatch):
    from app.api.routes import buffer_reconciliation_discovery as route
    monkeypatch.setattr(route, "current_user", lambda request: None)
    with pytest.raises(HTTPException) as error:
        asyncio.run(route.buffer_unknown_discovery("publication", SimpleNamespace(), SimpleNamespace()))
    assert error.value.status_code == 401
    assert error.value.detail == "Authentication required"


def test_discovery_route_returns_only_service_result_when_authenticated(monkeypatch):
    from app.api.routes import buffer_reconciliation_discovery as route
    expected = {"classification": "ZERO_MATCH", "retry_permitted": False}
    called = []

    async def fake_discover(db, publication_id):
        called.append((db, publication_id))
        return expected

    db = SimpleNamespace()
    monkeypatch.setattr(route, "current_user", lambda request: "admin")
    monkeypatch.setattr(route, "discover_buffer_unknown", fake_discover)
    result = asyncio.run(route.buffer_unknown_discovery("publication", SimpleNamespace(), db))
    assert result == expected
    assert called == [(db, "publication")]
