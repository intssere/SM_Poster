"""DB-backed Buffer acceptance: every HTTP request terminates in MockTransport."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, func

from app.core.config import Settings
from app.integrations.buffer.gateway import BufferGateway, BufferReadError
from app.models.domain import PinPublication, PublicationAttempt, PublicationReconciliationEvent, PublicationStatus
from app.services.buffer_manual_publication_dispatch import dispatch_buffer
from app.services.buffer_publication_reconciliation import reconcile_buffer, BufferReconciliationError, pinterest_pin_id
from app.services.buffer_single_pin_pilot import validate_pilot
from app.services.manual_publication_dispatch import ManualDispatchError, atomic_authorized_claim
from app.services.publication_dispatch_authorization import create_authorization
from app.services.publication_scheduler import request_fingerprint_for, schedule
from app.services.publication_reconciliation import reconcile, ReconciliationError
from app.services.publication_duplicates import evaluate_publication_duplicates
from app.services.pinterest_publisher import PublicationReconciliationError
from test_manual_publication_dispatch import _db, _ready_publication

SECRET = "fake-buffer-phase2-secret-not-real"


@pytest.fixture
def case():
    Session, engine = _db()
    with Session() as db:
        p = _ready_publication(db, scopes=["user_accounts:read", "boards:read", "pins:read"])
        auth = create_authorization(db, p, actor="operator")
        settings = Settings(_env_file=None, DATABASE_URL="sqlite+pysqlite:///:memory:",
            buffer_api_key=SECRET, buffer_organization_id="org", buffer_pinterest_channel_id="channel",
            buffer_api_base="https://api.buffer.com", publishing_enabled=True, buffer_publishing_enabled=True,
            buffer_single_pin_pilot_enabled=True, buffer_single_pin_pilot_publication_id=p.id,
            buffer_single_pin_pilot_publication_fingerprint=p.publication_fingerprint,
            buffer_single_pin_pilot_request_fingerprint=request_fingerprint_for(p))
        c = SimpleNamespace(db=db, p=p, auth=auth, settings=settings, calls=[], status="sending", failure=None, read_failure=None,
                            change_post=None, on_channel=None)
        def handler(request):
            body = json.loads(request.content)
            query = body["query"]
            assert SECRET not in request.content.decode()
            assert str(request.url) == "https://api.buffer.com"
            assert request.headers["Authorization"] == "Bearer " + SECRET
            if "createPost" in query:
                c.calls.append("create")
                value = body["variables"]["input"]
                assert value["needsApproval"] is False
                assert value["mode"] == "shareNow" and value["schedulingType"] == "automatic"
                assert value["text"] == p.description_snapshot
                assert value["metadata"]["pinterest"] == {"boardServiceId": p.pinterest_board_id_snapshot, "title": p.title_snapshot, "url": p.utm_url}
                assert value["assets"] == [{"image": {"url": p.media_url_snapshot, "metadata": {"altText": p.alt_text_snapshot}}}]
                if c.failure == "rejected":
                    return httpx.Response(200, json={"data": {"createPost": {"__typename": "MutationError", "errorMessage": SECRET}}})
                if c.failure == "timeout":
                    raise httpx.ReadTimeout(SECRET)
                if c.failure == "transport":
                    raise httpx.ConnectError(SECRET)
                if c.failure == "500":
                    return httpx.Response(500, text=SECRET)
                if c.failure == "malformed":
                    return httpx.Response(200, json={"data": {"createPost": {"__typename": "PostActionSuccess", "post": {"id": SECRET}}}})
                return httpx.Response(200, json={"data": {"createPost": {"__typename": "PostActionSuccess", "post": post(c)}}})
            if "ExactPost" in query:
                c.calls.append("post")
                assert body["variables"] == {"input": {"id": "operation-1"}}
                assert "operation-1" not in query
                if c.read_failure == "timeout":
                    raise httpx.ReadTimeout(SECRET)
                if c.read_failure == "500":
                    return httpx.Response(500, text=SECRET)
                if c.read_failure == "malformed":
                    return httpx.Response(200, json={"data": {"post": []}})
                value = post(c)
                if c.change_post:
                    c.change_post(value)
                return httpx.Response(200, json={"data": {"post": value}})
            if "organizations" in query:
                c.calls.append("organizations")
                return httpx.Response(200, json={"data": {"account": {"organizations": [{"id": "org", "name": "Organization"}]}}})
            assert "channels" in query
            c.calls.append("channels")
            if c.on_channel:
                c.on_channel()
            return httpx.Response(200, json={"data": {"channels": [{"id": "channel", "name": "Pinterest", "service": "pinterest",
                "isDisconnected": False, "isLocked": False, "metadata": {"boards": [{"serviceId": p.pinterest_board_id_snapshot, "name": "Fragrance"}]}}]}})
        c.handler = handler
        yield c
    engine.dispose()


def post(c):
    return {"id": "operation-1", "status": c.status, "channelId": "channel", "channelService": "pinterest",
        "createdAt": "2026-09-06T00:00:00Z", "dueAt": None, "sentAt": "2026-09-06T00:01:00Z" if c.status == "sent" else None,
        "externalLink": "https://www.pinterest.com/pin/123456789/" if c.status == "sent" else None,
        "text": c.p.description_snapshot,
        "metadata": {"board": {"serviceId": c.p.pinterest_board_id_snapshot}, "title": c.p.title_snapshot, "url": c.p.utm_url},
        "assets": [{"__typename": "ImageAsset", "source": c.p.media_url_snapshot, "image": {"altText": c.p.alt_text_snapshot}}],
        "raw_body": SECRET}


def run(c, reconciliation=False):
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(c.handler)) as client:
            gateway = BufferGateway(c.settings, client=client)
            if reconciliation:
                return await reconcile_buffer(c.db, c.p.id, actor="operator", settings=c.settings, gateway=gateway)
            return await dispatch_buffer(c.db, c.p, settings=c.settings, gateway=gateway)
    return asyncio.run(invoke())


def attempt(c):
    c.db.refresh(c.p)
    c.db.refresh(c.auth)
    a = c.db.scalar(select(PublicationAttempt).where(PublicationAttempt.publication_id == c.p.id))
    if a:
        c.db.refresh(a)
        assert SECRET not in json.dumps(a.safe_response_metadata)
    assert not c.p.provider_response
    return a


@pytest.mark.parametrize("field,value", [
    ("publishing_enabled", False), ("buffer_publishing_enabled", False), ("buffer_single_pin_pilot_enabled", False),
    ("buffer_single_pin_pilot_publication_id", "wrong"), ("buffer_single_pin_pilot_publication_fingerprint", "wrong"),
    ("buffer_single_pin_pilot_request_fingerprint", "wrong"), ("buffer_api_key", None),
    ("buffer_organization_id", None), ("buffer_pinterest_channel_id", None),
])
def test_preclaim_configuration_and_pilot_leave_authorization_active(case, field, value):
    setattr(case.settings, field, value)
    with pytest.raises(ManualDispatchError):
        run(case)
    assert attempt(case) is None
    assert case.auth.status == "ACTIVE" and case.p.status == PublicationStatus.SCHEDULED
    assert case.calls == []


@pytest.mark.parametrize("invalid", ["missing", "expired", "revoked", "snapshot", "not-due", "duplicate", "prior-attempt"])
def test_authorization_and_prior_attempt_fail_closed(case, invalid):
    c = case
    if invalid == "missing": c.db.delete(c.auth)
    if invalid == "expired": c.auth.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    if invalid == "revoked": c.auth.status = "REVOKED"
    if invalid == "snapshot": c.p.title_snapshot = "Changed title"
    if invalid == "not-due": c.p.scheduled_for = datetime.now(timezone.utc) + timedelta(days=1)
    if invalid == "duplicate":
        other = PinPublication(id="other", draft_id=c.p.draft_id, creative_id=c.p.creative_id,
                               publication_fingerprint="q" * 64, status=PublicationStatus.PUBLISHED,
                               pinterest_board_id_snapshot=c.p.pinterest_board_id_snapshot, utm_url=c.p.utm_url,
                               creative_fingerprint=c.p.creative_fingerprint, text_fingerprint=c.p.text_fingerprint)
        c.db.add(other)
    if invalid == "prior-attempt": c.db.add(PublicationAttempt(publication_id=c.p.id, attempt_number=1, status="FAILED"))
    c.db.commit()
    with pytest.raises(ManualDispatchError): run(c)
    assert c.calls == []
    assert c.p.status == PublicationStatus.SCHEDULED
    assert c.db.scalar(select(func.count()).select_from(PublicationAttempt)) == (1 if invalid == "prior-attempt" else 0)


@pytest.mark.parametrize("status", ["scheduled", "sending", "draft", "needs_approval", "error", "sent"])
def test_create_outcome_persists_distinct_operation_and_pin_identities(case, status, caplog):
    c = case
    c.status = status
    run(c)
    a = attempt(c)
    assert c.auth.status == "CONSUMED"
    assert a.dispatch_provider == "buffer" and a.provider_operation_id == "operation-1"
    assert a.provider_operation_status == status
    assert a.provider_submitted_at and a.provider_last_observed_at
    assert c.calls.count("create") == 1
    if status == "sent":
        assert c.p.status == PublicationStatus.PUBLISHED and a.status == "SUCCEEDED"
        assert a.provider_pin_id == c.p.pinterest_pin_id == "123456789"
        assert c.calls.count("post") == 1
    elif status == "error":
        assert c.p.status == PublicationStatus.PUBLISH_FAILED and a.status == "FAILED"
        assert a.error_code == "BUFFER_PROVIDER_FAILED"
    else:
        assert c.p.status == PublicationStatus.PUBLISH_UNKNOWN and a.status == "UNKNOWN"
        assert a.error_code == "BUFFER_FINAL_OUTCOME_PENDING"
    if status != "sent": assert a.provider_pin_id is None and c.p.pinterest_pin_id is None
    with pytest.raises(ManualDispatchError): run(c)
    assert c.calls.count("create") == 1
    assert SECRET not in caplog.text


@pytest.mark.parametrize("failure", ["rejected", "timeout", "transport", "500", "malformed"])
def test_mutation_failures_bounded_no_second_create(case, failure):
    case.failure = failure
    run(case)
    a = attempt(case)
    assert a.provider_operation_id is None and a.provider_pin_id is None
    assert a.completed_at is not None
    assert a.status == ("FAILED" if failure == "rejected" else "UNKNOWN")
    assert a.error_code == ("PROVIDER_REJECTED" if failure == "rejected" else "PUBLISH_UNKNOWN")
    assert case.calls.count("create") == 1


@pytest.mark.parametrize("mutation", ["pilot", "approval", "destination"])
def test_postclaim_revalidation_blocks_mutation(case, mutation):
    def change():
        if case.calls.count("channels") == 2:
            if mutation == "pilot": case.settings.buffer_single_pin_pilot_enabled = False
            elif mutation == "approval":
                from app.models.domain import PinApproval
                case.db.get(PinApproval, case.p.approval_id).decision = "REJECTED"
                case.db.commit()
            else: raise BufferReadError(SECRET)
    case.on_channel = change
    with pytest.raises(ManualDispatchError, match="BUFFER_POSTCLAIM_VALIDATION_FAILED"): run(case)
    a = attempt(case)
    assert case.auth.status == "CONSUMED" and a.status == "FAILED"
    assert case.p.status == PublicationStatus.PUBLISH_FAILED
    assert case.calls.count("create") == 0


@pytest.mark.parametrize("recovery_fails", [False, True])
def test_known_operation_survives_commit_failure_or_typed_error(case, monkeypatch, recovery_fails):
    real = case.db.commit
    calls = []
    def commit():
        calls.append(1)
        if len(calls) == 2 or recovery_fails and len(calls) == 3: raise RuntimeError(SECRET)
        return real()
    monkeypatch.setattr(case.db, "commit", commit)
    if recovery_fails:
        with pytest.raises(PublicationReconciliationError, match="BUFFER_RECONCILIATION_PERSISTENCE_FAILED") as error: run(case)
        assert SECRET not in str(error.value)
    else:
        run(case)
        a = attempt(case)
        assert a.provider_operation_id == "operation-1"
        assert case.p.status == PublicationStatus.PUBLISH_UNKNOWN
        assert a.error_code == "BUFFER_STATE_PERSISTENCE_UNKNOWN"
    assert case.calls.count("create") == 1


@pytest.mark.parametrize("status", ["scheduled", "sending", "draft", "needs_approval", "sent", "error"])
def test_explicit_reconciliation_exact_post_and_atomic_audit(case, status):
    run(case)
    case.calls.clear()
    case.status = status
    run(case, True)
    a = attempt(case)
    assert case.calls == ["post"]
    events = case.db.scalars(select(PublicationReconciliationEvent)).all()
    if status in {"sent", "error"}:
        assert len(events) == 1
        e = events[0]
        assert e.provider == "buffer" and e.provider_operation_id == "operation-1"
        assert e.provider_operation_status == status
        assert e.previous_status == "PUBLISH_UNKNOWN" and e.new_status == case.p.status.value
        assert e.action == ("PROVIDER_PIN_CONFIRMED" if status == "sent" else "PROVIDER_FAILURE_CONFIRMED")
        assert a.status == ("SUCCEEDED" if status == "sent" else "FAILED")
        assert a.completed_at
    else:
        assert events == [] and a.status == "UNKNOWN" and case.p.status == PublicationStatus.PUBLISH_UNKNOWN


@pytest.mark.parametrize("field", ["channelId", "text", "board", "title", "url", "media", "alt", "id"])
def test_reconciliation_snapshot_mismatch_never_changes_outcome(case, field):
    run(case)
    case.calls.clear()
    case.status = "sent"
    def change(p):
        if field in {"channelId", "text", "id"}: p[field] = "wrong"
        elif field == "board": p["metadata"]["board"]["serviceId"] = "wrong"
        elif field in {"title", "url"}: p["metadata"][field] = "https://diamondshelf.us/wrong" if field == "url" else "wrong"
        elif field == "media": p["assets"][0]["source"] = "https://cdn.shopify.com/wrong.jpg"
        else: p["assets"][0]["image"]["altText"] = "wrong"
    case.change_post = change
    with pytest.raises(BufferReconciliationError): run(case, True)
    a = attempt(case)
    assert a.status == "UNKNOWN" and case.p.status == PublicationStatus.PUBLISH_UNKNOWN
    assert a.provider_pin_id is None and case.p.pinterest_pin_id is None
    assert case.calls == ["post"]
    assert case.db.scalar(select(func.count()).select_from(PublicationReconciliationEvent)) == 0


@pytest.mark.parametrize("failure", ["timeout", "500", "malformed"])
def test_reconciliation_read_failure_preserves_metadata(case, failure):
    run(case)
    a = attempt(case)
    before = (a.provider_operation_status, a.provider_last_observed_at, a.error_code, case.p.error_code)
    case.calls.clear()
    case.read_failure = failure
    with pytest.raises(BufferReconciliationError, match="BUFFER_RECONCILIATION_READ_FAILED"): run(case, True)
    a = attempt(case)
    assert before == (a.provider_operation_status, a.provider_last_observed_at, a.error_code, case.p.error_code)
    assert case.calls == ["post"]


def test_no_operation_never_scans_history(case):
    case.failure = "timeout"
    run(case)
    case.calls.clear()
    with pytest.raises(BufferReconciliationError, match="BUFFER_OPERATION_ID_REQUIRED"): run(case, True)
    assert case.calls == []


def test_reconciliation_commit_failure_rolls_back_attempt_publication_and_audit(case, monkeypatch):
    run(case)
    case.status = "sent"
    def fail(): raise RuntimeError(SECRET)
    monkeypatch.setattr(case.db, "commit", fail)
    with pytest.raises(PublicationReconciliationError): run(case, True)
    assert attempt(case).status == "UNKNOWN" and case.p.status == PublicationStatus.PUBLISH_UNKNOWN
    assert case.db.scalar(select(func.count()).select_from(PublicationReconciliationEvent)) == 0


def test_known_operation_blocks_cancel_reschedule_and_duplicate(case):
    run(case)
    with pytest.raises(ReconciliationError, match="KNOWN_PROVIDER_OPERATION_REQUIRES_RECONCILIATION"):
        reconcile(case.db, case.p.id, actor="operator", action="CANCELLED_UNKNOWN", confirmed=True, reason="Investigated")
    with pytest.raises(ValueError): schedule(case.db, case.p, datetime.now(timezone.utc))
    assert evaluate_publication_duplicates(case.db, case.p)["status"] == "UNKNOWN_OUTCOME_BLOCKS_RETRY"
    other = PinPublication(id="other", draft_id=case.p.draft_id, creative_id=case.p.creative_id,
                           publication_fingerprint="q" * 64, status=PublicationStatus.SCHEDULED)
    case.db.add(other)
    for name in ("pinterest_board_id_snapshot", "utm_url", "creative_fingerprint", "text_fingerprint"):
        setattr(other, name, getattr(case.p, name))
    case.db.commit()
    assert evaluate_publication_duplicates(case.db, other)["status"] == "UNKNOWN_OUTCOME_BLOCKS_RETRY"


@pytest.mark.parametrize("link", [None, "https://pinterest.com/board/123", "http://pinterest.com/pin/123/",
    "https://pinterest.com.evil.net/pin/123/", "https://user@pinterest.com/pin/123/", "https://pinterest.com/pin/123/#x",
    "https://pinterest.com/pin/123/?token=x", "https://pinterest.com:abc/pin/123/", "https://pinterest.com/pin/abc/"])
def test_strict_external_link_rejected_without_network(link):
    assert pinterest_pin_id(link) is None


def test_buffer_defaults_stay_off():
    for name in ("publishing_enabled", "buffer_publishing_enabled", "buffer_single_pin_pilot_enabled", "pinterest_write_scope_enabled", "pinterest_single_pin_pilot_enabled"):
        assert Settings.model_fields[name].default is False
    for name in ("buffer_single_pin_pilot_publication_id", "buffer_single_pin_pilot_publication_fingerprint", "buffer_single_pin_pilot_request_fingerprint"):
        assert Settings.model_fields[name].default == ""


@pytest.mark.parametrize("invalid", ["missing-assets", "multiple-assets", "not-image", "bad-metadata", "bad-service", "bad-time", "secret", "bad-link"])
def test_exact_read_rejects_malformed_shapes_and_secret(case, invalid, caplog):
    run(case)
    case.calls.clear()
    def change(p):
        if invalid == "missing-assets": p["assets"] = []
        if invalid == "multiple-assets": p["assets"] *= 2
        if invalid == "not-image": p["assets"][0]["__typename"] = "VideoAsset"
        if invalid == "bad-metadata": p["metadata"] = []
        if invalid == "bad-service": p["channelService"] = "facebook"
        if invalid == "bad-time": p["createdAt"] = "not-a-time"
        if invalid == "secret": p["text"] = SECRET
        if invalid == "bad-link": p["externalLink"] = "https://127.0.0.1/pin/123/"
    case.change_post = change
    with pytest.raises(BufferReconciliationError, match="BUFFER_RECONCILIATION_READ_FAILED") as error:
        run(case, True)
    assert SECRET not in str(error.value) + caplog.text
    assert attempt(case).status == "UNKNOWN" and case.calls == ["post"]


@pytest.mark.parametrize("invalid", ["link", "snapshot", "read"])
def test_create_sent_without_exact_evidence_stays_unknown(case, invalid):
    case.status = "sent"
    if invalid == "read": case.read_failure = "500"
    elif invalid == "link": case.change_post = lambda p: p.update(externalLink="https://www.pinterest.com/board/123/")
    else: case.change_post = lambda p: p.update(text="different")
    run(case)
    a = attempt(case)
    assert case.p.status == PublicationStatus.PUBLISH_UNKNOWN and a.status == "UNKNOWN"
    assert a.error_code == case.p.error_code == "BUFFER_SENT_LINK_UNVERIFIED"
    assert a.provider_operation_id == "operation-1" and a.provider_pin_id is None and case.p.pinterest_pin_id is None
    assert case.calls.count("create") == case.calls.count("post") == 1


@pytest.mark.parametrize("conflict", ["operations", "known-pins", "assigned-pin", "changed-config", "changed-fingerprint"])
def test_reconciliation_conflicts_fail_closed(case, conflict):
    run(case)
    a = attempt(case)
    if conflict == "operations":
        case.db.add(PublicationAttempt(publication_id=case.p.id, attempt_number=2, status="UNKNOWN", dispatch_provider="buffer", provider_operation_id="operation-2"))
    if conflict == "known-pins":
        a.provider_pin_id = "111"
        case.p.pinterest_pin_id = "222"
    if conflict == "assigned-pin":
        case.db.add(PinPublication(id="other", draft_id=case.p.draft_id, creative_id=case.p.creative_id,
                    publication_fingerprint="q" * 64, status=PublicationStatus.PUBLISHED, pinterest_pin_id="123456789"))
    if conflict == "changed-config": case.settings.buffer_pinterest_channel_id = "changed-channel"
    if conflict == "changed-fingerprint": a.request_fingerprint = "different"
    case.db.commit()
    case.calls.clear()
    case.status = "sent"
    with pytest.raises(BufferReconciliationError): run(case, True)
    assert case.p.status == PublicationStatus.PUBLISH_UNKNOWN
    assert case.calls == (["post"] if conflict == "assigned-pin" else [])


def test_exact_claim_provider_allowlist_and_second_claim(case):
    with pytest.raises(ManualDispatchError, match="INVALID_DISPATCH_PROVIDER"):
        atomic_authorized_claim(case.db, case.p, case.auth, dispatch_provider="browser-owned")
    assert attempt(case) is None
    a = atomic_authorized_claim(case.db, case.p, case.auth, dispatch_provider="buffer")
    assert a.dispatch_provider == "buffer"
    assert atomic_authorized_claim(case.db, case.p, case.auth, dispatch_provider="buffer") is None
    assert case.db.scalar(select(func.count()).select_from(PublicationAttempt)) == 1


def test_direct_claim_still_defaults_to_pinterest(case):
    assert atomic_authorized_claim(case.db, case.p, case.auth).dispatch_provider == "pinterest_direct"


def test_known_buffer_operation_requires_exact_reconciliation_not_manual_pin_override(case):
    run(case)
    with pytest.raises(ReconciliationError, match="KNOWN_PROVIDER_OPERATION_REQUIRES_RECONCILIATION"):
        reconcile(case.db, case.p.id, actor="operator", action="PROVIDER_PIN_CONFIRMED", confirmed=True, provider_pin_id="123456789")
    assert case.p.status == PublicationStatus.PUBLISH_UNKNOWN


def test_preclaim_destination_failure_does_not_consume(case):
    case.on_channel = lambda: (_ for _ in ()).throw(BufferReadError(SECRET))
    with pytest.raises(ManualDispatchError, match="BUFFER_PRECLAIM_VALIDATION_FAILED"): run(case)
    assert attempt(case) is None and case.auth.status == "ACTIVE" and case.p.status == PublicationStatus.SCHEDULED
    assert case.calls.count("create") == 0


def test_observer_cannot_finish_twice(case):
    run(case)
    case.status = "sent"
    run(case, True)
    case.calls.clear()
    with pytest.raises(BufferReconciliationError, match="RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN"): run(case, True)
    assert case.calls == []
    assert case.db.scalar(select(func.count()).select_from(PublicationReconciliationEvent)) == 1
