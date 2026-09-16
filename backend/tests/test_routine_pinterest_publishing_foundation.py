from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
)
from app.services.publication_scheduler import due_publications, request_fingerprint_for


def _engine(url="sqlite+pysqlite:///:memory:"):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine


def _publication(*, ident="routine-pub", status=PublicationStatus.SCHEDULED, scheduled_for=None, fingerprint_char="r"):
    return PinPublication(
        id=ident,
        draft_id=f"{ident}-draft",
        creative_id=f"{ident}-creative",
        approval_id=f"{ident}-approval",
        pinterest_board_record_id=f"{ident}-board-record",
        pinterest_connection_id=f"{ident}-connection",
        pinterest_board_id_snapshot="1093811896939213383",
        publication_fingerprint=fingerprint_char * 64,
        text_fingerprint=(fingerprint_char.upper()) * 64,
        creative_fingerprint=(fingerprint_char.lower()) * 64,
        status=status,
        scheduled_for=scheduled_for or datetime.now(timezone.utc) - timedelta(minutes=5),
        title_snapshot="Routine title",
        description_snapshot="Routine description",
        alt_text_snapshot="Routine alt",
        destination_url="https://diamondshelf.us/products/routine",
        utm_url="https://diamondshelf.us/products/routine?utm_source=pinterest",
        media_url_snapshot="https://cdn.example.test/routine.png",
        source_image_id=f"{ident}-image",
        template_id=f"{ident}-template",
        template_key="routine-template",
        template_version=1,
    )


def _permit(publication, *, ident="routine-permit", status="ACTIVE"):
    return RoutineDispatchPermit(
        id=ident,
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=publication.approval_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="pinterest-quality-v1",
        quality_snapshot={}, duplicate_snapshot={}, readiness_snapshot={"dispatch_provider": "buffer"},
        authorized_by="tester",
        authorized_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        status=status,
    )


def test_routine_configuration_defaults_fail_closed():
    settings = Settings(database_url="sqlite:///:memory:")
    assert settings.routine_pinterest_worker_enabled is False
    assert settings.routine_buffer_dispatch_enabled is False
    assert settings.routine_pinterest_dry_run is True
    assert settings.routine_pinterest_batch_size == 1
    assert settings.routine_pinterest_daily_write_limit == 1
    with pytest.raises(Exception):
        Settings(database_url="sqlite:///:memory:", routine_pinterest_batch_size=26)


def test_stale_worker_run_is_failed_before_new_singleton_run_starts():
    from app.services.routine_publishing_control import start_run

    engine = _engine(); SessionLocal = sessionmaker(bind=engine, expire_on_commit=False); db = SessionLocal()
    now = datetime.now(timezone.utc)
    stale = RoutinePublishingRun(
        id="stale-run", mode="LIVE", started_at=now - timedelta(hours=1),
        heartbeat_at=now - timedelta(hours=1), status="RUNNING", metadata_json={},
    )
    db.add(stale); db.commit()
    new = start_run(db, mode="DRY_RUN", now=now, stale_seconds=60)
    db.refresh(stale)
    assert stale.status == "FAILED" and stale.error_code == "ROUTINE_WORKER_STALE"
    assert new.status == "RUNNING" and new.id != stale.id
    db.close(); engine.dispose()


def test_claim_is_single_winner_and_consumes_only_routine_permit(tmp_path, monkeypatch):
    from app.services import routine_buffer_dispatch as service

    engine = _engine(f"sqlite+pysqlite:///{(tmp_path / 'routine-cas.sqlite').as_posix()}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    seed = SessionLocal()
    publication = _publication()
    permit = _permit(publication)
    seed.add_all([publication, permit]); seed.commit(); seed.close()

    monkeypatch.setattr(service, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    a = SessionLocal(); b = SessionLocal()
    try:
        pa = a.get(PinPublication, publication.id); pb = b.get(PinPublication, publication.id)
        qa = a.get(RoutineDispatchPermit, permit.id); qb = b.get(RoutineDispatchPermit, permit.id)
        first = service.claim_for_routine(a, pa, qa)
        assert first is not None and first.attempt_number == 1 and first.dispatch_provider == "buffer"
        second = service.claim_for_routine(b, pb, qb)
        assert second is None
        check = SessionLocal()
        assert check.get(PinPublication, publication.id).status == PublicationStatus.PUBLISHING
        assert check.get(RoutineDispatchPermit, permit.id).status == "CONSUMED"
        attempts = check.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)).all()
        boundaries = check.scalars(select(RoutineAttemptBoundary).where(RoutineAttemptBoundary.publication_id == publication.id)).all()
        assert len(attempts) == 1 and len(boundaries) == 1
        check.close()
    finally:
        a.close(); b.close(); engine.dispose()


