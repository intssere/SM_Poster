from app.models.domain import Board
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.domain import (
    PinPublication,
    PinterestBoard,
    ProductImage,
    PublicationAttempt,
    PublicationDispatchAuthorization,
    PublicationReconciliationEvent,
    PublicationStatus,
)
from app.services.buffer_pilot_execution_gate import (
    FINAL_EXECUTION_READY,
    LIVE_EXECUTION_LOCKED,
    BufferPilotExecutionEvidence,
    evaluate_buffer_pilot_execution_readiness,
)
from app.services.publication_dispatch_authorization import create_authorization
from app.services.publication_scheduler import request_fingerprint_for
from test_manual_publication_dispatch import _db, _ready_publication


SECRET = "fake-buffer-phase3b-secret-not-real"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def settings(publication=None, **overrides):
    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "publishing_enabled": False,
        "buffer_publishing_enabled": False,
        "buffer_single_pin_pilot_enabled": False,
        "buffer_api_key": SECRET,
        "buffer_api_base": "https://api.buffer.com",
        "buffer_organization_id": "org",
        "buffer_pinterest_channel_id": "channel",
        "buffer_single_pin_pilot_publication_id": publication.id if publication else "",
        "buffer_single_pin_pilot_publication_fingerprint": publication.publication_fingerprint if publication else "",
        "buffer_single_pin_pilot_request_fingerprint": request_fingerprint_for(publication) if publication else "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def live_settings(publication, **overrides):
    values = {
        "publishing_enabled": True,
        "buffer_publishing_enabled": True,
        "buffer_single_pin_pilot_enabled": True,
    }
    values.update(overrides)
    return settings(publication, **values)


def evidence(publication, settings_value=None, **overrides):
    settings_value = settings_value or live_settings(publication)
    values = {
        "publication_id": publication.id,
        "publication_fingerprint": publication.publication_fingerprint,
        "request_fingerprint": request_fingerprint_for(publication),
        "buffer_organization_id": settings_value.buffer_organization_id,
        "buffer_pinterest_channel_id": settings_value.buffer_pinterest_channel_id,
        "board_service_id": publication.pinterest_board_id_snapshot,
        "media_url": publication.media_url_snapshot,
        "observed_at": NOW - timedelta(minutes=1),
        "write_credential_authorized": False,
        "provider_destination_live_verified": False,
        "media_live_fetch_verified": False,
    }
    values.update(overrides)
    return BufferPilotExecutionEvidence(**values)


@pytest.fixture
def case(tmp_path):
    SessionLocal, engine = _db(tmp_path / "phase3b-case.db")
    with SessionLocal() as db:
        publication = _ready_publication(db, dispatch_provider="buffer",
            scopes=["user_accounts:read", "boards:read", "pins:read"],
            scheduled_for=NOW - timedelta(minutes=1),
        )
        yield db, publication
    engine.dispose()


def evaluate(db, publication, evidence_value=None, settings_value=None):
    return evaluate_buffer_pilot_execution_readiness(
        db,
        publication.id,
        evidence=evidence_value,
        settings=settings_value or settings(publication),
        now=NOW,
    )


def assert_locked(result, reason):
    assert result["execution_status"] == LIVE_EXECUTION_LOCKED
    assert result["lock_reason"] == reason


def assert_secret_absent(result):
    text = json.dumps(result, sort_keys=True, default=str)
    assert SECRET not in text
    assert "Bearer " not in text
    assert "credential_length" not in text
    assert "credential_prefix" not in text
    assert "credential_suffix" not in text
    assert "credential_hash" not in text
    assert "Authorization" not in text


def _fresh_persisted_case(tmp_path, *, fingerprint="fresh"):
    SessionLocal, engine = _db(tmp_path / f"{fingerprint}.db")
    setup = SessionLocal()
    try:
        publication = _ready_publication(setup, dispatch_provider="buffer",
            scopes=["user_accounts:read", "boards:read", "pins:read"],
            scheduled_for=NOW - timedelta(minutes=1),
            fingerprint=fingerprint,
        )
        authorization = create_authorization(setup, publication, dispatch_provider="buffer", actor="operator", now=NOW)
        live = live_settings(publication)
        ev = evidence(
            publication,
            live,
            write_credential_authorized=True,
            provider_destination_live_verified=True,
            media_live_fetch_verified=True,
        )
        return SessionLocal, engine, publication.id, authorization.id, publication.board_id, live, ev
    finally:
        setup.close()


