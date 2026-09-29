from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
)
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_buffer_dispatch import RoutineDispatchError, claim_for_routine


def _publication():
    return PinPublication(
        id="scheduled-live-publication",
        draft_id="draft",
        creative_id="creative",
        approval_id="approval",
        pinterest_board_record_id="board-record",
        pinterest_connection_id="connection",
        pinterest_board_id_snapshot="external-board",
        publication_fingerprint="p" * 64,
        text_fingerprint="t" * 64,
        creative_fingerprint="c" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
    )


def _permit(publication):
    return RoutineDispatchPermit(
        id="scheduled-live-permit",
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=publication.approval_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="test",
        quality_snapshot={},
        duplicate_snapshot={},
        readiness_snapshot={},
        authorized_by="test",
        authorized_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        status="ACTIVE",
    )


def _settings(*, admission_enabled):
    return Settings(
        database_url="sqlite:///:memory:",
        publishing_enabled=True,
        buffer_publishing_enabled=True,
        routine_pinterest_worker_enabled=True,
        routine_buffer_dispatch_enabled=True,
        routine_scheduled_live_admission_enabled=admission_enabled,
        routine_pinterest_dry_run=False,
    )


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def test_scheduled_claim_requires_explicit_live_admission_settings(monkeypatch):
    from app.services import routine_buffer_dispatch as dispatch

    engine, db = _session()
    publication = _publication()
    permit = _permit(publication)
    db.add_all([publication, permit])
    db.commit()
    monkeypatch.setattr(dispatch, "validate_permit", lambda *args, **kwargs: {
        "valid": True, "status": "ACTIVE",
    })
    monkeypatch.setattr(dispatch, "_scheduled_plan_item_id", lambda *args, **kwargs: "portfolio-item")
    monkeypatch.setattr(
        dispatch,
        "admit_scheduled_publication",
        lambda *args, **kwargs: pytest.fail("disabled admission must not be called"),
    )

    with pytest.raises(RoutineDispatchError, match="SCHEDULED_LIVE_ADMISSION_DISABLED"):
        claim_for_routine(db, publication, permit)

    db.expire_all()
    assert db.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
    assert db.get(RoutineDispatchPermit, permit.id).status == "ACTIVE"
    assert db.scalars(select(PublicationAttempt)).all() == []
    assert db.scalars(select(RoutineAttemptBoundary)).all() == []
    db.close()
    engine.dispose()


def test_scheduled_claim_uses_admission_cas_then_creates_routine_attempt(monkeypatch):
    from app.services import routine_buffer_dispatch as dispatch

    engine, db = _session()
    publication = _publication()
    permit = _permit(publication)
    db.add_all([
        publication,
        permit,
        RoutinePublishingControl(id="default", state="LIVE"),
    ])
    db.commit()
    monkeypatch.setattr(dispatch, "validate_permit", lambda *args, **kwargs: {
        "valid": True, "status": "ACTIVE",
    })
    monkeypatch.setattr(dispatch, "_scheduled_plan_item_id", lambda *args, **kwargs: "portfolio-item")
    admissions = []

    def admit(db_arg, *, publication_id, plan_item_id, settings, now):
        admissions.append((publication_id, plan_item_id, settings, now))
        result = db_arg.execute(
            update(PinPublication)
            .where(PinPublication.id == publication_id)
            .values(status=PublicationStatus.PUBLISHING, attempt_started_at=now)
        )
        assert result.rowcount == 1

    monkeypatch.setattr(dispatch, "admit_scheduled_publication", admit)
    now = datetime.now(timezone.utc)
    settings = _settings(admission_enabled=True)

    attempt = claim_for_routine(db, publication, permit, settings=settings, now=now)

    assert attempt is not None
    assert len(admissions) == 1
    assert admissions[0][:2] == (publication.id, "portfolio-item")
    assert admissions[0][2] is settings
    assert admissions[0][3] == now
    assert db.get(PinPublication, publication.id).status == PublicationStatus.PUBLISHING
    db.refresh(permit)
    assert permit.status == "CONSUMED"
    assert db.scalar(select(PublicationAttempt).where(
        PublicationAttempt.publication_id == publication.id
    )) is not None
    assert db.scalar(select(RoutineAttemptBoundary).where(
        RoutineAttemptBoundary.publication_id == publication.id
    )) is not None
    db.close()
    engine.dispose()