from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    ContentAngle,
    CreativeTemplate,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    Product,
    ProductImage,
    PublicationAttempt,
    PublicationStatus,
    Store,
)
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
)
from app.services import routine_certified_canary as canary
from app.services import routine_certified_live as certified
from test_routine_canary_fixture import NOW, _db, _scheduler


PUBLICATION_ID = "canary-source"
PERMIT_ID = "permit-1"


def _settings(**overrides):
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "app_secret_key": "s" * 48,
        "publishing_enabled": True,
        "buffer_publishing_enabled": True,
        "buffer_single_pin_pilot_enabled": False,
        "pinterest_single_pin_pilot_enabled": False,
        "routine_pinterest_worker_enabled": False,
        "routine_buffer_dispatch_enabled": False,
        "routine_pinterest_scheduler_enabled": False,
        "routine_autonomous_authorization_enabled": False,
        "routine_pinterest_dry_run": True,
        "routine_pinterest_batch_size": 1,
        "routine_pinterest_daily_write_limit": 1,
        "pinterest_autonomous_generation_enabled": False,
        "pinterest_autonomous_execution_enabled": False,
        "pinterest_autonomous_board_ensure_enabled": False,
        "pinterest_write_scope_enabled": False,
        "pinterest_board_write_scope_enabled": False,
        "pinterest_board_provisioning_enabled": False,
        "buffer_organization_id": "buffer-org-1",
        "buffer_pinterest_channel_id": "buffer-channel-1",
    }
    values.update(overrides)
    return Settings(**values)


def _permit(publication, *, ident=PERMIT_ID, expires_at=None):
    return RoutineDispatchPermit(
        id=ident,
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=publication.approval_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint="d" * 64,
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="pinterest-quality-v1",
        quality_snapshot={"status": "PASS"},
        duplicate_snapshot={"status": "SAFE_TO_CONTINUE"},
        readiness_snapshot={"dispatch_provider": "buffer"},
        authorized_by="test",
        authorized_at=NOW - timedelta(minutes=5),
        expires_at=expires_at or NOW + timedelta(hours=1),
        status="ACTIVE",
    )


def _ready(db):
    publication = db.get(PinPublication, PUBLICATION_ID)
    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = NOW - timedelta(minutes=1)
    db.add(PinterestBoard(
        id=publication.pinterest_board_record_id,
        connection_id=publication.pinterest_connection_id,
        external_board_id=publication.pinterest_board_id_snapshot,
        name="Certified live test board",
        is_active=True,
        is_eligible=True,
        last_synced_at=NOW,
    ))
    db.add(_permit(publication))
    db.commit()


def _evidence(publication_id=PUBLICATION_ID):
    return SimpleNamespace(
        publication_id=publication_id,
        permit_validated=True,
        quality_passed=True,
        duplicate_safe=True,
        persisted_route_validated=True,
        external_requests=0,
    )


@pytest.fixture
def live_case(monkeypatch):
    engine, db = _db()
    _ready(db)
    monkeypatch.setattr(
        canary,
        "validate_permit",
        lambda _db, publication, permit, *, now, require_due: {
            "valid": permit.publication_id == publication.id
            and (permit.expires_at.replace(tzinfo=timezone.utc)
                 if permit.expires_at.tzinfo is None else permit.expires_at) > now,
            "status": "ACTIVE",
        },
    )
    monkeypatch.setattr(
        canary,
        "build_routine_offline_evidence",
        lambda _db, publication, **kwargs: _evidence(publication.id),
    )
    monkeypatch.setattr(canary, "_release_identity", lambda: {"commit": "test-release"})
    yield db
    db.close()
    engine.dispose()


def _preflight(db, *, settings=None, publication_id=PUBLICATION_ID, now=NOW, scheduler=None):
    return certified.preflight_certified_live(
        db,
        publication_id=publication_id,
        settings=settings or _settings(),
        now=now,
        scheduler_snapshot=scheduler or _scheduler(),
    )


def _reserve(db, receipt, *, settings=None, publication_id=PUBLICATION_ID, now=NOW):
    return certified.reserve_certified_live(
        db,
        publication_id=publication_id,
        settings=settings or _settings(),
        receipt=receipt,
        now=now,
        scheduler_snapshot=_scheduler(),
        actor="live-pilot-test",
    )