def test_explicit_publication_id_required(case):
    db, _publication = case
    result = evaluate_buffer_pilot_execution_readiness(db, "", settings=settings(), now=NOW)
    assert result["static_status"] == "PUBLICATION_ID_REQUIRED"
    assert_locked(result, "STATIC_CERTIFICATION_BLOCKED")
    assert result["publication_id"] is None


def test_phase3a_blocked_propagates_static_status_safely(case):
    db, publication = case
    publication.title_snapshot = None
    db.commit()
    result = evaluate(db, publication, settings_value=live_settings(publication))
    assert result["static_status"] == "INCOMPLETE_SNAPSHOT"
    assert_locked(result, "STATIC_CERTIFICATION_BLOCKED")
    assert result["publication_id"] == publication.id


def test_publication_not_found_preserves_requested_publication_id(case):
    db, _publication = case
    requested_id = "missing-publication"
    result = evaluate_buffer_pilot_execution_readiness(
        db,
        requested_id,
        evidence=None,
        settings=settings(),
        now=NOW,
    )
    assert result["static_status"] == "PUBLICATION_NOT_FOUND"
    assert_locked(result, "STATIC_CERTIFICATION_BLOCKED")
    assert result["publication_id"] == requested_id


def test_current_false_flags_keep_valid_static_candidate_locked(case):
    db, publication = case
    result = evaluate(db, publication, evidence(publication, settings(publication)))
    assert result["static_status"] == "STATIC_CANDIDATE_READY"
    assert result["execution_status"] == LIVE_EXECUTION_LOCKED
    assert result["lock_reason"] == "PUBLISHING_DISABLED"
    assert result["static"]["status"] == "STATIC_CANDIDATE_READY"


def test_no_evidence_is_locked_after_flags_and_authorization_are_valid(case):
    db, publication = case
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(db, publication, settings_value=live_settings(publication))
    assert_locked(result, "EXECUTION_EVIDENCE_REQUIRED")
    assert result["static_status"] == "STATIC_CANDIDATE_READY"


def test_evidence_dataclass_is_frozen_and_defaults_false(case):
    ev = BufferPilotExecutionEvidence("p", "f", "r", "org", "channel", "board", "https://cdn.shopify.com/i.jpg", NOW)
    assert ev.write_credential_authorized is False
    assert ev.provider_destination_live_verified is False
    assert ev.media_live_fetch_verified is False
    with pytest.raises(FrozenInstanceError):
        ev.write_credential_authorized = True


@pytest.mark.parametrize(
    "field,value",
    [
        ("publication_id", "other"),
        ("publication_fingerprint", "f" * 64),
        ("request_fingerprint", "r" * 64),
        ("buffer_organization_id", "other-org"),
        ("buffer_pinterest_channel_id", "other-channel"),
        ("board_service_id", "other-board"),
        ("media_url", "https://cdn.shopify.com/other.jpg"),
    ],
)
def test_evidence_identity_mismatch_blocks(case, field, value):
    db, publication = case
    live = live_settings(publication)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(db, publication, evidence(publication, live, **{field: value}), live)
    assert_locked(result, "EXECUTION_EVIDENCE_MISMATCH")


@pytest.mark.parametrize("observed_at", [None, NOW + timedelta(seconds=1), NOW - timedelta(minutes=16)])
def test_missing_future_or_stale_evidence_timestamp_blocks(case, observed_at):
    db, publication = case
    live = live_settings(publication)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(db, publication, evidence(publication, live, observed_at=observed_at), live)
    assert_locked(result, "EXECUTION_EVIDENCE_STALE")


