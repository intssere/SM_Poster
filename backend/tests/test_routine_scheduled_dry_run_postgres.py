"""End-to-end scheduled DRY_RUN admission against disposable PostgreSQL."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
    RoutineScheduledQuotaReservation,
)
from app.services import routine_pinterest_scheduler as scheduler
from app.services import routine_pinterest_worker as worker
from app.services.routine_buffer_dispatch import RoutineDispatchError, claim_for_routine
from test_routine_scheduled_commitments_postgres import local_postgres_url
from test_scheduled_autonomous_readiness import (
    NOW,
    FakeLease,
    TripwireProviderGateway,
    _seed_positive_ready_autonomous_chain,
    _settings,
)


@pytest.fixture
def postgres_sessions(local_postgres_url):
    engine = create_engine(local_postgres_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    yield engine, sessions
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_worker_previews_locked_claim_without_persisting_it(
    postgres_sessions,
):
    _, sessions = postgres_sessions
    scheduler.reset_scheduler_state_for_tests()
    try:
        with sessions() as seed:
            item, publication, permit = _seed_positive_ready_autonomous_chain(seed)
        settings = _settings(routine_scheduled_autonomy_enabled=True)
        gateway = TripwireProviderGateway()

        async def real_worker(db, *, settings):
            return await worker.run_once(
                db, settings=settings, gateway=gateway, now=NOW,
            )

        result = await scheduler.scheduler_tick(
            settings=settings,
            session_factory=sessions,
            runner=real_worker,
            leader_lease=FakeLease(),
        )
        assert result["status"] == "SUCCEEDED"
        assert result["scanned"] == 1
        assert result["eligible"] == 1
        assert result["skipped"] == 0
        assert result["claimed"] == result["dispatched"] == 0
        assert gateway.calls == []
        with sessions() as check:
            receipts = check.scalar(select(RoutinePublishingRun)).metadata_json[
                "scheduled_autonomy_certificates"
            ]
            assert len(receipts) == 1
            receipt = receipts[0]
            assert receipt["publication_id"] == publication.id
            assert receipt["portfolio_item_id"] == item.id
            assert receipt["ready"] is True
            assert receipt["blockers"] == []
            assert receipt["offline_validated"] is True
            assert receipt["external_requests"] == 0
            assert receipt["atomic_admission"] == {
                "evaluated": True,
                "would_admit": True,
                "reason": None,
                "claim_committed": False,
                "reservation_committed": False,
            }
            assert check.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
            assert check.get(RoutineDispatchPermit, permit.id).status == "ACTIVE"
            assert check.scalars(select(RoutineScheduledQuotaReservation)).all() == []
            assert check.scalars(select(PublicationAttempt)).all() == []
            assert check.scalars(select(RoutineAttemptBoundary)).all() == []
    finally:
        scheduler.reset_scheduler_state_for_tests()


def _synthetic_live_claim_settings():
    # Only used for a direct claim transaction in the disposable local database.
    return _settings(
        publishing_enabled=True,
        buffer_publishing_enabled=True,
        routine_buffer_dispatch_enabled=True,
        routine_pinterest_dry_run=False,
        routine_scheduled_live_admission_enabled=True,
    )


def test_real_claim_commits_reservation_status_permit_and_attempt_together(
    postgres_sessions,
):
    _, sessions = postgres_sessions
    with sessions() as db:
        item, publication, permit = _seed_positive_ready_autonomous_chain(db)
        db.get(RoutinePublishingControl, "default").state = "LIVE"
        db.commit()
        attempt = claim_for_routine(
            db, publication, permit, settings=_synthetic_live_claim_settings(), now=NOW,
        )
        assert attempt is not None
    with sessions() as check:
        rows = check.scalars(select(RoutineScheduledQuotaReservation)).all()
        assert len(rows) == 1
        assert rows[0].publication_id == publication.id
        assert rows[0].plan_item_id == item.id
        assert check.get(PinPublication, publication.id).status == PublicationStatus.PUBLISHING
        assert check.get(RoutineDispatchPermit, permit.id).status == "CONSUMED"
        assert len(check.scalars(select(PublicationAttempt)).all()) == 1
        assert len(check.scalars(select(RoutineAttemptBoundary)).all()) == 1


def test_failed_permit_cas_rolls_back_real_admission_and_claim(
    postgres_sessions, monkeypatch,
):
    from app.services import routine_buffer_dispatch as dispatch

    _, sessions = postgres_sessions
    with sessions() as db:
        _, publication, permit = _seed_positive_ready_autonomous_chain(db)
        publication_id, permit_id = publication.id, permit.id
        db.get(RoutinePublishingControl, "default").state = "LIVE"
        db.commit()
        # Force a late permit CAS failure after the real PostgreSQL reservation
        # and publication CAS have succeeded; no provider call is possible here.
        monkeypatch.setattr(dispatch, "validate_permit", lambda *args, **kwargs: {
            "valid": True, "status": "ACTIVE",
        })
        permit.request_fingerprint = "not-the-publication-fingerprint"
        db.commit()
        assert claim_for_routine(
            db, publication, permit, settings=_synthetic_live_claim_settings(), now=NOW,
        ) is None
    with sessions() as check:
        assert check.scalars(select(RoutineScheduledQuotaReservation)).all() == []
        assert check.get(PinPublication, publication_id).status == PublicationStatus.SCHEDULED
        assert check.get(RoutineDispatchPermit, permit_id).status == "ACTIVE"
        assert check.scalars(select(PublicationAttempt)).all() == []
        assert check.scalars(select(RoutineAttemptBoundary)).all() == []


def test_lost_plan_link_does_not_fall_through_to_manual_claim(postgres_sessions):
    from app.models.domain import PinterestPortfolioPlanItem

    _, sessions = postgres_sessions
    with sessions() as db:
        item, publication, permit = _seed_positive_ready_autonomous_chain(db)
        publication_id, permit_id = publication.id, permit.id
        item.publication_id = None
        db.commit()
        with pytest.raises(RoutineDispatchError, match="SCHEDULED_ADMISSION_LINEAGE_MISSING"):
            claim_for_routine(db, publication, permit, now=NOW)
    with sessions() as check:
        assert check.scalars(select(RoutineScheduledQuotaReservation)).all() == []
        assert check.get(PinPublication, publication_id).status == PublicationStatus.SCHEDULED
        assert check.get(RoutineDispatchPermit, permit_id).status == "ACTIVE"
        assert check.scalars(select(PublicationAttempt)).all() == []
        assert check.scalars(select(RoutineAttemptBoundary)).all() == []