def _assert_paused_and_no_provider_attempt(db):
    assert db.get(RoutinePublishingControl, "default").state == "PAUSED"
    assert db.get(PinPublication, PUBLICATION_ID).status == PublicationStatus.SCHEDULED
    permit = db.get(RoutineDispatchPermit, PERMIT_ID)
    assert permit.status == "ACTIVE" and permit.consumed_at is None
    assert db.scalars(select(PublicationAttempt)).all() == []
    assert db.scalars(select(RoutineAttemptBoundary)).all() == []


def test_live_preflight_returns_signed_five_minute_receipt_without_provider_access(live_case, monkeypatch):
    from app.integrations.buffer.gateway import BufferGateway

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight and reserve must not call Buffer")

    monkeypatch.setattr(BufferGateway, "create_pinterest_post", forbidden)
    receipt = _preflight(live_case)

    assert receipt["contract_version"] == certified.PREFLIGHT_CONTRACT_VERSION
    assert receipt["publication_id"] == PUBLICATION_ID
    assert receipt["permit_id"] == PERMIT_ID
    issued = datetime.fromisoformat(receipt["issued_at"])
    expires = datetime.fromisoformat(receipt["expires_at"])
    assert expires - issued == timedelta(minutes=5)
    assert len(receipt["signature"]) == 64
    assert _reserve(live_case, receipt) is not None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda receipt: receipt.update(signature="0" * 64),
        lambda receipt: receipt.update(publication_id="different-publication"),
        lambda receipt: receipt.update(permit_id="different-permit"),
        lambda receipt: receipt.update(prerequisite_fingerprint="f" * 64),
        lambda receipt: receipt.update(expires_at=(NOW + timedelta(minutes=6)).isoformat()),
    ],
    ids=["signature", "target", "permit", "state-fingerprint", "expiry-payload"],
)
def test_tampered_live_receipts_are_rejected_without_reservation(live_case, mutation):
    receipt = _preflight(live_case)
    mutation(receipt)

    with pytest.raises(RuntimeError):
        _reserve(live_case, receipt)
    _assert_paused_and_no_provider_attempt(live_case)
    assert live_case.scalars(select(RoutinePublishingRun)).all() == []


def test_live_receipt_expiry_and_clock_drift_fail_closed(live_case):
    receipt = _preflight(live_case)

    with pytest.raises(RuntimeError):
        _reserve(live_case, receipt, now=NOW + timedelta(minutes=5))
    _assert_paused_and_no_provider_attempt(live_case)

    # A future-issued receipt is not accepted even if its signature is sound.
    future_receipt = _preflight(live_case, now=NOW + timedelta(minutes=1))
    with pytest.raises(RuntimeError):
        _reserve(live_case, future_receipt, now=NOW)
    _assert_paused_and_no_provider_attempt(live_case)


@pytest.mark.parametrize(
    ("override", "scheduler"),
    [
        ({"routine_pinterest_worker_enabled": True}, None),
        ({"routine_buffer_dispatch_enabled": True}, None),
        ({"routine_pinterest_scheduler_enabled": True}, None),
        ({"routine_autonomous_authorization_enabled": True}, None),
        ({"pinterest_autonomous_execution_enabled": True}, None),
        ({"pinterest_write_scope_enabled": True}, None),
        ({"buffer_single_pin_pilot_enabled": True}, None),
        ({"routine_pinterest_batch_size": 2}, None),
        ({"routine_pinterest_daily_write_limit": 2}, None),
        ({}, {**_scheduler(), "task_running": True}),
        ({}, {**_scheduler(), "lease_held": True}),
    ],
)
def test_unsafe_runtime_or_scheduler_gate_blocks_live_preflight(live_case, override, scheduler):
    with pytest.raises(RuntimeError):
        _preflight(live_case, settings=_settings(**override), scheduler=scheduler)
    _assert_paused_and_no_provider_attempt(live_case)