@pytest.mark.parametrize("observed_at", ["2026-09-06T12:00:00Z", 123, object()])
def test_malformed_evidence_timestamp_is_bounded_invalid(case, observed_at):
    db, publication = case
    live = live_settings(publication)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(db, publication, evidence(publication, live, observed_at=observed_at), live)
    assert_locked(result, "EXECUTION_EVIDENCE_INVALID")
    assert result["evidence"]["observed_at"] == "<invalid>"
    assert_secret_absent(result)


def test_fresh_evidence_reaches_external_boolean_gates(case):
    db, publication = case
    live = live_settings(publication)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(db, publication, evidence(publication, live), live)
    assert_locked(result, "WRITE_CREDENTIAL_NOT_AUTHORIZED")


def test_buffer_api_key_presence_never_implies_write_authorization(case):
    db, publication = case
    live = live_settings(publication, buffer_api_key=SECRET)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(db, publication, evidence(publication, live), live)
    assert_locked(result, "WRITE_CREDENTIAL_NOT_AUTHORIZED")
    assert_secret_absent(result)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({}, "WRITE_CREDENTIAL_NOT_AUTHORIZED"),
        ({"write_credential_authorized": True}, "PROVIDER_DESTINATION_NOT_VERIFIED"),
        (
            {"write_credential_authorized": True, "provider_destination_live_verified": True},
            "MEDIA_NOT_LIVE_VERIFIED",
        ),
    ],
)
def test_external_evidence_booleans_are_independent(case, overrides, reason):
    db, publication = case
    live = live_settings(publication)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(db, publication, evidence(publication, live, **overrides), live)
    assert_locked(result, reason)


@pytest.mark.parametrize(
    "flag,reason",
    [
        ("publishing_enabled", "PUBLISHING_DISABLED"),
        ("buffer_publishing_enabled", "BUFFER_PUBLISHING_DISABLED"),
        ("buffer_single_pin_pilot_enabled", "BUFFER_PILOT_DISABLED"),
    ],
)
def test_each_protected_gate_false_blocks(case, flag, reason):
    db, publication = case
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    live = live_settings(publication, **{flag: False})
    result = evaluate(db, publication, evidence(publication, live), live)
    assert_locked(result, reason)


def test_buffer_pilot_binding_mismatch_blocks(case):
    db, publication = case
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    live = live_settings(publication, buffer_single_pin_pilot_request_fingerprint="wrong")
    result = evaluate(db, publication, evidence(publication, live), live)
    assert_locked(result, "BUFFER_PILOT_BINDING_MISMATCH")


def test_future_scheduled_candidate_is_not_due(case):
    db, publication = case
    publication.scheduled_for = NOW + timedelta(hours=1)
    db.commit()
    live = live_settings(publication)
    result = evaluate(db, publication, evidence(publication, live), live)
    assert result["static_status"] == "STATIC_CANDIDATE_READY"
    assert_locked(result, "NOT_DUE")


def test_due_candidate_passes_due_check_and_waits_for_authorization(case):
    db, publication = case
    live = live_settings(publication)
    result = evaluate(db, publication, evidence(publication, live), live)
    assert_locked(result, "AUTHORIZATION_REQUIRED")


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing", "AUTHORIZATION_REQUIRED"),
        ("expired-time", "AUTHORIZATION_EXPIRED"),
        ("expired-status", "AUTHORIZATION_EXPIRED"),
        ("revoked", "AUTHORIZATION_REVOKED"),
        ("consumed", "AUTHORIZATION_CONSUMED"),
        ("mismatch", "AUTHORIZATION_MISMATCH"),
    ],
)
def test_authorization_states_are_bounded(case, mutation, reason):
    db, publication = case
    if mutation != "missing":
        auth = create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
        if mutation == "expired-time":
            auth.expires_at = NOW - timedelta(seconds=1)
        elif mutation == "expired-status":
            auth.status = "EXPIRED"
        elif mutation == "revoked":
            auth.status = "REVOKED"
        elif mutation == "consumed":
            auth.status = "CONSUMED"
        elif mutation == "mismatch":
            auth.publication_fingerprint = "x" * 64
        db.commit()
    live = live_settings(publication)
    result = evaluate(db, publication, evidence(publication, live), live)
    assert_locked(result, reason)


