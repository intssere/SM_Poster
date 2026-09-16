from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import RoutineDispatchPermit, RoutinePublishingControl
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_buffer_preflight import RoutineExecutionEvidence


@pytest.mark.asyncio
async def test_sent_result_reconciles_to_published_without_pausing(monkeypatch):
    from app.services import routine_buffer_dispatch as service

    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    publication = PinPublication(
        id="sent-success", draft_id="sent-draft", creative_id="sent-creative",
        approval_id="sent-approval", pinterest_board_record_id="sent-board",
        pinterest_connection_id="sent-connection", pinterest_board_id_snapshot="1093811896939213383",
        publication_fingerprint="s" * 64, text_fingerprint="t" * 64, creative_fingerprint="c" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
        title_snapshot="Title", description_snapshot="Description", alt_text_snapshot="Alt",
        destination_url="https://diamondshelf.us/products/item",
        utm_url="https://diamondshelf.us/products/item?utm_source=pinterest",
        media_url_snapshot="https://cdn.shopify.com/item.jpg", source_image_id="sent-image",
        template_id="sent-template", template_key="template", template_version=1,
    )
    permit = RoutineDispatchPermit(
        id="sent-permit", publication_id=publication.id, dispatch_provider="buffer",
        approval_id=publication.approval_id, pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication), scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="pinterest-quality-v1", quality_snapshot={}, duplicate_snapshot={},
        readiness_snapshot={"dispatch_provider": "buffer"}, authorized_by="tester",
        authorized_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(hours=1), status="ACTIVE",
    )
    control = RoutinePublishingControl(id="default", state="LIVE")
    db.add_all([publication, permit, control]); db.commit()

    monkeypatch.setattr(service, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
    async def verify(*a, **k): return {"ok": True}
    monkeypatch.setattr(service, "verify_destination", verify)
    monkeypatch.setattr(service, "build_pinterest_payload", lambda *a, **k: object())

    async def reconcile(db_, publication_id, **kwargs):
        row = db_.get(PinPublication, publication_id)
        attempt = db_.scalar(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication_id))
        row.status = PublicationStatus.PUBLISHED
        row.error_code = None
        row.pinterest_pin_id = "123456789"
        row.published_at = datetime.now(timezone.utc)
        attempt.status = "SUCCEEDED"
        attempt.error_code = None
        attempt.provider_pin_id = "123456789"
        attempt.completed_at = datetime.now(timezone.utc)
        db_.commit()
        return row
    monkeypatch.setattr(service, "reconcile_buffer", reconcile)

    settings = Settings(
        database_url="sqlite:///:memory:", publishing_enabled=True, buffer_publishing_enabled=True,
        routine_pinterest_worker_enabled=True, routine_buffer_dispatch_enabled=True,
        routine_pinterest_dry_run=False, buffer_api_key="test-key",
        buffer_organization_id="org", buffer_pinterest_channel_id="channel",
    )
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
            return SimpleNamespace(buffer_post_id="buffer-op-success", status="sent", external_link="https://www.pinterest.com/pin/123456789/")
    gateway = Gateway()

    result = await service.dispatch_routine_buffer(db, publication, evidence=evidence, settings=settings, gateway=gateway)
    db.refresh(control)
    assert gateway.calls == 1
    assert result.status == PublicationStatus.PUBLISHED
    assert result.pinterest_pin_id == "123456789"
    assert control.state == "LIVE"
    db.close(); engine.dispose()
