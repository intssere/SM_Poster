from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import (
    CreativeTemplate,
    PinApproval,
    PinCreative,
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    ProductImage,
    PublicationAttempt,
    PublicationDispatchAuthorization,
    PublicationStatus,
)
from app.services.manual_publication_dispatch import (
    ManualDispatchError,
    atomic_authorized_claim,
    dispatch_publication,
    validate_post_claim,
)
from app.services.publication_dispatch_authorization import create_authorization
from app.services.publication_scheduler import request_fingerprint_for


def _db(path=None):
    engine = create_engine("sqlite+pysqlite:///:memory:" if path is None else f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _ready_publication(db, *, scopes=None, scheduled_for=None, fingerprint="p"):
    now = datetime.now(timezone.utc)
    template = CreativeTemplate(id=f"template-{fingerprint}", key="product_classification", version=1, name="Template")
    source = ProductImage(id=f"source-{fingerprint}", product_id=f"product-{fingerprint}", source_url="https://cdn.shopify.com/source.jpg", width=1000, height=1500)
    creative = PinCreative(
        id=f"creative-{fingerprint}",
        draft_id=f"draft-{fingerprint}",
        template_id=template.id,
        source_image_id=source.id,
        rendered_url=f"https://cdn.shopify.com/{fingerprint}/creative.jpg",
        creative_fingerprint=fingerprint * 64,
        width=1000,
        height=1500,
        render_status="COMPLETE",
    )
    approval = PinApproval(
        id=f"approval-{fingerprint}",
        draft_id=creative.draft_id,
        revision_id=f"revision-{fingerprint}",
        creative_id=creative.id,
        decision="APPROVED",
        decided_by="admin@example.test",
    )
    connection = PinterestConnection(
        id=f"connection-{fingerprint}",
        external_user_id=f"user-{fingerprint}",
        access_token_ciphertext=f"enc-access-{fingerprint}",
        refresh_token_ciphertext=f"enc-refresh-{fingerprint}",
        granted_scopes=scopes or ["user_accounts:read", "boards:read", "pins:read", "pins:write"],
        status="CONNECTED",
    )
    board = PinterestBoard(id=f"board-{fingerprint}", connection_id=connection.id, external_board_id=f"external-board-{fingerprint}", name="Fragrance", is_active=True, is_eligible=True, routing_label="fragrance")
    publication = PinPublication(
        id=f"publication-{fingerprint}",
        draft_id=creative.draft_id,
        revision_id=approval.revision_id,
        creative_id=creative.id,
        approval_id=approval.id,
        source_image_id=source.id,
        template_id=template.id,
        template_key=template.key,
        template_version=template.version,
        text_fingerprint="t" * 64,
        creative_fingerprint=creative.creative_fingerprint,
        pinterest_connection_id=connection.id,
        pinterest_board_record_id=board.id,
        pinterest_board_id_snapshot=board.external_board_id,
        title_snapshot="Fragrance gift pick",
        description_snapshot="Explore this fragrance gift pick for a polished scent routine.",
        alt_text_snapshot="A verified product creative for a fragrance gift pick.",
        destination_url="https://diamondshelf.us/products/fragrance-pick",
        utm_url="https://diamondshelf.us/products/fragrance-pick?utm_source=pinterest",
        media_url_snapshot=creative.rendered_url,
        publication_fingerprint=fingerprint * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=scheduled_for or (now - timedelta(minutes=1)),
    )
    db.add_all([template, source, creative, approval, connection, board, publication])
    db.commit()
    return publication


def _settings(enabled, publication=None, *, pilot=True, publication_id=None, publication_fingerprint=None, request_fingerprint=None):
    if publication is None:
        return type("S", (), {"publishing_enabled": enabled, "pinterest_single_pin_pilot_enabled": pilot})()
    return type("S", (), {
        "publishing_enabled": enabled,
        "pinterest_single_pin_pilot_enabled": pilot,
        "pinterest_single_pin_pilot_publication_id": publication.id if publication_id is None else publication_id,
        "pinterest_single_pin_pilot_publication_fingerprint": publication.publication_fingerprint if publication_fingerprint is None else publication_fingerprint,
        "pinterest_single_pin_pilot_request_fingerprint": request_fingerprint_for(publication) if request_fingerprint is None else request_fingerprint,
    })()


def test_dispatch_disabled_preserves_active_authorization_and_creates_no_attempt(monkeypatch):
    from app.services import publication_dispatch_authorization as auth_service
    from app.services import pinterest_single_pin_pilot as pilot_service

    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(False, publication))
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(False, publication))
    with pytest.raises(ManualDispatchError, match="PUBLISHING_DISABLED"):
        import asyncio

        asyncio.run(dispatch_publication(db, publication))
    db.expire_all()
    assert db.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
    assert db.get(type(auth), auth.id).status == "ACTIVE"
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0
    engine.dispose()


