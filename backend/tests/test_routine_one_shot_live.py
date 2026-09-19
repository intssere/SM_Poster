from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.models.domain import PinPublication, PublicationStatus
from app.services.routine_publishing_control import RoutineControlError


PUB_ID = "target-publication"
PERMIT_ID = "permit-1"
PUB_FP = "a" * 64
REQ_FP = "b" * 64


class FakeDB:
    def __init__(self, publication=None):
        self.publication = publication
        self.get_calls = []
        self.rollbacks = 0

    def get(self, model, ident):
        self.get_calls.append((model, ident))
        if model is PinPublication and self.publication and ident == self.publication.id:
            return self.publication
        return None

    def refresh(self, obj):
        return None

    def rollback(self):
        self.rollbacks += 1


def _persistent_settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "publishing_enabled": True,
        "buffer_publishing_enabled": True,
        "routine_pinterest_worker_enabled": False,
        "routine_buffer_dispatch_enabled": False,
        "routine_pinterest_dry_run": True,
        "routine_pinterest_batch_size": 1,
        "routine_pinterest_daily_write_limit": 1,
    }
    values.update(overrides)
    return Settings(**values)


def _effective_live_settings():
    return _persistent_settings().model_copy(update={
        "routine_pinterest_worker_enabled": True,
        "routine_buffer_dispatch_enabled": True,
        "routine_pinterest_dry_run": False,
    })


def _publication():
    return SimpleNamespace(
        id=PUB_ID,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=None,
        publication_fingerprint=PUB_FP,
    )


def _run():
    return SimpleNamespace(
        id="run-1",
        status="RUNNING",
        scanned=0,
        eligible=0,
        skipped=0,
        claimed=0,
        dispatched=0,
        published=0,
        failed=0,
        unknown=0,
        error_code=None,
    )


@pytest.mark.parametrize(
    ("overrides", "control_state", "expected"),
    [
        ({}, "LIVE", "ROUTINE_CONTROL_NOT_PAUSED"),
        ({"routine_pinterest_worker_enabled": True}, "PAUSED", "ROUTINE_WORKER_MUST_BE_DISABLED"),
        ({"routine_buffer_dispatch_enabled": True}, "PAUSED", "ROUTINE_BUFFER_DISPATCH_MUST_BE_DISABLED"),
        ({"routine_pinterest_dry_run": False}, "PAUSED", "ROUTINE_DRY_RUN_CONFIG_REQUIRED"),
        ({"routine_pinterest_batch_size": 2}, "PAUSED", "ROUTINE_LIVE_BATCH_SIZE_MUST_BE_ONE"),
        ({"routine_pinterest_daily_write_limit": 2}, "PAUSED", "ROUTINE_LIVE_DAILY_LIMIT_MUST_BE_ONE"),
        ({"publishing_enabled": False}, "PAUSED", "PUBLISHING_DISABLED"),
        ({"buffer_publishing_enabled": False}, "PAUSED", "BUFFER_PUBLISHING_DISABLED"),
    ],
)
def test_one_shot_live_settings_fail_closed(overrides, control_state, expected):
    from app.api.routes import routine_publishing as route

    with pytest.raises(route.RoutineControlError, match=f"^{expected}$"):
        route._one_shot_live_settings(
            _persistent_settings(**overrides),
            SimpleNamespace(state=control_state),
        )


def test_one_shot_live_settings_create_bounded_effective_copy():
    from app.api.routes import routine_publishing as route

    persistent = _persistent_settings()
    effective = route._one_shot_live_settings(persistent, SimpleNamespace(state="PAUSED"))

    assert persistent.routine_pinterest_worker_enabled is False
    assert persistent.routine_buffer_dispatch_enabled is False
    assert persistent.routine_pinterest_dry_run is True
    assert effective.routine_pinterest_worker_enabled is True
    assert effective.routine_buffer_dispatch_enabled is True
    assert effective.routine_pinterest_dry_run is False
    assert effective.routine_pinterest_batch_size == 1
    assert effective.routine_pinterest_daily_write_limit == 1