@pytest.mark.parametrize(
    "mutation,static_status",
    [
        ("prior-attempt", "PRIOR_ATTEMPT_EXISTS"),
        ("reconciliation", "RECONCILIATION_HISTORY_EXISTS"),
        ("known-pin", "KNOWN_PIN_EXISTS"),
        ("duplicate", "DUPLICATE_PUBLICATION"),
        ("unknown", "INVALID_PUBLICATION_STATE"),
    ],
)
def test_retry_or_ambiguity_state_blocks_before_execution(case, mutation, static_status):
    db, publication = case
    if mutation == "prior-attempt":
        db.add(PublicationAttempt(publication_id=publication.id, attempt_number=1, status="FAILED"))
    elif mutation == "reconciliation":
        db.add(
            PublicationReconciliationEvent(
                publication_id=publication.id,
                actor="operator",
                action="PROVIDER_PIN_CONFIRMED",
                previous_status="PUBLISH_UNKNOWN",
                new_status="PUBLISHED",
                provider="buffer",
                provider_pin_id="123",
            )
        )
    elif mutation == "known-pin":
        publication.pinterest_pin_id = "123"
    elif mutation == "duplicate":
        db.add(
            PinPublication(
                id="other",
                draft_id=publication.draft_id,
                creative_id=publication.creative_id,
                publication_fingerprint="q" * 64,
                status=PublicationStatus.PUBLISHED,
                pinterest_board_id_snapshot=publication.pinterest_board_id_snapshot,
                utm_url=publication.utm_url,
                creative_fingerprint=publication.creative_fingerprint,
                text_fingerprint=publication.text_fingerprint,
            )
        )
    elif mutation == "unknown":
        publication.status = PublicationStatus.PUBLISH_UNKNOWN
    db.commit()
    live = live_settings(publication)
    result = evaluate(db, publication, evidence(publication, live), live)
    assert result["static_status"] == static_status
    assert_locked(result, "STATIC_CERTIFICATION_BLOCKED")


def test_dirty_unrelated_orm_object_does_not_autoflush_or_persist():
    SessionLocal, engine = _db()
    db = SessionLocal()
    audit = SessionLocal()
    try:
        publication = _ready_publication(db, dispatch_provider="buffer", scopes=["user_accounts:read", "boards:read", "pins:read"])
        source = db.get(ProductImage, publication.source_image_id)
        source.alt_text = "dirty pending change"
        live = live_settings(publication)
        flushes = []
        event.listen(db, "before_flush", lambda *args: flushes.append("flush"))
        first = evaluate(db, publication, evidence(publication, live), live)
        second = evaluate(db, publication, evidence(publication, live), live)
        assert first == second
        assert first["lock_reason"] == "FRESH_PERSISTED_READ_UNAVAILABLE"
        assert flushes == []
        assert audit.get(ProductImage, publication.source_image_id).alt_text is None
    finally:
        db.close()
        audit.close()
        engine.dispose()


def test_sqlite_memory_active_transaction_fails_closed_without_rollback():
    SessionLocal, engine = _db()
    db = SessionLocal()
    audit = SessionLocal()
    try:
        publication = _ready_publication(db, dispatch_provider="buffer",
            scopes=["user_accounts:read", "boards:read", "pins:read"],
            scheduled_for=NOW - timedelta(minutes=1),
        )
        create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
        source = db.get(ProductImage, publication.source_image_id)
        source.alt_text = "flushed but uncommitted"
        db.flush()
        live = live_settings(publication)

        result = evaluate_buffer_pilot_execution_readiness(
            db,
            publication.id,
            evidence=evidence(
                publication,
                live,
                write_credential_authorized=True,
                provider_destination_live_verified=True,
                media_live_fetch_verified=True,
            ),
            settings=live,
            now=NOW,
        )

        assert_locked(result, "FRESH_PERSISTED_READ_UNAVAILABLE")
        assert db.get(ProductImage, publication.source_image_id).alt_text == "flushed but uncommitted"
        db.commit()
        assert audit.get(ProductImage, publication.source_image_id).alt_text == "flushed but uncommitted"
    finally:
        db.close()
        audit.close()
        engine.dispose()