def test_dispatch_missing_pins_write_preserves_active_authorization_and_creates_no_attempt(monkeypatch):
    from app.services import publication_dispatch_authorization as auth_service
    from app.services import pinterest_single_pin_pilot as pilot_service

    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db, scopes=["user_accounts:read", "boards:read", "pins:read"])
    auth = create_authorization(db, publication, actor="admin@example.test")
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(True, publication))
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(True, publication))
    with pytest.raises(ManualDispatchError, match="PUBLISHING_SCOPE_REQUIRED"):
        import asyncio

        asyncio.run(dispatch_publication(db, publication))
    db.expire_all()
    assert db.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
    assert db.get(type(auth), auth.id).status == "ACTIVE"
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0
    engine.dispose()


def test_dispatch_token_decrypt_failure_happens_before_claim(monkeypatch):
    from app.services import publication_dispatch_authorization as auth_service
    from app.services import pinterest_single_pin_pilot as pilot_service

    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(True, publication))
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(True, publication))
    with pytest.raises(ManualDispatchError, match="TOKEN_DECRYPT_FAILED"):
        import asyncio

        asyncio.run(dispatch_publication(db, publication, decrypt=lambda _: (_ for _ in ()).throw(RuntimeError("secret"))))
    db.expire_all()
    assert db.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
    assert db.get(type(auth), auth.id).status == "ACTIVE"
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0
    engine.dispose()


def test_dispatch_ready_path_claims_and_calls_mock_gateway_once(monkeypatch):
    from app.services import publication_dispatch_authorization as auth_service
    from app.services import pinterest_publisher
    from app.services import pinterest_single_pin_pilot as pilot_service

    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(True, publication))
    monkeypatch.setattr(pinterest_publisher, "get_settings", lambda: _settings(True, publication))
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(True, publication))

    class Gateway:
        calls = 0

        def __init__(self, *args, **kwargs):
            assert kwargs["access_token"] == "plain-token"
            assert kwargs["publishing_enabled"] is True

        async def create_pin(self, payload):
            type(self).calls += 1
            assert payload.link == publication.utm_url
            assert payload.link != publication.destination_url
            return {"id": "pin-123"}

    import asyncio

    result = asyncio.run(dispatch_publication(db, publication, decrypt=lambda _: "plain-token", gateway_factory=Gateway))
    assert result == {"id": "pin-123"}
    db.expire_all()
    assert Gateway.calls == 1
    assert db.get(PinPublication, publication.id).status == PublicationStatus.PUBLISHED
    assert db.get(type(auth), auth.id).status == "CONSUMED"
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id, status="SUCCEEDED").count() == 1
    engine.dispose()