@pytest.mark.parametrize("case", ["future_target", "wrong_target", "second_due", "second_active_permit"])
def test_live_preflight_requires_exact_due_target_and_permit_cardinality(live_case, case):
    target = PUBLICATION_ID
    if case == "future_target":
        live_case.get(PinPublication, PUBLICATION_ID).scheduled_for = NOW + timedelta(minutes=2)
        live_case.commit()
    elif case == "wrong_target":
        target = "not-the-authorized-publication"
    elif case in {"second_due", "second_active_permit"}:
        original = live_case.get(PinPublication, PUBLICATION_ID)
        values = {column.name: getattr(original, column.name) for column in PinPublication.__table__.columns}
        values["id"] = "unrelated-publication"
        values["publication_fingerprint"] = "f" * 64
        values["scheduled_for"] = (
            NOW - timedelta(minutes=2) if case == "second_due" else NOW + timedelta(minutes=2)
        )
        other = PinPublication(**values)
        live_case.add(other)
        live_case.flush()
        if case == "second_active_permit":
            live_case.add(_permit(other, ident="permit-extra"))
        live_case.commit()

    with pytest.raises(RuntimeError):
        _preflight(live_case, publication_id=target)
    _assert_paused_and_no_provider_attempt(live_case)


def test_unknown_publication_blocks_live_preflight_globally(live_case):
    other = live_case.get(PinPublication, PUBLICATION_ID)
    values = {column.name: getattr(other, column.name) for column in PinPublication.__table__.columns}
    values.update(
        id="unknown-publication",
        publication_fingerprint="f" * 64,
        status=PublicationStatus.PUBLISH_UNKNOWN,
        scheduled_for=None,
    )
    live_case.add(PinPublication(**values))
    live_case.commit()

    with pytest.raises(RuntimeError):
        _preflight(live_case)
    _assert_paused_and_no_provider_attempt(live_case)


def test_reservation_rechecks_state_drift_and_is_target_scoped(live_case):
    receipt = _preflight(live_case)
    publication = live_case.get(PinPublication, PUBLICATION_ID)
    publication.title_snapshot = "Changed after signed preflight"
    live_case.commit()

    with pytest.raises(RuntimeError):
        _reserve(live_case, receipt)
    _assert_paused_and_no_provider_attempt(live_case)


def test_successful_reservation_cannot_be_replayed(live_case):
    receipt = _preflight(live_case)
    first = _reserve(live_case, receipt)
    assert first is not None

    with pytest.raises(RuntimeError):
        _reserve(live_case, receipt)
    assert len(live_case.scalars(select(RoutinePublishingRun)).all()) == 1


def test_receipt_expires_while_waiting_for_locked_revalidation(live_case, monkeypatch):
    receipt = _preflight(live_case)
    original_evaluate = certified._evaluate

    def delayed_evaluate(*args, **kwargs):
        permit_id, request_fingerprint, state, _ = original_evaluate(*args, **kwargs)
        return permit_id, request_fingerprint, state, datetime.fromisoformat(receipt["expires_at"])

    monkeypatch.setattr(certified, "_evaluate", delayed_evaluate)
    with pytest.raises(certified.CertifiedLiveError, match="CERTIFIED_LIVE_RECEIPT_EXPIRED"):
        _reserve(live_case, receipt)
    _assert_paused_and_no_provider_attempt(live_case)
    assert live_case.scalars(select(RoutinePublishingRun)).all() == []


def test_release_identity_drift_rejects_preflight_receipt(live_case, monkeypatch):
    receipt = _preflight(live_case)
    monkeypatch.setattr(canary, "_release_identity", lambda: {"commit": "changed-release"})

    with pytest.raises(certified.CertifiedLiveError, match="CERTIFIED_LIVE_RELEASE_DRIFT"):
        _reserve(live_case, receipt)
    _assert_paused_and_no_provider_attempt(live_case)


def _seed_recovery_state(
    db,
    *,
    boundary_started=True,
    run_status="FAILED",
    control_state="PAUSED",
    heartbeat_at=None,
):
    publication = db.get(PinPublication, PUBLICATION_ID)
    publication.status = PublicationStatus.PUBLISHING
    control = db.get(RoutinePublishingControl, "default")
    control.state = control_state
    permit = db.get(RoutineDispatchPermit, PERMIT_ID)
    permit.status = "CONSUMED"
    permit.consumed_at = NOW - timedelta(minutes=10)
    attempt = PublicationAttempt(
        id="recovery-attempt",
        publication_id=PUBLICATION_ID,
        attempt_number=1,
        status="STARTED",
        dispatch_provider="buffer",
        request_fingerprint="d" * 64,
        safe_response_metadata={},
        started_at=NOW - timedelta(minutes=10),
    )
    db.add(attempt)
    db.flush()
    db.add(RoutineAttemptBoundary(
        id="recovery-boundary",
        attempt_id=attempt.id,
        publication_id=PUBLICATION_ID,
        routine_dispatch_permit_id=PERMIT_ID,
        claimed_at=NOW - timedelta(minutes=10),
        provider_mutation_started_at=(
            NOW - timedelta(minutes=9) if boundary_started else None
        ),
        safe_metadata={},
    ))
    run = RoutinePublishingRun(
        id="certified-recovery-run",
        mode="LIVE",
        status=run_status,
        started_at=NOW - timedelta(minutes=10),
        heartbeat_at=heartbeat_at or NOW - timedelta(minutes=10),
        completed_at=NOW - timedelta(minutes=9) if run_status != "RUNNING" else None,
        metadata_json={
            "certified_live_version": certified.PREFLIGHT_CONTRACT_VERSION,
            "publication_id": PUBLICATION_ID,
            "permit_id": PERMIT_ID,
        },
    )
    db.add(run)
    db.commit()
    return publication, permit, attempt, run