@pytest.mark.asyncio
async def test_live_route_requires_explicit_confirmation(monkeypatch):
    from app.api.routes import routine_publishing as route

    monkeypatch.setattr(route, "_actor", lambda request: "operator")
    payload = route.RunOnceLiveRequest(
        confirmed=False,
        confirmation_text_version="ROUTINE_LIVE_ONCE_V1",
        permit_id=PERMIT_ID,
        publication_fingerprint=PUB_FP,
        request_fingerprint=REQ_FP,
    )
    with pytest.raises(HTTPException) as raised:
        await route.run_once_live(PUB_ID, object(), payload, FakeDB())
    assert raised.value.status_code == 422
    assert raised.value.detail == "INVALID_ROUTINE_LIVE_CONFIRMATION"


@pytest.mark.asyncio
async def test_live_route_binds_exact_identity_and_always_pauses(monkeypatch):
    from app.api.routes import routine_publishing as route

    publication = _publication()
    db = FakeDB(publication)
    permit = SimpleNamespace(id=PERMIT_ID)
    states = []
    calls = []

    monkeypatch.setattr(route, "_actor", lambda request: "operator")
    monkeypatch.setattr(route, "get_settings", lambda: _persistent_settings())
    monkeypatch.setattr(route, "get_control", lambda db_, create=False: SimpleNamespace(state="PAUSED"))
    monkeypatch.setattr(route, "request_fingerprint_for", lambda publication_: REQ_FP)
    monkeypatch.setattr(route, "active_permit", lambda db_, publication_id: permit)
    monkeypatch.setattr(route, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    monkeypatch.setattr(route, "_active_running_run", lambda db_: None)
    monkeypatch.setattr(route, "daily_provider_write_count", lambda db_, day_start: 0)
    monkeypatch.setattr(
        route,
        "set_control",
        lambda db_, *, state, actor, reason=None: states.append((state, actor, reason)) or SimpleNamespace(state=state),
    )

    async def fake_run_once(db_, **kwargs):
        calls.append(kwargs)
        return {
            "status": "SUCCEEDED",
            "mode": "LIVE",
            "run_id": "run-1",
            "scanned": 1,
            "eligible": 1,
            "skipped": 0,
            "claimed": 1,
            "dispatched": 1,
            "published": 1,
            "failed": 0,
            "unknown": 0,
            "error_code": None,
        }

    monkeypatch.setattr(route, "run_routine_worker_once", fake_run_once)
    payload = route.RunOnceLiveRequest(
        confirmed=True,
        confirmation_text_version="ROUTINE_LIVE_ONCE_V1",
        permit_id=PERMIT_ID,
        publication_fingerprint=PUB_FP,
        request_fingerprint=REQ_FP,
    )

    result = await route.run_once_live(PUB_ID, object(), payload, db)

    assert result["published"] == 1
    assert [state for state, _, _ in states] == ["LIVE", "PAUSED"]
    assert len(calls) == 1
    assert calls[0]["target_publication_id"] == PUB_ID
    assert calls[0]["target_permit_id"] == PERMIT_ID
    assert calls[0]["allow_targeted_live"] is True
    assert calls[0]["settings"].routine_pinterest_batch_size == 1
    assert calls[0]["settings"].routine_pinterest_daily_write_limit == 1


@pytest.mark.asyncio
async def test_live_route_rejects_identity_drift_before_arming(monkeypatch):
    from app.api.routes import routine_publishing as route

    publication = _publication()
    db = FakeDB(publication)
    armed = []

    monkeypatch.setattr(route, "_actor", lambda request: "operator")
    monkeypatch.setattr(route, "get_settings", lambda: _persistent_settings())
    monkeypatch.setattr(route, "get_control", lambda db_, create=False: SimpleNamespace(state="PAUSED"))
    monkeypatch.setattr(route, "request_fingerprint_for", lambda publication_: REQ_FP)
    monkeypatch.setattr(route, "set_control", lambda *a, **k: armed.append(True))

    payload = route.RunOnceLiveRequest(
        confirmed=True,
        confirmation_text_version="ROUTINE_LIVE_ONCE_V1",
        permit_id=PERMIT_ID,
        publication_fingerprint="c" * 64,
        request_fingerprint=REQ_FP,
    )
    with pytest.raises(HTTPException) as raised:
        await route.run_once_live(PUB_ID, object(), payload, db)

    assert raised.value.detail == "ROUTINE_PUBLICATION_FINGERPRINT_MISMATCH"
    assert armed == []


@pytest.mark.asyncio
async def test_live_route_rejects_active_run_and_daily_quota_before_live(monkeypatch):
    from app.api.routes import routine_publishing as route

    publication = _publication()
    permit = SimpleNamespace(id=PERMIT_ID)
    payload = route.RunOnceLiveRequest(
        confirmed=True,
        confirmation_text_version="ROUTINE_LIVE_ONCE_V1",
        permit_id=PERMIT_ID,
        publication_fingerprint=PUB_FP,
        request_fingerprint=REQ_FP,
    )

    async def run_case(active_run, write_count, expected):
        db = FakeDB(publication)
        armed = []
        monkeypatch.setattr(route, "_actor", lambda request: "operator")
        monkeypatch.setattr(route, "get_settings", lambda: _persistent_settings())
        monkeypatch.setattr(route, "get_control", lambda db_, create=False: SimpleNamespace(state="PAUSED"))
        monkeypatch.setattr(route, "request_fingerprint_for", lambda publication_: REQ_FP)
        monkeypatch.setattr(route, "active_permit", lambda db_, publication_id: permit)
        monkeypatch.setattr(route, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
        monkeypatch.setattr(route, "_active_running_run", lambda db_: active_run)
        monkeypatch.setattr(route, "daily_provider_write_count", lambda db_, day_start: write_count)
        monkeypatch.setattr(route, "set_control", lambda *a, **k: armed.append(True))
        with pytest.raises(HTTPException) as raised:
            await route.run_once_live(PUB_ID, object(), payload, db)
        assert raised.value.detail == expected
        assert armed == []

    await run_case(SimpleNamespace(id="already-running"), 0, "ROUTINE_WORKER_ALREADY_RUNNING")
    await run_case(None, 1, "ROUTINE_DAILY_WRITE_LIMIT_REACHED")


@pytest.mark.asyncio
async def test_live_route_pauses_after_unexpected_worker_exception(monkeypatch):
    from app.api.routes import routine_publishing as route

    publication = _publication()
    db = FakeDB(publication)
    permit = SimpleNamespace(id=PERMIT_ID)
    states = []

    monkeypatch.setattr(route, "_actor", lambda request: "operator")
    monkeypatch.setattr(route, "get_settings", lambda: _persistent_settings())
    monkeypatch.setattr(route, "get_control", lambda db_, create=False: SimpleNamespace(state="PAUSED"))
    monkeypatch.setattr(route, "request_fingerprint_for", lambda publication_: REQ_FP)
    monkeypatch.setattr(route, "active_permit", lambda db_, publication_id: permit)
    monkeypatch.setattr(route, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    monkeypatch.setattr(route, "_active_running_run", lambda db_: None)
    monkeypatch.setattr(route, "daily_provider_write_count", lambda db_, day_start: 0)
    monkeypatch.setattr(
        route,
        "set_control",
        lambda db_, *, state, actor, reason=None: states.append((state, reason)) or SimpleNamespace(state=state),
    )

    async def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(route, "run_routine_worker_once", explode)
    payload = route.RunOnceLiveRequest(
        confirmed=True,
        confirmation_text_version="ROUTINE_LIVE_ONCE_V1",
        permit_id=PERMIT_ID,
        publication_fingerprint=PUB_FP,
        request_fingerprint=REQ_FP,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await route.run_once_live(PUB_ID, object(), payload, db)

    assert states[0][0] == "LIVE"
    assert states[-1] == ("PAUSED", "ONE_SHOT_LIVE_EXCEPTION")


@pytest.mark.asyncio
async def test_worker_targeted_live_is_rejected_without_explicit_authorization(monkeypatch):
    from app.services import routine_pinterest_worker as worker

    monkeypatch.setattr(worker, "get_control", lambda db: SimpleNamespace(state="LIVE", pause_reason=None))
    result = await worker.run_once(
        FakeDB(),
        settings=_effective_live_settings(),
        target_publication_id=PUB_ID,
    )
    assert result == {"status": "ROUTINE_TARGETED_RUN_DRY_RUN_ONLY", "dispatched": 0}


@pytest.mark.asyncio
async def test_worker_explicit_targeted_live_is_exact_single_candidate(monkeypatch):
    from app.services import routine_pinterest_worker as worker

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    publication = SimpleNamespace(
        id=PUB_ID,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=now,
    )
    db = FakeDB(publication)
    run = _run()
    permit = SimpleNamespace(id=PERMIT_ID)
    dispatch_calls = []

    monkeypatch.setattr(worker, "get_control", lambda db_: SimpleNamespace(state="LIVE", pause_reason=None))
    monkeypatch.setattr(worker, "start_run", lambda *a, **k: run)
    monkeypatch.setattr(worker, "heartbeat_run", lambda *a, **k: run)
    monkeypatch.setattr(worker, "finish_run", lambda *a, **k: run)
    monkeypatch.setattr(worker, "recover_stale_routine_claims", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not recover unrelated claims")))
    monkeypatch.setattr(worker, "due_publications", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scan due queue")))
    monkeypatch.setattr(worker, "active_permit", lambda db_, publication_id: permit)
    monkeypatch.setattr(worker, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})

    async def evidence(*a, **k):
        return SimpleNamespace(publication_id=PUB_ID)

    async def dispatch(*a, **k):
        dispatch_calls.append(a[1].id)
        return SimpleNamespace(status=PublicationStatus.PUBLISHED)

    monkeypatch.setattr(worker, "build_routine_execution_evidence", evidence)
    monkeypatch.setattr(worker, "daily_provider_write_count", lambda *a, **k: 0)
    monkeypatch.setattr(worker, "dispatch_routine_buffer", dispatch)

    result = await worker.run_once(
        db,
        settings=_effective_live_settings(),
        now=now,
        target_publication_id=PUB_ID,
        target_permit_id=PERMIT_ID,
        allow_targeted_live=True,
    )

    assert result["scanned"] == 1
    assert result["eligible"] == 1
    assert result["claimed"] == 1
    assert result["dispatched"] == 1
    assert result["published"] == 1
    assert dispatch_calls == [PUB_ID]
    assert [ident for model, ident in db.get_calls if model is PinPublication] == [PUB_ID]


@pytest.mark.asyncio
async def test_worker_targeted_live_enforces_exact_permit_and_daily_limit(monkeypatch):
    from app.services import routine_pinterest_worker as worker

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    publication = SimpleNamespace(id=PUB_ID, status=PublicationStatus.SCHEDULED, scheduled_for=now)
    db = FakeDB(publication)
    permit = SimpleNamespace(id=PERMIT_ID)
    dispatch_calls = []

    def install_common(run):
        monkeypatch.setattr(worker, "get_control", lambda db_: SimpleNamespace(state="LIVE", pause_reason=None))
        monkeypatch.setattr(worker, "start_run", lambda *a, **k: run)
        monkeypatch.setattr(worker, "heartbeat_run", lambda *a, **k: run)
        monkeypatch.setattr(worker, "finish_run", lambda *a, **k: run)
        monkeypatch.setattr(worker, "active_permit", lambda db_, publication_id: permit)
        monkeypatch.setattr(worker, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})

        async def evidence(*a, **k):
            return SimpleNamespace(publication_id=PUB_ID)

        async def dispatch(*a, **k):
            dispatch_calls.append(PUB_ID)
            return SimpleNamespace(status=PublicationStatus.PUBLISHED)

        monkeypatch.setattr(worker, "build_routine_execution_evidence", evidence)
        monkeypatch.setattr(worker, "dispatch_routine_buffer", dispatch)

    run1 = _run()
    install_common(run1)
    result = await worker.run_once(
        db,
        settings=_effective_live_settings(),
        now=now,
        target_publication_id=PUB_ID,
        target_permit_id="different-permit",
        allow_targeted_live=True,
    )
    assert result["error_code"] == "ROUTINE_PERMIT_ID_MISMATCH"
    assert result["dispatched"] == 0

    run2 = _run()
    install_common(run2)
    monkeypatch.setattr(worker, "daily_provider_write_count", lambda *a, **k: 1)
    result = await worker.run_once(
        db,
        settings=_effective_live_settings(),
        now=now,
        target_publication_id=PUB_ID,
        target_permit_id=PERMIT_ID,
        allow_targeted_live=True,
    )
    assert result["error_code"] == "ROUTINE_DAILY_WRITE_LIMIT_REACHED"
    assert result["dispatched"] == 0
    assert dispatch_calls == []


@pytest.mark.asyncio
async def test_worker_targeted_live_preserves_run_lock_and_unknown_circuit_breaker(monkeypatch):
    from app.services import routine_pinterest_worker as worker

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    publication = SimpleNamespace(id=PUB_ID, status=PublicationStatus.SCHEDULED, scheduled_for=now)
    db = FakeDB(publication)

    monkeypatch.setattr(worker, "get_control", lambda db_: SimpleNamespace(state="LIVE", pause_reason=None))
    monkeypatch.setattr(
        worker,
        "start_run",
        lambda *a, **k: (_ for _ in ()).throw(RoutineControlError("ROUTINE_WORKER_ALREADY_RUNNING")),
    )
    result = await worker.run_once(
        db,
        settings=_effective_live_settings(),
        now=now,
        target_publication_id=PUB_ID,
        target_permit_id=PERMIT_ID,
        allow_targeted_live=True,
    )
    assert result == {"status": "ROUTINE_WORKER_ALREADY_RUNNING", "dispatched": 0}

    run = _run()
    permit = SimpleNamespace(id=PERMIT_ID)
    calls = []
    monkeypatch.setattr(worker, "start_run", lambda *a, **k: run)
    monkeypatch.setattr(worker, "heartbeat_run", lambda *a, **k: run)
    monkeypatch.setattr(worker, "finish_run", lambda *a, **k: run)
    monkeypatch.setattr(worker, "active_permit", lambda *a, **k: permit)
    monkeypatch.setattr(worker, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})

    async def evidence(*a, **k):
        return SimpleNamespace(publication_id=PUB_ID)

    async def dispatch(*a, **k):
        calls.append(1)
        return SimpleNamespace(status=PublicationStatus.PUBLISH_UNKNOWN)

    monkeypatch.setattr(worker, "build_routine_execution_evidence", evidence)
    monkeypatch.setattr(worker, "daily_provider_write_count", lambda *a, **k: 0)
    monkeypatch.setattr(worker, "dispatch_routine_buffer", dispatch)

    result = await worker.run_once(
        db,
        settings=_effective_live_settings(),
        now=now,
        target_publication_id=PUB_ID,
        target_permit_id=PERMIT_ID,
        allow_targeted_live=True,
    )
    assert result["dispatched"] == 1
    assert result["unknown"] == 1
    assert result["error_code"] == "PUBLISH_UNKNOWN_CIRCUIT_BREAKER"
    assert calls == [1]