def test_atomic_authorized_claim_consumes_once_and_binds_utm_fingerprint():
    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    original = request_fingerprint_for(publication)
    publication.utm_url = "https://diamondshelf.us/products/fragrance-pick?utm_source=pinterest&utm_campaign=changed"
    changed = request_fingerprint_for(publication)
    assert changed != original
    publication.utm_url = "https://diamondshelf.us/products/fragrance-pick?utm_source=pinterest"
    db.commit()

    attempt = atomic_authorized_claim(db, publication, auth)
    assert attempt is not None
    db.expire_all()
    assert db.get(PinPublication, publication.id).status == PublicationStatus.PUBLISHING
    assert db.get(type(auth), auth.id).status == "CONSUMED"
    assert db.get(type(auth), auth.id).consumed_at is not None
    assert attempt.status == "STARTED"
    assert attempt.request_fingerprint == request_fingerprint_for(db.get(PinPublication, publication.id))
    assert atomic_authorized_claim(db, db.get(PinPublication, publication.id), db.get(type(auth), auth.id)) is None
    engine.dispose()


def test_atomic_authorized_claim_rolls_back_publication_or_authorization_cas_failure():
    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    publication.status = PublicationStatus.APPROVED
    db.commit()
    assert atomic_authorized_claim(db, publication, auth) is None
    db.expire_all()
    assert db.get(PinPublication, publication.id).status == PublicationStatus.APPROVED
    assert db.get(type(auth), auth.id).status == "ACTIVE"
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0

    publication.status = PublicationStatus.SCHEDULED
    auth.status = "REVOKED"
    db.commit()
    assert atomic_authorized_claim(db, publication, auth) is None
    db.expire_all()
    assert db.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0
    engine.dispose()


def test_atomic_authorized_claim_rejects_expired_and_fingerprint_mismatch():
    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    auth.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert atomic_authorized_claim(db, publication, auth) is None
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0

    auth.expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    auth.publication_fingerprint = "x" * 64
    db.commit()
    assert atomic_authorized_claim(db, publication, auth) is None
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0
    engine.dispose()


def test_post_claim_validation_does_not_mutate_lifecycle_state(monkeypatch):
    from app.services import manual_publication_dispatch as dispatch_service

    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    attempt = atomic_authorized_claim(db, publication, auth)
    db.refresh(publication)
    db.refresh(auth)
    db.refresh(attempt)
    observed = {}

    def snapshot_binding(spy_db, spy_publication, spy_authorization, **kwargs):
        observed["publication_status"] = spy_publication.status
        observed["authorization_status"] = spy_authorization.status
        return {
            "valid": True,
            "status": "SNAPSHOT_BOUND",
            "quality": auth.quality_snapshot,
            "duplicate": auth.duplicate_snapshot,
            "manual": auth.readiness_snapshot,
        }

    monkeypatch.setattr(dispatch_service, "validate_authorization_snapshot_binding", snapshot_binding)

    assert validate_post_claim(db, publication, auth, attempt)["valid"] is True
    assert observed["publication_status"] == PublicationStatus.PUBLISHING
    assert observed["authorization_status"] == "CONSUMED"
    assert publication.status == PublicationStatus.PUBLISHING
    assert auth.status == "CONSUMED"
    engine.dispose()


def test_post_claim_validation_exception_fails_known_without_provider_call(monkeypatch):
    from app.services import manual_publication_dispatch as dispatch_service
    from app.services import publication_dispatch_authorization as auth_service
    from app.services import pinterest_publisher
    from app.services import pinterest_single_pin_pilot as pilot_service

    SessionLocal, engine = _db()
    db = SessionLocal()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(True, publication))
    monkeypatch.setattr(pinterest_publisher, "get_settings", lambda: _settings(True, publication))
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(True, publication))
    monkeypatch.setattr(
        dispatch_service,
        "validate_post_claim",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("internal traceback secret")),
    )

    class Gateway:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        async def create_pin(self, payload):
            type(self).calls += 1
            return {"id": "pin-123"}

    import asyncio

    with pytest.raises(ManualDispatchError, match="TASK39_POST_CLAIM_REVALIDATION_FAILED"):
        asyncio.run(dispatch_publication(db, publication, decrypt=lambda _: "plain-token", gateway_factory=Gateway))

    db.expire_all()
    persisted_publication = db.get(PinPublication, publication.id)
    persisted_auth = db.get(type(auth), auth.id)
    attempt = db.query(PublicationAttempt).filter_by(publication_id=publication.id).one()
    assert Gateway.calls == 0
    assert persisted_publication.status == PublicationStatus.PUBLISH_FAILED
    assert persisted_publication.error_code == "TASK39_POST_CLAIM_REVALIDATION_FAILED"
    assert persisted_auth.status == "CONSUMED"
    assert attempt.status == "FAILED"
    assert attempt.error_code == "TASK39_POST_CLAIM_REVALIDATION_FAILED"
    assert attempt.provider_pin_id is None
    assert "secret" not in str(attempt.safe_response_metadata)
    assert "traceback" not in str(attempt.safe_response_metadata).lower()
    engine.dispose()