def test_connection_bound_session_fails_closed_without_taking_transaction_control(tmp_path):
    SessionLocal, engine = _db(tmp_path / "connection-bound.db")
    with SessionLocal() as setup:
        publication = _ready_publication(setup, dispatch_provider="buffer",
            scopes=["user_accounts:read", "boards:read", "pins:read"],
            scheduled_for=NOW - timedelta(minutes=1),
            fingerprint="connection-bound",
        )
        create_authorization(setup, publication, dispatch_provider="buffer", actor="operator", now=NOW)
        publication_id = publication.id
        source_id = publication.source_image_id
        live = live_settings(publication)
        ev = evidence(
            publication,
            live,
            write_credential_authorized=True,
            provider_destination_live_verified=True,
            media_live_fetch_verified=True,
        )

    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, expire_on_commit=False)
    try:
        result = evaluate_buffer_pilot_execution_readiness(
            db,
            publication_id,
            evidence=ev,
            settings=live,
            now=NOW,
        )
        assert_locked(result, "FRESH_PERSISTED_READ_UNAVAILABLE")
        assert transaction.is_active is True
        assert connection.closed is False

        db.get(ProductImage, source_id).alt_text = "caller still owns transaction"
        db.flush()
        assert transaction.is_active is True
        transaction.rollback()
        assert connection.closed is False

        with SessionLocal() as audit:
            assert audit.get(ProductImage, source_id).alt_text is None
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()


def test_evaluator_never_constructs_gateway_http_or_mutates(tmp_path, monkeypatch):
    SessionLocal, engine = _db(tmp_path / "no-provider.db")
    db = SessionLocal()
    publication = _ready_publication(db, dispatch_provider="buffer",
        scopes=["user_accounts:read", "boards:read", "pins:read"],
        scheduled_for=NOW - timedelta(minutes=1),
        fingerprint="no-provider",
    )
    live = live_settings(publication)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("provider access is forbidden")

    monkeypatch.setattr("httpx.AsyncClient", fail)
    monkeypatch.setattr("app.integrations.buffer.gateway.BufferGateway", fail)
    monkeypatch.setattr("app.integrations.pinterest.gateway.PinterestV5Gateway", fail)
    monkeypatch.setattr("app.services.buffer_pinterest_adapter.verify_destination", fail)
    before = {
        "publication": tuple(db.execute(select(PinPublication.id, PinPublication.status, PinPublication.error_code)).all()),
        "attempts": db.query(PublicationAttempt).count(),
        "auth": db.query(PublicationDispatchAuthorization).count(),
        "reconciliation": db.query(PublicationReconciliationEvent).count(),
    }
    flushes = []
    event.listen(db, "before_flush", lambda *args: flushes.append("flush"))
    result = evaluate(db, publication, evidence(publication, live, write_credential_authorized=True, provider_destination_live_verified=True, media_live_fetch_verified=True), live)
    after = {
        "publication": tuple(db.execute(select(PinPublication.id, PinPublication.status, PinPublication.error_code)).all()),
        "attempts": db.query(PublicationAttempt).count(),
        "auth": db.query(PublicationDispatchAuthorization).count(),
        "reconciliation": db.query(PublicationReconciliationEvent).count(),
    }
    assert result["execution_status"] == FINAL_EXECUTION_READY
    assert calls == []
    assert flushes == []
    assert before == after
    db.close()
    engine.dispose()


