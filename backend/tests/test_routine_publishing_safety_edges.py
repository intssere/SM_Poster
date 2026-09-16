from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.integrations.buffer.gateway import BufferAmbiguousFailure, BufferDefinitiveRejection
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import RoutineAttemptBoundary, RoutineDispatchPermit, RoutinePublishingControl
from app.services.publication_scheduler import due_publications, request_fingerprint_for
from app.services.routine_buffer_preflight import RoutineExecutionEvidence


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, SessionLocal()


def _publication(ident, char="r"):
    return PinPublication(
        id=ident,
        draft_id=f"{ident}-draft",
        creative_id=f"{ident}-creative",
        approval_id=f"{ident}-approval",
        pinterest_board_record_id=f"{ident}-board",
        pinterest_connection_id=f"{ident}-connection",
        pinterest_board_id_snapshot="1093811896939213383",
        publication_fingerprint=char * 64,
        text_fingerprint=char.upper() * 64,
        creative_fingerprint=char.lower() * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
        title_snapshot="Title",
        description_snapshot="Description",
        alt_text_snapshot="Alt",
        destination_url="https://diamondshelf.us/products/item",
        utm_url="https://diamondshelf.us/products/item?utm_source=pinterest",
        media_url_snapshot="https://cdn.shopify.com/item.jpg",
        source_image_id=f"{ident}-source",
        template_id=f"{ident}-template",
        template_key="template",
        template_version=1,
    )


def _permit(publication, ident):
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
        authorized_by="tester", authorized_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), status="ACTIVE",
    )


def _settings(**overrides):
    values = dict(
        database_url="sqlite:///:memory:", publishing_enabled=True,
        buffer_publishing_enabled=True, routine_pinterest_worker_enabled=True,
        routine_buffer_dispatch_enabled=True, routine_pinterest_dry_run=False,
        buffer_api_key="test-key", buffer_organization_id="org", buffer_pinterest_channel_id="channel",
    )
    values.update(overrides)
    return Settings(**values)


def test_every_runtime_kill_switch_blocks_live_gate():
    from app.services.routine_buffer_dispatch import _live_gates

    control = type("Control", (), {"state": "LIVE"})()
    cases = [
        ("publishing_enabled", "PUBLISHING_DISABLED"),
        ("buffer_publishing_enabled", "BUFFER_PUBLISHING_DISABLED"),
        ("routine_pinterest_worker_enabled", "ROUTINE_WORKER_DISABLED"),
        ("routine_buffer_dispatch_enabled", "ROUTINE_BUFFER_DISPATCH_DISABLED"),
    ]
    for field, code in cases:
        settings = _settings(**{field: False})
        assert _live_gates(settings, control) == (False, code)
    assert _live_gates(_settings(routine_pinterest_dry_run=True), control) == (False, "ROUTINE_DRY_RUN_ENABLED")
    paused = type("Control", (), {"state": "PAUSED"})()
    assert _live_gates(_settings(), paused) == (False, "ROUTINE_CONTROL_NOT_LIVE")


def test_provider_operation_id_remains_globally_unique_across_publications():
    engine, db = _db()
    first = _publication("op-one", "a"); second = _publication("op-two", "b")
    db.add_all([first, second]); db.commit()
    a = PublicationAttempt(publication_id=first.id, attempt_number=1, status="UNKNOWN", dispatch_provider="buffer", provider_operation_id="same-operation", safe_response_metadata={})
    b = PublicationAttempt(publication_id=second.id, attempt_number=1, status="UNKNOWN", dispatch_provider="buffer", provider_operation_id="same-operation", safe_response_metadata={})
    db.add(a); db.commit(); db.add(b)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback(); db.close(); engine.dispose()


@pytest.mark.asyncio
async def test_definitive_rejection_performs_exactly_one_provider_create(monkeypatch):
    from app.services import routine_buffer_dispatch as service

    engine, db = _db()
    publication = _publication("definitive", "c"); permit = _permit(publication, "permit-def")
    db.add_all([publication, permit, RoutinePublishingControl(id="default", state="LIVE")]); db.commit()
    monkeypatch.setattr(service, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    async def verify(*a, **k): return {"ok": True}
    monkeypatch.setattr(service, "verify_destination", verify)
    monkeypatch.setattr(service, "build_pinterest_payload", lambda *a, **k: object())
    evidence = RoutineExecutionEvidence(
        publication_id=publication.id, publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication), buffer_organization_id="org",
        buffer_pinterest_channel_id="channel", board_service_id=publication.pinterest_board_id_snapshot,
        media_url=publication.media_url_snapshot, observed_at=datetime.now(timezone.utc),
        provider_destination_live_verified=True, media_live_fetch_verified=True,
    )
    class Gateway:
        calls = 0
        async def create_pinterest_post(self, payload):
            self.calls += 1
            raise BufferDefinitiveRejection(
                "rejected", failure_code="provider_rejected", phase="mutation_boundary",
                http_status=400, response_received=True, request_send_state="sent",
            )
    gateway = Gateway()
    result = await service.dispatch_routine_buffer(db, publication, evidence=evidence, settings=_settings(), gateway=gateway)
    assert gateway.calls == 1
    assert result.status == PublicationStatus.PUBLISH_FAILED
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)).all()
    boundaries = db.scalars(select(RoutineAttemptBoundary).where(RoutineAttemptBoundary.publication_id == publication.id)).all()
    assert len(attempts) == 1 and attempts[0].status == "FAILED"
    assert len(boundaries) == 1 and boundaries[0].provider_mutation_started_at is not None
    db.close(); engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_provider_result_becomes_unknown_pauses_and_is_never_due(monkeypatch):
    from app.services import routine_buffer_dispatch as service

    engine, db = _db()
    publication = _publication("ambiguous", "d"); permit = _permit(publication, "permit-amb")
    control = RoutinePublishingControl(id="default", state="LIVE")
    db.add_all([publication, permit, control]); db.commit()
    monkeypatch.setattr(service, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    async def verify(*a, **k): return {"ok": True}
    monkeypatch.setattr(service, "verify_destination", verify)
    monkeypatch.setattr(service, "build_pinterest_payload", lambda *a, **k: object())
    evidence = RoutineExecutionEvidence(
        publication_id=publication.id, publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication), buffer_organization_id="org",
        buffer_pinterest_channel_id="channel", board_service_id=publication.pinterest_board_id_snapshot,
        media_url=publication.media_url_snapshot, observed_at=datetime.now(timezone.utc),
        provider_destination_live_verified=True, media_live_fetch_verified=True,
    )
    class Gateway:
        calls = 0
        async def create_pinterest_post(self, payload):
            self.calls += 1
            raise BufferAmbiguousFailure(
                "ambiguous", failure_code="timeout", phase="mutation_boundary",
                response_received=None, request_send_state="unknown",
            )
    gateway = Gateway()
    result = await service.dispatch_routine_buffer(db, publication, evidence=evidence, settings=_settings(), gateway=gateway)
    db.refresh(control)
    assert gateway.calls == 1
    assert result.status == PublicationStatus.PUBLISH_UNKNOWN
    assert control.state == "PAUSED" and control.last_unknown_publication_id == publication.id
    assert publication.id not in {row.id for row in due_publications(db, now=datetime.now(timezone.utc), limit=25)}
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)).all()
    assert len(attempts) == 1 and attempts[0].status == "UNKNOWN"
    db.close(); engine.dispose()