@pytest.mark.parametrize("mode,expected", [("disabled", "PILOT_DISABLED"), ("wrong_id", "PILOT_PUBLICATION_MISMATCH"), ("wrong_fp", "PILOT_PUBLICATION_FINGERPRINT_MISMATCH"), ("wrong_request", "PILOT_REQUEST_FINGERPRINT_MISMATCH")])
def test_dispatch_pilot_preclaim_failures_are_zero_mutation(monkeypatch, mode, expected):
    from app.services import pinterest_single_pin_pilot as pilot_service
    SessionLocal, engine = _db(); db = SessionLocal(); publication = _ready_publication(db); auth = create_authorization(db, publication, actor="admin@example.test")
    kwargs = {"pilot": mode != "disabled"}
    if mode == "wrong_id": kwargs["publication_id"] = "other"
    if mode == "wrong_fp": kwargs["publication_fingerprint"] = "other"
    if mode == "wrong_request": kwargs["request_fingerprint"] = "other"
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(True, publication, **kwargs))
    decrypt_calls = []; gateway_calls = []
    class Gateway:
        def __init__(self, *args, **kwargs): gateway_calls.append(1)
    import asyncio
    with pytest.raises(ManualDispatchError, match=expected):
        asyncio.run(dispatch_publication(db, publication, decrypt=lambda value: decrypt_calls.append(1) or "token", gateway_factory=Gateway))
    db.expire_all()
    assert decrypt_calls == [] and gateway_calls == []
    assert db.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
    assert db.get(type(auth), auth.id).status == "ACTIVE"
    assert db.query(PublicationAttempt).filter_by(publication_id=publication.id).count() == 0
    engine.dispose()


def test_two_session_stale_authorized_claim_consumes_once(tmp_path):
    db_path = tmp_path / "claim-race.db"
    SessionLocal, engine = _db(db_path)
    setup = SessionLocal()
    publication = _ready_publication(setup)
    auth = create_authorization(setup, publication, actor="admin@example.test")
    setup.close()

    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        pub_a = session_a.get(PinPublication, publication.id)
        auth_a = session_a.get(PublicationDispatchAuthorization, auth.id)
        pub_b = session_b.get(PinPublication, publication.id)
        auth_b = session_b.get(PublicationDispatchAuthorization, auth.id)
        assert pub_a.status == pub_b.status == PublicationStatus.SCHEDULED
        assert auth_a.status == auth_b.status == "ACTIVE"

        first_attempt = atomic_authorized_claim(session_a, pub_a, auth_a)
        assert first_attempt is not None
        second_attempt = atomic_authorized_claim(session_b, pub_b, auth_b)
        assert second_attempt is None

        audit = SessionLocal()
        try:
            persisted_publication = audit.get(PinPublication, publication.id)
            persisted_auth = audit.get(PublicationDispatchAuthorization, auth.id)
            attempts = audit.query(PublicationAttempt).filter_by(publication_id=publication.id).all()
            assert persisted_publication.status == PublicationStatus.PUBLISHING
            assert persisted_auth.status == "CONSUMED"
            assert len(attempts) == 1
            assert attempts[0].status == "STARTED"
        finally:
            audit.close()
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()