def test_recovery_after_committed_provider_boundary_fails_closed_unknown(live_case, monkeypatch):
    publication, permit, attempt, run = _seed_recovery_state(
        live_case, boundary_started=True, run_status="FAILED", control_state="PAUSED",
    )
    submissions = []

    def forbidden_submit(*args, **kwargs):
        submissions.append((args, kwargs))
        raise AssertionError("recovery must never submit another provider request")

    from app.integrations.buffer.gateway import BufferGateway

    monkeypatch.setattr(BufferGateway, "create_pinterest_post", forbidden_submit)
    result = certified.recover_certified_live(
        live_case, actor="operator", stale_seconds=60, now=NOW,
    )

    assert result == {"status": "PAUSED", "publication_id": PUBLICATION_ID}
    assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
    assert attempt.status == "UNKNOWN"
    assert permit.status == "CONSUMED" and permit.consumed_at is not None
    assert run.status == "FAILED"
    assert live_case.get(RoutinePublishingControl, "default").state == "PAUSED"
    assert submissions == []
    assert live_case.scalars(select(PublicationAttempt)).all() == [attempt]

    with pytest.raises(certified.CertifiedLiveError, match="CERTIFIED_LIVE_NOT_ARMED"):
        certified.recover_certified_live(
            live_case, actor="operator", stale_seconds=60, now=NOW,
        )
    assert submissions == []


def test_recovery_without_provider_boundary_marks_publication_failed(live_case):
    publication, permit, attempt, _run = _seed_recovery_state(
        live_case, boundary_started=False, run_status="FAILED", control_state="PAUSED",
    )

    result = certified.recover_certified_live(
        live_case, actor="operator", stale_seconds=60, now=NOW,
    )

    assert result["status"] == "PAUSED"
    assert publication.status == PublicationStatus.PUBLISH_FAILED
    assert attempt.status == "FAILED"
    assert attempt.completed_at is not None
    assert permit.status == "CONSUMED" and permit.consumed_at is not None
    assert live_case.get(RoutinePublishingControl, "default").state == "PAUSED"


def test_recovery_rejects_fresh_running_certified_run(live_case):
    publication, permit, attempt, run = _seed_recovery_state(
        live_case,
        boundary_started=True,
        run_status="RUNNING",
        control_state="PAUSED",
        heartbeat_at=NOW.replace(tzinfo=None),
    )
    fresh_now = NOW.replace(tzinfo=None)

    with pytest.raises(certified.CertifiedLiveError, match="CERTIFIED_LIVE_RUN_NOT_STALE"):
        certified.recover_certified_live(
            live_case, actor="operator", stale_seconds=60, now=fresh_now,
        )

    assert publication.status == PublicationStatus.PUBLISHING
    assert attempt.status == "STARTED"
    assert permit.status == "CONSUMED" and permit.consumed_at is not None
    assert run.status == "RUNNING"
    assert live_case.get(RoutinePublishingControl, "default").state == "PAUSED"


def _live_dispatch_settings():
    return _settings(
        routine_pinterest_worker_enabled=True,
        routine_buffer_dispatch_enabled=True,
        routine_pinterest_dry_run=False,
    )


def _prepare_dispatch_db(db):
    publication = db.get(PinPublication, PUBLICATION_ID)
    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = NOW - timedelta(minutes=1)
    db.get(RoutinePublishingControl, "default").state = "LIVE"
    db.commit()
    return publication