def test_evaluation_fresh_reads_publication_safety_state_drift(tmp_path):
    SessionLocal, engine, publication_id, _authorization_id, _board_id, live, ev = _fresh_persisted_case(
        tmp_path,
        fingerprint="drift-state",
    )
    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        cached_publication = session_a.get(PinPublication, publication_id)
        assert cached_publication.status == PublicationStatus.SCHEDULED
        ready = evaluate_buffer_pilot_execution_readiness(
            session_a,
            publication_id,
            evidence=ev,
            settings=live,
            now=NOW,
        )
        assert ready["execution_status"] == FINAL_EXECUTION_READY

        session_b.get(PinPublication, publication_id).status = PublicationStatus.PUBLISH_UNKNOWN
        session_b.commit()

        result = evaluate_buffer_pilot_execution_readiness(
            session_a,
            publication_id,
            evidence=ev,
            settings=live,
            now=NOW,
        )
        assert result["execution_status"] == LIVE_EXECUTION_LOCKED
        assert result["static_status"] == "INVALID_PUBLICATION_STATE"
        assert result["lock_reason"] == "STATIC_CERTIFICATION_BLOCKED"
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()


def test_evaluation_fresh_reads_destination_eligibility_drift(tmp_path):
    SessionLocal, engine, publication_id, _authorization_id, board_id, live, ev = _fresh_persisted_case(
        tmp_path,
        fingerprint="drift-board",
    )
    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        cached_board = session_a.get(Board, board_id)
        assert cached_board.active is True
        ready = evaluate_buffer_pilot_execution_readiness(
            session_a,
            publication_id,
            evidence=ev,
            settings=live,
            now=NOW,
        )
        assert ready["execution_status"] == FINAL_EXECUTION_READY

        session_b.get(Board, board_id).active = False
        session_b.commit()

        result = evaluate_buffer_pilot_execution_readiness(
            session_a,
            publication_id,
            evidence=ev,
            settings=live,
            now=NOW,
        )
        assert result["execution_status"] == LIVE_EXECUTION_LOCKED
        assert result["static_status"] == "DESTINATION_INVALID"
        assert result["lock_reason"] == "STATIC_CERTIFICATION_BLOCKED"
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()


def test_evaluation_fresh_reads_authorization_status_drift(tmp_path):
    SessionLocal, engine, publication_id, authorization_id, _board_id, live, ev = _fresh_persisted_case(
        tmp_path,
        fingerprint="drift-auth",
    )
    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        cached_authorization = session_a.get(PublicationDispatchAuthorization, authorization_id)
        assert cached_authorization.status == "ACTIVE"
        ready = evaluate_buffer_pilot_execution_readiness(
            session_a,
            publication_id,
            evidence=ev,
            settings=live,
            now=NOW,
        )
        assert ready["execution_status"] == FINAL_EXECUTION_READY

        session_b.get(PublicationDispatchAuthorization, authorization_id).status = "REVOKED"
        session_b.commit()

        result = evaluate_buffer_pilot_execution_readiness(
            session_a,
            publication_id,
            evidence=ev,
            settings=live,
            now=NOW,
        )
        assert result["execution_status"] == LIVE_EXECUTION_LOCKED
        assert result["static_status"] == "STATIC_CANDIDATE_READY"
        assert result["lock_reason"] == "AUTHORIZATION_REVOKED"
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()


def test_hypothetical_all_valid_evidence_produces_final_execution_ready(case):
    db, publication = case
    live = live_settings(publication)
    create_authorization(db, publication, dispatch_provider="buffer", actor="operator", now=NOW)
    result = evaluate(
        db,
        publication,
        evidence(publication, live, write_credential_authorized=True, provider_destination_live_verified=True, media_live_fetch_verified=True),
        live,
    )
    assert result["static_status"] == "STATIC_CANDIDATE_READY"
    assert result["execution_status"] == FINAL_EXECUTION_READY
    assert result["lock_reason"] is None
    assert result["publication_fingerprint"] == publication.publication_fingerprint
    assert result["request_fingerprint"] == request_fingerprint_for(publication)


def test_production_like_defaults_never_produce_final_execution_ready(case):
    db, publication = case
    production_like = settings(publication, buffer_api_key=SECRET)
    result = evaluate(
        db,
        publication,
        evidence(publication, production_like, write_credential_authorized=True, provider_destination_live_verified=True, media_live_fetch_verified=True),
        production_like,
    )
    assert result["execution_status"] == LIVE_EXECUTION_LOCKED
    assert result["lock_reason"] == "PUBLISHING_DISABLED"
    assert_secret_absent(result)