def test_stale_claim_before_provider_boundary_becomes_definitive_failure(monkeypatch):
    from app.services import routine_buffer_dispatch as service

    engine = _engine(); SessionLocal = sessionmaker(bind=engine, expire_on_commit=False); db = SessionLocal()
    publication = _publication(ident="pre-boundary")
    permit = _permit(publication, ident="pre-boundary-permit")
    db.add_all([publication, permit]); db.commit()
    monkeypatch.setattr(service, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    attempt = service.claim_for_routine(db, publication, permit)
    attempt.started_at = datetime.now(timezone.utc) - timedelta(hours=1); db.commit()
    recovered = service.recover_stale_routine_claims(db, stale_seconds=60)
    db.refresh(publication); db.refresh(attempt)
    assert recovered == [(publication.id, "PUBLISH_FAILED")]
    assert publication.status == PublicationStatus.PUBLISH_FAILED
    assert attempt.status == "FAILED"
    assert db.get(RoutinePublishingControl, "default") is None
    db.close(); engine.dispose()


def test_stale_claim_after_provider_boundary_becomes_unknown_and_pauses(monkeypatch):
    from app.services import routine_buffer_dispatch as service

    engine = _engine(); SessionLocal = sessionmaker(bind=engine, expire_on_commit=False); db = SessionLocal()
    publication = _publication(ident="post-boundary", fingerprint_char="z")
    permit = _permit(publication, ident="post-boundary-permit")
    db.add_all([publication, permit]); db.commit()
    monkeypatch.setattr(service, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    attempt = service.claim_for_routine(db, publication, permit)
    attempt.started_at = datetime.now(timezone.utc) - timedelta(hours=1); db.commit()
    service.mark_provider_mutation_boundary(db, attempt.id, now=datetime.now(timezone.utc) - timedelta(minutes=30))
    recovered = service.recover_stale_routine_claims(db, stale_seconds=60)
    db.refresh(publication); db.refresh(attempt)
    control = db.get(RoutinePublishingControl, "default")
    assert recovered == [(publication.id, "PUBLISH_UNKNOWN")]
    assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
    assert attempt.status == "UNKNOWN"
    assert control.state == "PAUSED" and control.last_unknown_publication_id == publication.id
    db.close(); engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_creates_no_attempt_or_claim(monkeypatch):
    from app.services import routine_pinterest_worker as worker
    from app.services.routine_publishing_control import get_control

    engine = _engine(); SessionLocal = sessionmaker(bind=engine, expire_on_commit=False); db = SessionLocal()
    publication = _publication(ident="dry-run", fingerprint_char="d")
    db.add(publication); db.commit()
    control = get_control(db); control.state = "DRY_RUN"; control.pause_reason = None; db.commit()
    dummy_permit = SimpleNamespace(id="permit")
    dummy_evidence = SimpleNamespace(publication_id=publication.id)
    monkeypatch.setattr(worker, "active_permit", lambda *a, **k: dummy_permit)
    monkeypatch.setattr(worker, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    async def preflight(*a, **k): return dummy_evidence
    monkeypatch.setattr(worker, "build_routine_execution_evidence", preflight)
    settings = Settings(
        database_url="sqlite:///:memory:", routine_pinterest_worker_enabled=True,
        routine_buffer_dispatch_enabled=True, routine_pinterest_dry_run=True,
        routine_pinterest_batch_size=1,
    )
    result = await worker.run_once(db, settings=settings, now=datetime.now(timezone.utc))
    db.refresh(publication)
    assert result["mode"] == "DRY_RUN" and result["dispatched"] == 0
    assert publication.status == PublicationStatus.SCHEDULED
    assert db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)).all() == []
    assert db.scalars(select(RoutineAttemptBoundary).where(RoutineAttemptBoundary.publication_id == publication.id)).all() == []
    db.close(); engine.dispose()


def test_historical_terminal_and_unknown_states_are_not_worker_queue_candidates():
    engine = _engine(); SessionLocal = sessionmaker(bind=engine, expire_on_commit=False); db = SessionLocal()
    unknown = _publication(ident="pilot-unknown", status=PublicationStatus.PUBLISH_UNKNOWN, fingerprint_char="u")
    published = _publication(ident="pilot-published", status=PublicationStatus.PUBLISHED, fingerprint_char="p")
    scheduled = _publication(ident="new-scheduled", status=PublicationStatus.SCHEDULED, fingerprint_char="s")
    db.add_all([unknown, published, scheduled]); db.commit()
    ids = {row.id for row in due_publications(db, now=datetime.now(timezone.utc), limit=25)}
    assert scheduled.id in ids
    assert unknown.id not in ids and published.id not in ids
    assert db.scalars(select(RoutineDispatchPermit).where(RoutineDispatchPermit.publication_id.in_([unknown.id, published.id]))).all() == []
    db.close(); engine.dispose()


@pytest.mark.asyncio
async def test_routine_reconciler_has_no_dispatch_path(monkeypatch):
    from app.services import routine_buffer_reconciliation as reconciliation

    engine = _engine(); SessionLocal = sessionmaker(bind=engine, expire_on_commit=False); db = SessionLocal()
    publication = _publication(ident="reconcile-only", status=PublicationStatus.PUBLISH_UNKNOWN, fingerprint_char="q")
    permit = _permit(publication, ident="reconcile-permit", status="CONSUMED")
    attempt = PublicationAttempt(
        id="reconcile-attempt", publication_id=publication.id, attempt_number=1,
        status="UNKNOWN", dispatch_provider="buffer", request_fingerprint=request_fingerprint_for(publication),
        provider_operation_id="buffer-operation-1", safe_response_metadata={},
    )
    boundary = RoutineAttemptBoundary(
        attempt_id=attempt.id, publication_id=publication.id, routine_dispatch_permit_id=permit.id,
        claimed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        provider_mutation_started_at=datetime.now(timezone.utc) - timedelta(minutes=9), safe_metadata={},
    )
    db.add_all([publication, permit, attempt, boundary]); db.commit()
    calls = []
    async def fake_reconcile(db_, publication_id, **kwargs):
        calls.append(publication_id)
        row = db_.get(PinPublication, publication_id)
        row.status = PublicationStatus.PUBLISHED
        db_.commit()
        return row
    monkeypatch.setattr(reconciliation, "reconcile_buffer", fake_reconcile)
    result = await reconciliation.run_reconciliation_once(db, settings=Settings(database_url="sqlite:///:memory:"))
    assert calls == [publication.id]
    assert result == {"checked": 1, "reconciled": 1, "unresolved": 0, "dispatched": 0}
    db.close(); engine.dispose()