def test_mocked_buffer_dispatch_submits_exactly_once(live_case, monkeypatch):
    from app.integrations.buffer.gateway import BufferPostResult
    from app.services import routine_buffer_dispatch as dispatch

    publication = _prepare_dispatch_db(live_case)
    submissions = []
    gateway = SimpleNamespace(
        create_pinterest_post=lambda payload: None,
    )

    async def submit(payload):
        submissions.append(payload)
        return BufferPostResult(
            buffer_post_id="buffer-post-1",
            status="sent",
            channel_id="buffer-channel-1",
            created_at=NOW.isoformat(),
            due_at=None,
            sent_at=NOW.isoformat(),
            external_link="https://www.pinterest.com/pin/123",
        )

    async def reconcile(db, publication_id, **kwargs):
        db.get(PinPublication, publication_id).status = PublicationStatus.PUBLISHED
        db.commit()

    async def verify(*args, **kwargs):
        return None

    monkeypatch.setattr(gateway, "create_pinterest_post", submit)
    monkeypatch.setattr(dispatch, "evidence_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(dispatch, "verify_destination", verify)
    monkeypatch.setattr(dispatch, "build_pinterest_payload", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatch, "reconcile_buffer", reconcile)
    monkeypatch.setattr(dispatch, "validate_permit", lambda *args, **kwargs: {"valid": True})
    monkeypatch.setattr(dispatch, "request_fingerprint_for", lambda publication: "d" * 64)

    result = __import__("asyncio").run(
        dispatch.dispatch_routine_buffer(
            live_case,
            publication,
            evidence=SimpleNamespace(observed_at=NOW),
            settings=_live_dispatch_settings(),
            gateway=gateway,
            now=NOW,
        )
    )

    assert len(submissions) == 1
    assert result.status == PublicationStatus.PUBLISHED
    assert live_case.get(PinPublication, PUBLICATION_ID).status == PublicationStatus.PUBLISHED
    permit = live_case.get(RoutineDispatchPermit, PERMIT_ID)
    attempt = live_case.scalar(select(PublicationAttempt))
    boundary = live_case.scalar(select(RoutineAttemptBoundary))
    assert permit.status == "CONSUMED" and permit.consumed_at is not None
    assert attempt.provider_operation_id == "buffer-post-1"
    assert attempt.provider_operation_status == "sent"
    assert boundary.provider_mutation_started_at is not None
    assert len(live_case.scalars(select(PublicationAttempt)).all()) == 1


@pytest.mark.parametrize("failure", ["ambiguous_response", "unexpected_crash"])
def test_ambiguous_or_crashed_provider_boundary_is_unknown_and_paused(live_case, monkeypatch, failure):
    from app.integrations.buffer.gateway import BufferAmbiguousFailure
    from app.services import routine_buffer_dispatch as dispatch

    publication = _prepare_dispatch_db(live_case)
    calls = []

    class FakeGateway:
        async def create_pinterest_post(self, payload):
            calls.append(payload)
            if failure == "ambiguous_response":
                raise BufferAmbiguousFailure(
                    "BUFFER_RESPONSE_UNCERTAIN",
                    failure_code="read_timeout",
                    phase="waiting_for_response",
                    response_received=False,
                )
            raise RuntimeError("crash after request may have been sent")

    async def verify(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatch, "evidence_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(dispatch, "verify_destination", verify)
    monkeypatch.setattr(dispatch, "build_pinterest_payload", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatch, "validate_permit", lambda *args, **kwargs: {"valid": True})
    monkeypatch.setattr(dispatch, "request_fingerprint_for", lambda publication: "d" * 64)

    result = __import__("asyncio").run(
        dispatch.dispatch_routine_buffer(
            live_case,
            publication,
            evidence=SimpleNamespace(observed_at=NOW),
            settings=_live_dispatch_settings(),
            gateway=FakeGateway(),
            now=NOW,
        )
    )

    assert len(calls) == 1
    assert result.status == PublicationStatus.PUBLISH_UNKNOWN
    assert live_case.get(RoutinePublishingControl, "default").state == "PAUSED"
    assert live_case.get(PinPublication, PUBLICATION_ID).status == PublicationStatus.PUBLISH_UNKNOWN
    attempt = live_case.scalar(select(PublicationAttempt))
    boundary = live_case.scalar(select(RoutineAttemptBoundary))
    assert attempt.status == "UNKNOWN"
    assert boundary.provider_mutation_started_at is not None
    assert live_case.scalars(select(PublicationAttempt)).all() == [attempt]


def test_provider_outcome_persistence_failure_recovers_to_unknown(live_case, monkeypatch):
    from app.integrations.buffer.gateway import BufferPostResult
    from app.services import routine_buffer_dispatch as dispatch

    publication = _prepare_dispatch_db(live_case)
    submissions = []

    class FakeGateway:
        async def create_pinterest_post(self, payload):
            submissions.append(payload)
            return BufferPostResult(
                buffer_post_id="buffer-post-uncertain",
                status="scheduled",
                channel_id="buffer-channel-1",
                created_at=NOW.isoformat(),
                due_at=NOW.isoformat(),
                sent_at=None,
                external_link=None,
            )

    async def verify(*args, **kwargs):
        return None

    def persistence_failure(*args, **kwargs):
        raise RuntimeError("simulated result persistence crash")

    monkeypatch.setattr(dispatch, "evidence_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(dispatch, "verify_destination", verify)
    monkeypatch.setattr(dispatch, "build_pinterest_payload", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatch, "validate_permit", lambda *args, **kwargs: {"valid": True})
    monkeypatch.setattr(dispatch, "request_fingerprint_for", lambda publication: "d" * 64)
    monkeypatch.setattr(dispatch, "_persist_provider_outcome", persistence_failure)

    with pytest.raises(RuntimeError, match="simulated result persistence crash"):
        __import__("asyncio").run(
            dispatch.dispatch_routine_buffer(
                live_case,
                publication,
                evidence=SimpleNamespace(observed_at=NOW),
                settings=_live_dispatch_settings(),
                gateway=FakeGateway(),
                now=NOW,
            )
        )

    attempt = live_case.scalar(select(PublicationAttempt))
    boundary = live_case.scalar(select(RoutineAttemptBoundary))
    permit = live_case.get(RoutineDispatchPermit, PERMIT_ID)
    assert len(submissions) == 1
    assert publication.status == PublicationStatus.PUBLISHING
    assert attempt.status == "STARTED"
    assert boundary.provider_mutation_started_at is not None
    assert permit.status == "CONSUMED"

    live_case.get(RoutinePublishingControl, "default").state = "PAUSED"
    live_case.add(RoutinePublishingRun(
        id="persistence-crash-run",
        mode="LIVE",
        status="FAILED",
        started_at=NOW - timedelta(minutes=2),
        heartbeat_at=NOW - timedelta(minutes=1),
        completed_at=NOW,
        metadata_json={
            "certified_live_version": certified.PREFLIGHT_CONTRACT_VERSION,
            "publication_id": PUBLICATION_ID,
            "permit_id": PERMIT_ID,
        },
    ))
    live_case.commit()

    recovered = certified.recover_certified_live(
        live_case, actor="operator", stale_seconds=60, now=NOW,
    )

    assert recovered["status"] == "PAUSED"
    assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
    assert attempt.status == "UNKNOWN"
    assert permit.status == "CONSUMED"
    assert len(submissions) == 1


def _seed_postgres_target(db, suffix):
    """Insert a fully linked target for the isolated PostgreSQL race test."""
    store_id, product_id = f"live-store-{suffix}", f"live-product-{suffix}"
    angle_id, concept_id = f"live-angle-{suffix}", f"live-concept-{suffix}"
    draft_id, image_id = f"live-draft-{suffix}", f"live-image-{suffix}"
    template_id, creative_id = f"live-template-{suffix}", f"live-creative-{suffix}"
    approval_id, connection_id = f"live-approval-{suffix}", f"live-connection-{suffix}"
    board_id, publication_id = f"live-board-{suffix}", f"live-publication-{suffix}"
    db.add(Store(id=store_id, name="Certified live test", shop_domain=f"{suffix}.example"))
    db.flush()
    db.add_all([
        Product(
            id=product_id, store_id=store_id, shopify_product_id=product_id,
            handle="certified-live-test", title="Certified live test",
            product_url=f"https://{suffix}.example/p/test",
            price_min=Decimal("10.00"), compare_at_min=Decimal("12.00"),
        ),
        ContentAngle(id=angle_id, key="certified-live-test", name="Certified live test"),
    ])
    db.flush()
    db.add(PinConcept(
        id=concept_id, store_id=store_id, product_id=product_id,
        content_angle_id=angle_id, fingerprint="e" * 64,
    ))
    db.flush()
    db.add(PinDraft(
        id=draft_id, concept_id=concept_id, version=1, title="Certified live test",
        description="A test publication description.", alt_text="A test publication.",
        destination_url=f"https://{suffix}.example/p/test",
        utm_url=f"https://{suffix}.example/p/test?utm_source=pinterest",
        text_fingerprint="b" * 64, status="APPROVED",
    ))
    db.add_all([
        ProductImage(
            id=image_id, product_id=product_id,
            source_url=f"https://cdn.{suffix}.example/test.jpg", is_primary=True,
        ),
        CreativeTemplate(id=template_id, key=f"template-{suffix}", version=1, name="Test"),
    ])
    db.flush()
    db.add(PinCreative(
        id=creative_id, draft_id=draft_id, template_id=template_id,
        source_image_id=image_id, creative_fingerprint="c" * 64,
        render_status="RENDERED", rendered_url=f"https://cdn.{suffix}.example/test.jpg",
        width=1000, height=1500,
    ))
    db.flush()
    db.add(PinApproval(
        id=approval_id, draft_id=draft_id, creative_id=creative_id,
        decision="APPROVED", decided_by="test",
    ))
    db.add(PinterestConnection(
        id=connection_id, external_user_id=f"user-{suffix}",
        access_token_ciphertext="test-only", refresh_token_ciphertext="test-only",
        status="CONNECTED", boards_last_synced_at=NOW,
    ))
    db.flush()
    db.add(PinterestBoard(
        id=board_id, connection_id=connection_id, external_board_id=f"board-{suffix}",
        name="Certified live test", is_active=True, is_eligible=True,
        routing_label="test", last_synced_at=NOW,
    ))
    db.flush()
    publication = PinPublication(
        id=publication_id, draft_id=draft_id, creative_id=creative_id,
        approval_id=approval_id, pinterest_connection_id=connection_id,
        pinterest_board_record_id=board_id, pinterest_board_id_snapshot=f"board-{suffix}",
        publication_fingerprint="a" * 64, text_fingerprint="b" * 64,
        creative_fingerprint="c" * 64, status=PublicationStatus.SCHEDULED,
        scheduled_for=NOW - timedelta(minutes=1), title_snapshot="Certified live test",
        description_snapshot="A test publication description.",
        alt_text_snapshot="A test publication.", destination_url=f"https://{suffix}.example/p/test",
        utm_url=f"https://{suffix}.example/p/test?utm_source=pinterest",
        media_url_snapshot=f"https://cdn.{suffix}.example/test.jpg",
        source_image_id=image_id, template_id=template_id,
        template_key=f"template-{suffix}", template_version=1,
    )
    db.add(publication)
    db.flush()
    db.add(_permit(publication, ident=f"live-permit-{suffix}"))
    return publication_id


@pytest.mark.skipif(not os.getenv("TASK595_POSTGRES_URL"), reason="isolated certified-live PostgreSQL URL not set")
def test_postgres_concurrent_reservations_have_single_winner(monkeypatch):
    """PostgreSQL test uses a short-lived private schema, never a production database."""
    url = make_url(os.environ["TASK595_POSTGRES_URL"])
    assert url.host in {"127.0.0.1", "localhost"}
    assert url.database.startswith(("sm_poster_task595", "sm_poster_task596"))
    schema = f"cert_live_{uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    isolated_url = url.render_as_string(hide_password=False)
    engine = create_engine(
        isolated_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        seed = Session()
        seed.add(RoutinePublishingControl(id="default", state="PAUSED", paused_at=NOW, paused_by="test"))
        publication_id = _seed_postgres_target(seed, schema[-8:])
        seed.commit()

        monkeypatch.setattr(canary, "validate_permit", lambda *a, **k: {"valid": True, "status": "ACTIVE"})
        monkeypatch.setattr(
            canary,
            "build_routine_offline_evidence",
            lambda _db, publication, **kwargs: _evidence(publication.id),
        )
        monkeypatch.setattr(canary, "_release_identity", lambda: {"commit": "test-release"})
        settings = _settings(database_url=isolated_url)
        receipt = certified.preflight_certified_live(
            seed, publication_id=publication_id, settings=settings, now=NOW,
            scheduler_snapshot=_scheduler(),
        )
        seed.close()

        ready = threading.Barrier(2)

        def attempt_reservation():
            db = Session()
            try:
                ready.wait(timeout=10)
                return certified.reserve_certified_live(
                    db,
                    publication_id=publication_id,
                    settings=settings,
                    receipt=receipt,
                    now=NOW,
                    scheduler_snapshot=_scheduler(),
                    actor="postgres-race-test",
                )
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt_reservation) for _ in range(2)]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(("reserved", future.result(timeout=30)))
                except RuntimeError:
                    outcomes.append(("rejected", None))
        assert [status for status, _ in outcomes].count("reserved") == 1
        check = Session()
        assert len(check.scalars(select(RoutinePublishingRun)).all()) <= 1
        assert check.get(RoutinePublishingControl, "default").state in {"PAUSED", "LIVE"}
        check.close()
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


@pytest.mark.skipif(not os.getenv("TASK595_POSTGRES_URL"), reason="isolated certified-live PostgreSQL URL not set")
def test_postgres_recovery_waits_for_committed_provider_boundary():
    """Recovery waits on the control lock and observes a concurrently committed boundary."""
    url = make_url(os.environ["TASK595_POSTGRES_URL"])
    assert url.host in {"127.0.0.1", "localhost"}
    assert url.database.startswith(("sm_poster_task595", "sm_poster_task596"))
    schema = f"cert_recover_{uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    isolated_url = url.render_as_string(hide_password=False)
    engine = create_engine(
        isolated_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        seed = Session()
        seed.add(RoutinePublishingControl(id="default", state="PAUSED", paused_at=NOW, paused_by="test"))
        publication_id = _seed_postgres_target(seed, schema[-8:])
        seed.flush()
        publication = seed.get(PinPublication, publication_id)
        publication.status = PublicationStatus.PUBLISHING
        permit = seed.scalar(select(RoutineDispatchPermit).where(
            RoutineDispatchPermit.publication_id == publication_id,
        ))
        permit.status = "CONSUMED"
        permit.consumed_at = NOW - timedelta(minutes=10)
        attempt = PublicationAttempt(
            id="pg-recovery-attempt",
            publication_id=publication_id,
            attempt_number=1,
            status="STARTED",
            dispatch_provider="buffer",
            request_fingerprint=permit.request_fingerprint,
            safe_response_metadata={},
            started_at=NOW - timedelta(minutes=10),
        )
        seed.add(attempt)
        seed.flush()
        boundary = RoutineAttemptBoundary(
            id="pg-recovery-boundary",
            attempt_id=attempt.id,
            publication_id=publication_id,
            routine_dispatch_permit_id=permit.id,
            claimed_at=NOW - timedelta(minutes=10),
            provider_mutation_started_at=None,
            safe_metadata={},
        )
        seed.add(boundary)
        seed.add(RoutinePublishingRun(
            id="pg-recovery-run",
            mode="LIVE",
            status="FAILED",
            started_at=NOW - timedelta(minutes=10),
            heartbeat_at=NOW - timedelta(minutes=9),
            completed_at=NOW - timedelta(minutes=8),
            metadata_json={
                "certified_live_version": certified.PREFLIGHT_CONTRACT_VERSION,
                "publication_id": publication_id,
                "permit_id": permit.id,
            },
        ))
        seed.commit()
        seed.close()

        locker = Session()
        locker.begin()
        locker.scalar(select(RoutinePublishingControl).where(
            RoutinePublishingControl.id == "default",
        ).with_for_update())

        def recover():
            db = Session()
            try:
                return certified.recover_certified_live(
                    db, actor="postgres-lock-test", stale_seconds=60, now=NOW,
                )
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(recover)
            time.sleep(0.2)
            assert not future.done(), "recovery should wait while the singleton control row is locked"
            boundary_writer = Session()
            row = boundary_writer.get(RoutineAttemptBoundary, "pg-recovery-boundary")
            row.provider_mutation_started_at = NOW - timedelta(minutes=9)
            boundary_writer.commit()
            boundary_writer.close()
            locker.commit()
            locker.close()
            assert future.result(timeout=20) == {
                "status": "PAUSED",
                "publication_id": publication_id,
            }

        check = Session()
        assert check.get(PinPublication, publication_id).status == PublicationStatus.PUBLISH_UNKNOWN
        assert check.get(PublicationAttempt, "pg-recovery-attempt").status == "UNKNOWN"
        assert check.get(RoutineDispatchPermit, permit.id).status == "CONSUMED"
        check.close()
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()