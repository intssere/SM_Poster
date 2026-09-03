def test_publications_router_is_registered():
    import os
    os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    from app.main import app
    from app.db.session import engine
    from app.models.domain import Base
    Base.metadata.create_all(engine)
    assert "/api/publications" in app.openapi()["paths"]

def _client(monkeypatch):
    from app.core import auth
    monkeypatch.setenv("APP_ENV", "development"); monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a"*48); monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret")); monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)

def test_publications_api_anonymous_routes_denied(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/publications").status_code == 401
    assert client.get("/api/publications/missing").status_code == 401
    assert client.post("/api/publications", json={}, headers={"Origin":"http://localhost:5000"}).status_code == 401


def test_publications_api_authenticated_list_and_detail_allowed(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.main import app
    from app.models.domain import Base, PinPublication, PublicationStatus
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            db.add(
                PinPublication(
                    id="authenticated-publication",
                    draft_id="authenticated-draft",
                    creative_id="authenticated-creative",
                    publication_fingerprint="a" * 64,
                    status=PublicationStatus.APPROVED,
                    title_snapshot="Authenticated publication title",
                    description_snapshot="Authenticated publication description",
                    alt_text_snapshot="Authenticated publication alt text",
                    media_url_snapshot="https://cdn.example.test/authenticated.jpg",
                    destination_url="https://diamondshelf.us/products/example",
                    utm_url="https://diamondshelf.us/products/example?utm_source=pinterest",
                )
            )
            db.commit()

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            auth_status = client.get("/api/auth/status")
            assert auth_status.status_code == 200
            assert auth_status.json()["authenticated"] is True

            list_response = client.get("/api/publications")
            assert list_response.status_code == 200
            body = list_response.json()
            assert len(body) == 1
            assert body[0]["id"] == "authenticated-publication"
            assert body[0]["status"] == "APPROVED"
            assert body[0]["title"] == "Authenticated publication title"
            assert body[0]["description"] == "Authenticated publication description"
            assert body[0]["alt_text"] == "Authenticated publication alt text"
            assert body[0]["destination_url"] == "https://diamondshelf.us/products/example"
            assert body[0]["media_url"] == "https://cdn.example.test/authenticated.jpg"
            assert body[0]["live_publishing_enabled"] is False
            assert body[0]["publishing_readiness_reason"] == "PUBLISHING_DISABLED"

            detail_response = client.get("/api/publications/authenticated-publication")
            assert detail_response.status_code == 200
            detail = detail_response.json()
            assert detail["id"] == "authenticated-publication"
            assert detail["status"] == "APPROVED"
            assert detail["title"] == "Authenticated publication title"
            assert detail["description"] == "Authenticated publication description"
            assert detail["alt_text"] == "Authenticated publication alt text"
            assert detail["destination_url"] == "https://diamondshelf.us/products/example"
            assert detail["media_url"] == "https://cdn.example.test/authenticated.jpg"
            assert detail["live_publishing_enabled"] is False
            assert detail["publishing_readiness_reason"] == "PUBLISHING_DISABLED"
            assert detail["attempts"] == []

            serialized = repr(body) + repr(detail)
            assert "access_token" not in serialized
            assert "refresh_token" not in serialized
            assert "client_secret" not in serialized
            assert "Authorization" not in serialized
            assert "Bearer" not in serialized

        with TestingSessionLocal() as db:
            from app.models.domain import PublicationAttempt

            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_ready_is_reported_without_dispatch(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        PinApproval,
        PinCreative,
        PinPublication,
        PinterestBoard,
        PinterestConnection,
        PublicationAttempt,
        PublicationStatus,
    )
    from app.services import pinterest_oauth, publication_scheduler
    from app.services.pinterest_publisher import (
        media_publishable,
        preflight_publish_readiness,
        publication_readiness,
    )
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is True

    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("readiness representation must not claim publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("readiness representation must not decrypt tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("readiness representation must not construct a gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("readiness representation must not call provider create_pin")

    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    granted_scopes = ["user_accounts:read", "boards:read", "pins:read", "pins:write"]
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            connection = PinterestConnection(
                id="ready-read-connection",
                external_user_id="ready-read-user",
                granted_scopes=granted_scopes,
                access_token_ciphertext="READY_READ_ACCESS_CIPHERTEXT_DO_NOT_USE",
                refresh_token_ciphertext="READY_READ_REFRESH_CIPHERTEXT_DO_NOT_USE",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="ready-read-board",
                connection_id=connection.id,
                external_board_id="ready-read-external-board",
                name="Ready Read Board",
                is_active=True,
                is_eligible=True,
            )
            creative = PinCreative(
                id="ready-read-creative",
                draft_id="ready-read-draft",
                template_id="ready-read-template",
                source_image_id="ready-read-source-image",
                rendered_url="https://cdn.example.test/ready-read-rendered.jpg",
                creative_fingerprint="y" * 64,
                render_status="RENDERED",
            )
            approval = PinApproval(
                id="ready-read-approval",
                draft_id="ready-read-draft",
                revision_id=None,
                creative_id=creative.id,
                approved_version_id="original",
                decision="APPROVED",
                decided_by="ready_read_api_test",
            )
            publication = PinPublication(
                id="ready-read-publication",
                draft_id="ready-read-draft",
                revision_id=None,
                creative_id=creative.id,
                approval_id=approval.id,
                source_image_id=creative.source_image_id,
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=board.id,
                pinterest_board_id_snapshot=board.external_board_id,
                pinterest_board_id=board.external_board_id,
                publication_fingerprint="j" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Ready read title",
                description_snapshot="Ready read description",
                alt_text_snapshot="Ready read alt",
                destination_url="https://diamondshelf.us/products/ready-read",
                utm_url="https://diamondshelf.us/products/ready-read?utm_source=pinterest",
                media_url_snapshot="https://cdn.example.test/ready-read-rendered.jpg",
            )
            db.add_all([connection, board, creative, approval, publication])
            db.commit()
            db.refresh(publication)

            assert publication.status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            assert connection.status == "CONNECTED"
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == connection.id
            assert board.is_active is True
            assert board.is_eligible is True
            assert board.external_board_id == publication.pinterest_board_id_snapshot
            assert approval.decision == "APPROVED"
            assert approval.draft_id == publication.draft_id
            assert approval.revision_id == publication.revision_id
            assert approval.creative_id == publication.creative_id
            assert creative.draft_id == publication.draft_id
            assert creative.source_image_id == publication.source_image_id
            assert media_publishable(publication.media_url_snapshot) is True
            assert db.query(PublicationAttempt).count() == 0
            assert publication_readiness(db, publication) == "READY"
            ready, reason = preflight_publish_readiness(db, publication)
            assert ready is True
            assert reason is None
            assert db.query(PublicationAttempt).count() == 0

            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at
            initial_publication_fingerprint = publication.publication_fingerprint
            initial_approval_decision = approval.decision
            initial_approval_draft_id = approval.draft_id
            initial_approval_revision_id = approval.revision_id
            initial_approval_creative_id = approval.creative_id
            initial_creative_draft_id = creative.draft_id
            initial_creative_source_image_id = creative.source_image_id

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            detail_response = client.get("/api/publications/ready-read-publication")
            list_response = client.get("/api/publications")

        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["id"] == "ready-read-publication"
        assert detail["status"] == "SCHEDULED"
        assert detail["live_publishing_enabled"] is True
        assert detail["publishing_readiness_reason"] == "READY"
        assert detail["pinterest_connection_id"] == "ready-read-connection"
        assert detail["pinterest_board_record_id"] == "ready-read-board"
        assert detail["pinterest_board_id"] == "ready-read-external-board"
        assert detail["creative_id"] == "ready-read-creative"
        assert detail["approval_id"] == "ready-read-approval"
        assert detail["title"] == "Ready read title"
        assert detail["description"] == "Ready read description"
        assert detail["media_url"] == "https://cdn.example.test/ready-read-rendered.jpg"
        assert detail["attempts"] == []
        assert detail["pinterest_pin_id"] is None
        assert detail["published_at"] is None
        assert detail["error_code"] is None

        assert list_response.status_code == 200
        matches = [row for row in list_response.json() if row["id"] == "ready-read-publication"]
        assert len(matches) == 1
        listed = matches[0]
        assert listed["live_publishing_enabled"] is True
        assert listed["publishing_readiness_reason"] == "READY"
        assert listed["status"] == "SCHEDULED"
        assert listed["pinterest_connection_id"] == "ready-read-connection"
        assert listed["pinterest_board_record_id"] == "ready-read-board"
        assert listed["pinterest_board_id"] == "ready-read-external-board"
        assert listed["creative_id"] == "ready-read-creative"
        assert listed["approval_id"] == "ready-read-approval"

        serialized = detail_response.text + list_response.text
        assert "READY_READ_ACCESS_CIPHERTEXT_DO_NOT_USE" not in serialized
        assert "READY_READ_REFRESH_CIPHERTEXT_DO_NOT_USE" not in serialized
        assert "access_token" not in serialized
        assert "refresh_token" not in serialized
        assert "Authorization" not in serialized
        assert "Bearer" not in serialized
        assert "client_secret" not in serialized
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "ready-read-publication")
            approval = db.get(PinApproval, "ready-read-approval")
            creative = db.get(PinCreative, "ready-read-creative")
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert publication.publication_fingerprint == initial_publication_fingerprint
            assert approval.decision == initial_approval_decision == "APPROVED"
            assert approval.draft_id == initial_approval_draft_id == publication.draft_id
            assert approval.revision_id == initial_approval_revision_id == publication.revision_id
            assert approval.creative_id == initial_approval_creative_id == publication.creative_id
            assert creative.draft_id == initial_creative_draft_id == publication.draft_id
            assert creative.source_image_id == initial_creative_source_image_id == publication.source_image_id
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_create_rejects_server_owned_fields_without_side_effects(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api.routes import publications as publications_route
    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.main import app
    from app.models.domain import Base, PinPublication, PublicationAttempt
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    snapshot_call_count = 0

    def forbidden_create_snapshot(*args, **kwargs):
        nonlocal snapshot_call_count
        snapshot_call_count += 1
        raise AssertionError("create_snapshot must not run for schema-invalid publication requests")

    monkeypatch.setattr(
        publications_route.PublicationIdentityService,
        "create_snapshot",
        forbidden_create_snapshot,
    )

    forbidden_fields = {
        "title": "CLIENT MUST NOT SET TITLE",
        "description": "CLIENT MUST NOT SET DESCRIPTION",
        "alt_text": "CLIENT MUST NOT SET ALT",
        "media_url": "https://attacker.example/client.jpg",
        "destination_url": "https://attacker.example/",
        "utm_url": "https://attacker.example/?utm_source=client",
        "status": "PUBLISHED",
        "revision_id": "client-revision",
        "creative_id": "client-creative",
        "source_image_id": "client-image",
        "publication_fingerprint": "f" * 64,
        "fingerprint": "client-fingerprint",
    }
    base_payload = {
        "approval_id": "approval-does-not-matter",
        "pinterest_connection_id": "connection-does-not-matter",
        "pinterest_board_id": "board-does-not-matter",
    }
    status_codes = {}
    error_types = {}
    error_locs = {}

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            assert db.query(PinPublication).count() == 0
            assert db.query(PublicationAttempt).count() == 0

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200

            for field, value in forbidden_fields.items():
                payload = dict(base_payload)
                payload[field] = value
                response = client.post(
                    "/api/publications",
                    json=payload,
                    headers={"Origin": "http://localhost:5000"},
                )
                status_codes[field] = response.status_code
                assert response.status_code == 422
                detail = response.json()["detail"]
                matching_errors = [
                    error
                    for error in detail
                    if error.get("type") == "extra_forbidden"
                    and error.get("loc", [])[-1] == field
                ]
                assert matching_errors, detail
                error_types[field] = matching_errors[0]["type"]
                error_locs[field] = matching_errors[0]["loc"]
                with TestingSessionLocal() as db:
                    assert db.query(PinPublication).count() == 0
                    assert db.query(PublicationAttempt).count() == 0

        with TestingSessionLocal() as db:
            assert db.query(PinPublication).count() == 0
            assert db.query(PublicationAttempt).count() == 0
        assert snapshot_call_count == 0
        assert set(status_codes) == set(forbidden_fields)
        assert all(code == 422 for code in status_codes.values())
        assert set(error_types.values()) == {"extra_forbidden"}
        assert all(loc[-1] == field for field, loc in error_locs.items())
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_create_origin_guard_blocks_missing_and_wrong_origin(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api.routes import publications as publications_route
    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.main import app
    from app.models.domain import Base, PinPublication, PublicationAttempt
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    snapshot_call_count = 0

    def forbidden_create_snapshot(*args, **kwargs):
        nonlocal snapshot_call_count
        snapshot_call_count += 1
        raise AssertionError("create_snapshot must not run before origin/destination validation")

    monkeypatch.setattr(
        publications_route.PublicationIdentityService,
        "create_snapshot",
        forbidden_create_snapshot,
    )

    payload = {
        "approval_id": "origin-test-approval",
        "pinterest_connection_id": "origin-test-connection",
        "pinterest_board_id": "origin-test-board",
    }

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            assert db.query(PinPublication).count() == 0
            assert db.query(PublicationAttempt).count() == 0

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            auth_status = client.get("/api/auth/status")
            assert auth_status.status_code == 200
            assert auth_status.json()["authenticated"] is True

            missing_origin = client.post("/api/publications", json=payload)
            assert missing_origin.status_code == 403
            assert missing_origin.json() == {"detail": "Origin header is required."}
            with TestingSessionLocal() as db:
                assert db.query(PinPublication).count() == 0
                assert db.query(PublicationAttempt).count() == 0

            wrong_origin = client.post(
                "/api/publications",
                json=payload,
                headers={"Origin": "https://evil.example"},
            )
            assert wrong_origin.status_code == 403
            assert wrong_origin.json() == {"detail": "Origin is not allowed."}
            with TestingSessionLocal() as db:
                assert db.query(PinPublication).count() == 0
                assert db.query(PublicationAttempt).count() == 0

            allowed_origin = client.post(
                "/api/publications",
                json=payload,
                headers={"Origin": "http://localhost:5000"},
            )
            assert allowed_origin.status_code == 422
            assert allowed_origin.json()["detail"] == "Invalid Pinterest destination"
            with TestingSessionLocal() as db:
                assert db.query(PinPublication).count() == 0
                assert db.query(PublicationAttempt).count() == 0

        with TestingSessionLocal() as db:
            assert db.query(PinPublication).count() == 0
            assert db.query(PublicationAttempt).count() == 0
        assert snapshot_call_count == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_create_derives_exact_approved_snapshot_server_side(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        ContentAngle,
        ContentRevision,
        CreativeTemplate,
        DraftStatus,
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
        Store,
    )
    from app.services.fingerprints import publication_identity_fingerprint
    from app.services import pinterest_oauth
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    get_settings.cache_clear()

    token_decrypt_call_count = 0
    provider_call_count = 0

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("publication creation must not decrypt Pinterest tokens")

    async def forbidden_create_pin(*args, **kwargs):
        nonlocal provider_call_count
        provider_call_count += 1
        raise AssertionError("publication creation must not call the Pinterest gateway")

    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module.PinterestV5Gateway, "create_pin", forbidden_create_pin)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            assert db.query(PinPublication).count() == 0
            assert db.query(PublicationAttempt).count() == 0

            store = Store(id="store-create-test", name="Diamond Shelf", shop_domain="diamondshelf.test")
            product = Product(
                id="product-create-test",
                store_id=store.id,
                shopify_product_id="gid://shopify/Product/create-success",
                handle="approved-product",
                title="Approved Product",
                vendor="Approved Brand",
                product_url="https://diamondshelf.us/products/approved-product",
            )
            product_image = ProductImage(
                id="image-create-test",
                product_id=product.id,
                source_url="https://cdn.shopify.com/s/files/approved-product-source.jpg",
                alt_text="Authentic approved product image",
                width=1200,
                height=1500,
                is_primary=True,
                editorial_eligible=True,
            )
            angle = ContentAngle(
                id="angle-create-test",
                key="approved_create_test",
                name="Approved Create Test",
            )
            concept = PinConcept(
                id="concept-create-test",
                store_id=store.id,
                product_id=product.id,
                content_angle_id=angle.id,
                fingerprint="c" * 64,
            )
            draft = PinDraft(
                id="draft-create-test",
                concept_id=concept.id,
                version=1,
                title="OLD DRAFT TITLE - MUST NOT BE SNAPSHOTTED",
                description="OLD DRAFT DESCRIPTION - MUST NOT BE SNAPSHOTTED",
                alt_text="OLD DRAFT ALT - MUST NOT BE SNAPSHOTTED",
                destination_url="https://diamondshelf.us/products/old-draft",
                utm_url="https://diamondshelf.us/products/old-draft?utm_source=pinterest",
                text_fingerprint="d" * 64,
                status=DraftStatus.READY_FOR_REVIEW,
            )
            template = CreativeTemplate(
                id="template-create-test",
                key="approved-template",
                version=7,
                name="Approved Template",
                renderer="satori",
            )
            creative = PinCreative(
                id="creative-create-test",
                draft_id=draft.id,
                template_id=template.id,
                source_image_id=product_image.id,
                rendered_url="https://cdn.example.test/approved-rendered-pin.jpg",
                creative_fingerprint="e" * 64,
                render_status="RENDERED",
            )
            revision = ContentRevision(
                id="revision-create-test",
                draft_id=draft.id,
                version=2,
                revision_kind="COPY",
                status="REVIEW",
                headline="Approved revision headline",
                title="APPROVED REVISION TITLE",
                description="APPROVED REVISION DESCRIPTION",
                alt_text="APPROVED REVISION ALT",
                cta="Shop now",
                content_angle="Approved Create Test",
                content_angle_key="approved_create_test",
                creative_template="Approved Template",
                creative_template_key="approved-template",
                destination_url="https://diamondshelf.us/products/approved-product",
                utm_url="https://diamondshelf.us/products/approved-product?utm_source=pinterest&utm_medium=organic",
                keywords=[],
                facts_used={},
                warnings=[],
                missing_facts=[],
                unsupported_claims=[],
                provenance={"source": "test_fixture"},
                text_fingerprint="r" * 64,
                creative_fingerprint=creative.creative_fingerprint,
                creative_id=creative.id,
                source_image_id=product_image.id,
                provider_mode="disabled",
                generation_mode="deterministic_fixture",
                reason="publication_api_success_test",
            )
            approval = PinApproval(
                id="approval-create-test",
                draft_id=draft.id,
                revision_id=revision.id,
                creative_id=creative.id,
                approved_version_id=revision.id,
                decision="APPROVED",
                decided_by="publication_api_success_test",
            )
            connection = PinterestConnection(
                id="connection-create-test",
                external_user_id="pinterest-user-create-test",
                granted_scopes=["user_accounts:read", "boards:read", "pins:read"],
                access_token_ciphertext="TEST_ACCESS_CIPHERTEXT_DO_NOT_SERIALIZE",
                refresh_token_ciphertext="TEST_REFRESH_CIPHERTEXT_DO_NOT_SERIALIZE",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="board-create-test",
                connection_id=connection.id,
                external_board_id="external-approved-board-123",
                name="Approved Test Board",
                is_active=True,
                is_eligible=True,
            )
            db.add_all(
                [
                    store,
                    product,
                    product_image,
                    angle,
                    concept,
                    draft,
                    template,
                    creative,
                    revision,
                    approval,
                    connection,
                    board,
                ]
            )
            db.commit()

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            auth_status = client.get("/api/auth/status")
            assert auth_status.status_code == 200
            assert auth_status.json()["authenticated"] is True

            response = client.post(
                "/api/publications",
                headers={"Origin": "http://localhost:5000"},
                json={
                    "approval_id": "approval-create-test",
                    "pinterest_connection_id": "connection-create-test",
                    "pinterest_board_id": "board-create-test",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "APPROVED"
        assert body["approval_id"] == "approval-create-test"
        assert body["revision_id"] == "revision-create-test"
        assert body["creative_id"] == "creative-create-test"
        assert body["pinterest_connection_id"] == "connection-create-test"
        assert body["pinterest_board_record_id"] == "board-create-test"
        assert body["pinterest_board_id"] == "external-approved-board-123"
        assert body["title"] == "APPROVED REVISION TITLE"
        assert body["description"] == "APPROVED REVISION DESCRIPTION"
        assert body["alt_text"] == "APPROVED REVISION ALT"
        assert body["destination_url"] == "https://diamondshelf.us/products/approved-product"
        assert body["utm_url"] == "https://diamondshelf.us/products/approved-product?utm_source=pinterest&utm_medium=organic"
        assert body["media_url"] == "https://cdn.example.test/approved-rendered-pin.jpg"
        serialized_body = repr(body)
        assert "OLD DRAFT TITLE - MUST NOT BE SNAPSHOTTED" not in serialized_body
        assert "OLD DRAFT DESCRIPTION - MUST NOT BE SNAPSHOTTED" not in serialized_body
        assert "OLD DRAFT ALT - MUST NOT BE SNAPSHOTTED" not in serialized_body
        assert body["live_publishing_enabled"] is False
        assert body["publishing_readiness_reason"] == "PUBLISHING_DISABLED"

        with TestingSessionLocal() as db:
            publications = db.query(PinPublication).all()
            assert len(publications) == 1
            publication = publications[0]
            expected_fingerprint = publication_identity_fingerprint(
                draft_id="draft-create-test",
                revision_id="revision-create-test",
                creative_id="creative-create-test",
                source_image_id="image-create-test",
                board_id=None,
                integration_account_id=None,
                destination_url="https://diamondshelf.us/products/approved-product",
                utm_url="https://diamondshelf.us/products/approved-product?utm_source=pinterest&utm_medium=organic",
                pinterest_connection_id="connection-create-test",
                pinterest_board_record_id="board-create-test",
                pinterest_board_id_snapshot="external-approved-board-123",
            )
            assert publication.draft_id == "draft-create-test"
            assert publication.revision_id == "revision-create-test"
            assert publication.creative_id == "creative-create-test"
            assert publication.approval_id == "approval-create-test"
            assert publication.source_image_id == "image-create-test"
            assert publication.media_url_snapshot == "https://cdn.example.test/approved-rendered-pin.jpg"
            assert publication.template_id == "template-create-test"
            assert publication.template_key == "approved-template"
            assert publication.template_version == 7
            assert publication.creative_fingerprint == "e" * 64
            assert publication.text_fingerprint == "r" * 64
            assert publication.pinterest_connection_id == "connection-create-test"
            assert publication.pinterest_board_record_id == "board-create-test"
            assert publication.pinterest_board_id_snapshot == "external-approved-board-123"
            assert publication.pinterest_board_id == "external-approved-board-123"
            assert publication.board_id is None
            assert publication.publication_fingerprint == expected_fingerprint
            assert len(publication.publication_fingerprint) == 64
            assert publication.pinterest_pin_id is None
            assert publication.published_at is None
            assert db.query(PublicationAttempt).count() == 0

        serialized_response = response.text + repr(body)
        assert "TEST_ACCESS_CIPHERTEXT_DO_NOT_SERIALIZE" not in serialized_response
        assert "TEST_REFRESH_CIPHERTEXT_DO_NOT_SERIALIZE" not in serialized_response
        assert "access_token" not in serialized_response
        assert "refresh_token" not in serialized_response
        assert "client_secret" not in serialized_response
        assert "Authorization" not in serialized_response
        assert "Bearer" not in serialized_response
        assert "pins:write" not in ["user_accounts:read", "boards:read", "pins:read"]
        assert "boards:write" not in ["user_accounts:read", "boards:read", "pins:read"]
        assert token_decrypt_call_count == 0
        assert provider_call_count == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_api_schedule_reschedule_cancel_lifecycle(monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api.routes import publications as publications_route
    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import Base, PinPublication, PublicationAttempt, PublicationStatus
    from app.services import pinterest_oauth
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    get_settings.cache_clear()

    token_decrypt_call_count = 0
    provider_call_count = 0
    create_snapshot_call_count = 0

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("scheduler API must not decrypt Pinterest tokens")

    async def forbidden_create_pin(*args, **kwargs):
        nonlocal provider_call_count
        provider_call_count += 1
        raise AssertionError("scheduler API must not call the Pinterest gateway")

    def forbidden_create_snapshot(*args, **kwargs):
        nonlocal create_snapshot_call_count
        create_snapshot_call_count += 1
        raise AssertionError("scheduler API must not create publication snapshots")

    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module.PinterestV5Gateway, "create_pin", forbidden_create_pin)
    monkeypatch.setattr(
        publications_route.PublicationIdentityService,
        "create_snapshot",
        forbidden_create_snapshot,
    )

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def parse_utc(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def assert_persisted_wall_clock(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected.replace(tzinfo=timezone.utc)
        else:
            assert value == expected

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            db.add(
                PinPublication(
                    id="api-schedule-publication",
                    draft_id="api-schedule-draft",
                    creative_id="api-schedule-creative",
                    publication_fingerprint="s" * 64,
                    status=PublicationStatus.APPROVED,
                    scheduled_for=None,
                    title_snapshot="API schedule title",
                    description_snapshot="API schedule description",
                    alt_text_snapshot="API schedule alt",
                    destination_url="https://diamondshelf.us/products/api-schedule",
                    media_url_snapshot="https://cdn.example.test/api-schedule.jpg",
                )
            )
            db.commit()
            assert db.query(PinPublication).count() == 1
            assert db.query(PublicationAttempt).count() == 0

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200

            naive_schedule = client.post(
                "/api/publications/api-schedule-publication/schedule",
                headers={"Origin": "http://localhost:5000"},
                json={"scheduled_for": "2030-01-15T14:30:00"},
            )
            assert naive_schedule.status_code == 422
            assert naive_schedule.json()["detail"] == "scheduled_for must include timezone"
            with TestingSessionLocal() as db:
                publication = db.get(PinPublication, "api-schedule-publication")
                assert publication.status == PublicationStatus.APPROVED
                assert publication.scheduled_for is None
                assert db.query(PublicationAttempt).count() == 0

            first_schedule = client.post(
                "/api/publications/api-schedule-publication/schedule",
                headers={"Origin": "http://localhost:5000"},
                json={"scheduled_for": "2030-01-15T14:30:00+05:00"},
            )
            assert first_schedule.status_code == 200
            first_body = first_schedule.json()
            first_expected = datetime(2030, 1, 15, 9, 30, tzinfo=timezone.utc)
            assert first_body["id"] == "api-schedule-publication"
            assert first_body["status"] == "SCHEDULED"
            assert first_body["live_publishing_enabled"] is False
            assert first_body["publishing_readiness_reason"] == "PUBLISHING_DISABLED"
            assert first_body["attempts"] == []
            assert parse_utc(first_body["scheduled_for"]) == first_expected
            with TestingSessionLocal() as db:
                publication = db.get(PinPublication, "api-schedule-publication")
                assert_persisted_wall_clock(
                    publication.scheduled_for,
                    datetime(2030, 1, 15, 9, 30),
                )
                assert db.query(PublicationAttempt).count() == 0

            second_schedule = client.post(
                "/api/publications/api-schedule-publication/schedule",
                headers={"Origin": "http://localhost:5000"},
                json={"scheduled_for": "2030-01-16T01:15:00-04:00"},
            )
            assert second_schedule.status_code == 200
            second_body = second_schedule.json()
            second_expected = datetime(2030, 1, 16, 5, 15, tzinfo=timezone.utc)
            assert second_body["status"] == "SCHEDULED"
            assert parse_utc(second_body["scheduled_for"]) == second_expected
            with TestingSessionLocal() as db:
                publication = db.get(PinPublication, "api-schedule-publication")
                assert_persisted_wall_clock(
                    publication.scheduled_for,
                    datetime(2030, 1, 16, 5, 15),
                )
                assert db.query(PublicationAttempt).count() == 0

            cancel_response = client.post(
                "/api/publications/api-schedule-publication/cancel",
                headers={"Origin": "http://localhost:5000"},
            )
            assert cancel_response.status_code == 200
            cancel_body = cancel_response.json()
            assert cancel_body["id"] == "api-schedule-publication"
            assert cancel_body["status"] == "CANCELLED"
            assert cancel_body["scheduled_for"] is None
            assert cancel_body["live_publishing_enabled"] is False
            assert cancel_body["publishing_readiness_reason"] == "PUBLISHING_DISABLED"
            with TestingSessionLocal() as db:
                publication = db.get(PinPublication, "api-schedule-publication")
                assert publication.status == PublicationStatus.CANCELLED
                assert publication.scheduled_for is None
                assert db.query(PublicationAttempt).count() == 0

            terminal_schedule = client.post(
                "/api/publications/api-schedule-publication/schedule",
                headers={"Origin": "http://localhost:5000"},
                json={"scheduled_for": "2030-01-17T12:00:00+00:00"},
            )
            assert terminal_schedule.status_code == 409
            assert terminal_schedule.json()["detail"] == "Invalid publication transition: CANCELLED -> SCHEDULED"

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "api-schedule-publication")
            assert db.query(PinPublication).count() == 1
            assert publication.status == PublicationStatus.CANCELLED
            assert publication.scheduled_for is None
            assert publication.pinterest_pin_id is None
            assert publication.published_at is None
            assert db.query(PublicationAttempt).count() == 0
        assert token_decrypt_call_count == 0
        assert provider_call_count == 0
        assert create_snapshot_call_count == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_publish_disabled_fails_before_claim_attempt_token_or_provider(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api.routes import publications as publications_route
    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import Base, PinPublication, PublicationAttempt, PublicationStatus
    from app.services import pinterest_oauth, publication_scheduler
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is False

    preflight_call_count = 0
    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_preflight(*args, **kwargs):
        nonlocal preflight_call_count
        preflight_call_count += 1
        raise AssertionError("publishing-disabled API must not run preflight readiness")

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("publishing-disabled API must not claim scheduled publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("publishing-disabled API must not decrypt Pinterest tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("publishing-disabled API must not construct a Pinterest gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("publishing-disabled API must not call provider create_pin")

    monkeypatch.setattr(publications_route, "preflight_publish_readiness", forbidden_preflight)
    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            publication = PinPublication(
                id="publish-disabled-publication",
                draft_id="publish-disabled-draft",
                creative_id="publish-disabled-creative",
                publication_fingerprint="p" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Publish disabled title",
                description_snapshot="Publish disabled description",
                alt_text_snapshot="Publish disabled alt",
                destination_url="https://diamondshelf.us/products/publish-disabled",
                media_url_snapshot="https://cdn.example.test/publish-disabled.jpg",
            )
            db.add(publication)
            db.commit()
            db.refresh(publication)
            assert db.query(PinPublication).count() == 1
            assert db.query(PublicationAttempt).count() == 0
            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            response = client.post(
                "/api/publications/publish-disabled-publication/publish",
                headers={"Origin": "http://localhost:5000"},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": "Publishing is disabled"}
        assert preflight_call_count == 0
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "publish-disabled-publication")
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_publish_scope_preflight_blocks_before_claim_attempt_token_or_provider(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        PinPublication,
        PinterestBoard,
        PinterestConnection,
        PublicationAttempt,
        PublicationStatus,
    )
    from app.services import pinterest_oauth, publication_scheduler
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is True

    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("scope preflight failure must not claim scheduled publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("scope preflight failure must not decrypt Pinterest tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("scope preflight failure must not construct a Pinterest gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("scope preflight failure must not call provider create_pin")

    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    granted_scopes = ["user_accounts:read", "boards:read", "pins:read"]
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            connection = PinterestConnection(
                id="publish-scope-connection",
                external_user_id="publish-scope-user",
                granted_scopes=granted_scopes,
                access_token_ciphertext="TEST_SCOPE_ACCESS_CIPHERTEXT",
                refresh_token_ciphertext="TEST_SCOPE_REFRESH_CIPHERTEXT",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="publish-scope-board",
                connection_id=connection.id,
                external_board_id="publish-scope-external-board",
                name="Publish Scope Board",
                is_active=True,
                is_eligible=True,
            )
            publication = PinPublication(
                id="publish-scope-publication",
                draft_id="publish-scope-draft",
                creative_id="publish-scope-creative",
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=board.id,
                pinterest_board_id_snapshot=board.external_board_id,
                pinterest_board_id=board.external_board_id,
                publication_fingerprint="q" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Publish scope title",
                description_snapshot="Publish scope description",
                alt_text_snapshot="Publish scope alt",
                destination_url="https://diamondshelf.us/products/publish-scope",
                media_url_snapshot="https://cdn.example.test/publish-scope.jpg",
            )
            db.add_all([connection, board, publication])
            db.commit()
            db.refresh(publication)
            assert connection.status == "CONNECTED"
            assert connection.granted_scopes == granted_scopes
            assert "pins:write" not in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.is_active is True
            assert board.is_eligible is True
            assert db.query(PinPublication).count() == 1
            assert db.query(PublicationAttempt).count() == 0
            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            response = client.post(
                "/api/publications/publish-scope-publication/publish",
                headers={"Origin": "http://localhost:5000"},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": "PUBLISHING_SCOPE_REQUIRED"}
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "publish-scope-publication")
            connection = db.get(PinterestConnection, "publish-scope-connection")
            assert connection.status == "CONNECTED"
            assert connection.granted_scopes == granted_scopes
            assert "pins:write" not in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_publish_inactive_destination_fails_before_claim(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        PinPublication,
        PinterestBoard,
        PinterestConnection,
        PublicationAttempt,
        PublicationStatus,
    )
    from app.services import pinterest_oauth, publication_scheduler
    from app.services.pinterest_publisher import preflight_publish_readiness
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is True

    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("inactive destination preflight failure must not claim publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("inactive destination preflight failure must not decrypt tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("inactive destination preflight failure must not construct a gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("inactive destination preflight failure must not call provider create_pin")

    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    granted_scopes = ["user_accounts:read", "boards:read", "pins:read", "pins:write"]
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            connection = PinterestConnection(
                id="inactive-destination-connection",
                external_user_id="inactive-destination-user",
                granted_scopes=granted_scopes,
                access_token_ciphertext="INACTIVE_DESTINATION_ACCESS_CIPHERTEXT_DO_NOT_USE",
                refresh_token_ciphertext="INACTIVE_DESTINATION_REFRESH_CIPHERTEXT_DO_NOT_USE",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="inactive-destination-board",
                connection_id=connection.id,
                external_board_id="inactive-external-board",
                name="Inactive Destination Board",
                is_active=False,
                is_eligible=True,
            )
            publication = PinPublication(
                id="inactive-destination-publication",
                draft_id="inactive-destination-draft",
                creative_id="inactive-destination-creative",
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=board.id,
                pinterest_board_id_snapshot=board.external_board_id,
                pinterest_board_id=board.external_board_id,
                publication_fingerprint="i" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Inactive destination title",
                description_snapshot="Inactive destination description",
                alt_text_snapshot="Inactive destination alt",
                destination_url="https://diamondshelf.us/products/inactive-destination",
                utm_url="https://diamondshelf.us/products/inactive-destination?utm_source=pinterest",
                media_url_snapshot="https://cdn.example.test/inactive-destination.jpg",
            )
            db.add_all([connection, board, publication])
            db.commit()
            db.refresh(publication)
            assert connection.status == "CONNECTED"
            assert connection.granted_scopes == granted_scopes
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == connection.id
            assert board.is_active is False
            assert board.is_eligible is True
            assert publication.pinterest_board_id_snapshot == board.external_board_id
            assert db.query(PublicationAttempt).count() == 0
            ready, reason = preflight_publish_readiness(db, publication)
            assert ready is False
            assert reason == "INVALID_DESTINATION"
            assert db.query(PublicationAttempt).count() == 0
            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            response = client.post(
                "/api/publications/inactive-destination-publication/publish",
                headers={"Origin": "http://localhost:5000"},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": "INVALID_DESTINATION"}
        assert "INACTIVE_DESTINATION_ACCESS_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "INACTIVE_DESTINATION_REFRESH_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "inactive-destination-publication")
            connection = db.get(PinterestConnection, "inactive-destination-connection")
            board = db.get(PinterestBoard, "inactive-destination-board")
            assert connection.status == "CONNECTED"
            assert connection.granted_scopes == granted_scopes
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == connection.id
            assert board.is_active is False
            assert board.is_eligible is True
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_publish_destination_snapshot_mismatch_fails_before_claim(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        PinPublication,
        PinterestBoard,
        PinterestConnection,
        PublicationAttempt,
        PublicationStatus,
    )
    from app.services import pinterest_oauth, publication_scheduler
    from app.services.pinterest_publisher import preflight_publish_readiness
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is True

    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("destination mismatch preflight failure must not claim publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("destination mismatch preflight failure must not decrypt tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("destination mismatch preflight failure must not construct a gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("destination mismatch preflight failure must not call provider create_pin")

    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    granted_scopes = ["user_accounts:read", "boards:read", "pins:read", "pins:write"]
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            connection = PinterestConnection(
                id="destination-mismatch-connection",
                external_user_id="destination-mismatch-user",
                granted_scopes=granted_scopes,
                access_token_ciphertext="DESTINATION_MISMATCH_ACCESS_CIPHERTEXT_DO_NOT_USE",
                refresh_token_ciphertext="DESTINATION_MISMATCH_REFRESH_CIPHERTEXT_DO_NOT_USE",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="destination-mismatch-board",
                connection_id=connection.id,
                external_board_id="current-authoritative-board-456",
                name="Current Authoritative Board",
                is_active=True,
                is_eligible=True,
            )
            publication = PinPublication(
                id="destination-mismatch-publication",
                draft_id="destination-mismatch-draft",
                creative_id="destination-mismatch-creative",
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=board.id,
                pinterest_board_id_snapshot="snapshotted-board-123",
                pinterest_board_id="snapshotted-board-123",
                publication_fingerprint="m" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Destination mismatch title",
                description_snapshot="Destination mismatch description",
                alt_text_snapshot="Destination mismatch alt",
                destination_url="https://diamondshelf.us/products/destination-mismatch",
                utm_url="https://diamondshelf.us/products/destination-mismatch?utm_source=pinterest",
                media_url_snapshot="https://cdn.example.test/destination-mismatch.jpg",
            )
            db.add_all([connection, board, publication])
            db.commit()
            db.refresh(publication)
            assert connection.status == "CONNECTED"
            assert connection.granted_scopes == granted_scopes
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == connection.id
            assert board.is_active is True
            assert board.is_eligible is True
            assert board.external_board_id == "current-authoritative-board-456"
            assert publication.pinterest_board_id_snapshot == "snapshotted-board-123"
            assert board.external_board_id != publication.pinterest_board_id_snapshot
            assert db.query(PublicationAttempt).count() == 0
            ready, reason = preflight_publish_readiness(db, publication)
            assert ready is False
            assert reason == "DESTINATION_MISMATCH"
            assert db.query(PublicationAttempt).count() == 0
            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at
            initial_board_snapshot = publication.pinterest_board_id_snapshot

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            response = client.post(
                "/api/publications/destination-mismatch-publication/publish",
                headers={"Origin": "http://localhost:5000"},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": "DESTINATION_MISMATCH"}
        assert "DESTINATION_MISMATCH_ACCESS_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "DESTINATION_MISMATCH_REFRESH_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "access_token" not in response.text
        assert "refresh_token" not in response.text
        assert "Authorization" not in response.text
        assert "Bearer" not in response.text
        assert "client_secret" not in response.text
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "destination-mismatch-publication")
            connection = db.get(PinterestConnection, "destination-mismatch-connection")
            board = db.get(PinterestBoard, "destination-mismatch-board")
            assert connection.status == "CONNECTED"
            assert connection.granted_scopes == granted_scopes
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == "destination-mismatch-connection"
            assert board.is_active is True
            assert board.is_eligible is True
            assert board.external_board_id == "current-authoritative-board-456"
            assert publication.pinterest_board_id_snapshot == initial_board_snapshot == "snapshotted-board-123"
            assert board.external_board_id != publication.pinterest_board_id_snapshot
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_publish_invalid_approval_fails_before_claim(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        PinApproval,
        PinCreative,
        PinPublication,
        PinterestBoard,
        PinterestConnection,
        PublicationAttempt,
        PublicationStatus,
    )
    from app.services import pinterest_oauth, publication_scheduler
    from app.services.pinterest_publisher import preflight_publish_readiness
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is True

    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("invalid approval preflight failure must not claim publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("invalid approval preflight failure must not decrypt tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("invalid approval preflight failure must not construct a gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("invalid approval preflight failure must not call provider create_pin")

    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    granted_scopes = ["user_accounts:read", "boards:read", "pins:read", "pins:write"]
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            connection = PinterestConnection(
                id="invalid-approval-connection",
                external_user_id="invalid-approval-user",
                granted_scopes=granted_scopes,
                access_token_ciphertext="INVALID_APPROVAL_ACCESS_CIPHERTEXT_DO_NOT_USE",
                refresh_token_ciphertext="INVALID_APPROVAL_REFRESH_CIPHERTEXT_DO_NOT_USE",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="invalid-approval-board",
                connection_id=connection.id,
                external_board_id="invalid-approval-external-board",
                name="Invalid Approval Board",
                is_active=True,
                is_eligible=True,
            )
            creative = PinCreative(
                id="invalid-approval-creative",
                draft_id="invalid-approval-draft",
                template_id="invalid-approval-template",
                source_image_id="invalid-approval-source-image",
                rendered_url="https://cdn.example.test/invalid-approval-rendered.jpg",
                creative_fingerprint="a" * 64,
                render_status="RENDERED",
            )
            approval = PinApproval(
                id="invalid-approval-record",
                draft_id="invalid-approval-draft",
                revision_id=None,
                creative_id=creative.id,
                approved_version_id="original",
                decision="REJECTED",
                decided_by="invalid_approval_api_test",
            )
            publication = PinPublication(
                id="invalid-approval-publication",
                draft_id="invalid-approval-draft",
                revision_id=None,
                creative_id=creative.id,
                approval_id=approval.id,
                source_image_id=creative.source_image_id,
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=board.id,
                pinterest_board_id_snapshot=board.external_board_id,
                pinterest_board_id=board.external_board_id,
                publication_fingerprint="v" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Invalid approval title",
                description_snapshot="Invalid approval description",
                alt_text_snapshot="Invalid approval alt",
                destination_url="https://diamondshelf.us/products/invalid-approval",
                utm_url="https://diamondshelf.us/products/invalid-approval?utm_source=pinterest",
                media_url_snapshot="https://cdn.example.test/invalid-approval-rendered.jpg",
            )
            db.add_all([connection, board, creative, approval, publication])
            db.commit()
            db.refresh(publication)
            assert publication.status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            assert connection.status == "CONNECTED"
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == connection.id
            assert board.is_active is True
            assert board.is_eligible is True
            assert board.external_board_id == publication.pinterest_board_id_snapshot
            assert all(
                [
                    publication.publication_fingerprint,
                    publication.pinterest_connection_id,
                    publication.pinterest_board_record_id,
                    publication.pinterest_board_id_snapshot,
                    publication.title_snapshot,
                    publication.description_snapshot,
                    publication.destination_url,
                    publication.media_url_snapshot,
                ]
            )
            assert approval.id == publication.approval_id
            assert approval.draft_id == publication.draft_id
            assert approval.revision_id == publication.revision_id
            assert approval.creative_id == publication.creative_id
            assert approval.decision == "REJECTED"
            assert creative.id == publication.creative_id
            assert creative.draft_id == publication.draft_id
            assert creative.source_image_id == publication.source_image_id
            assert db.query(PublicationAttempt).count() == 0
            ready, reason = preflight_publish_readiness(db, publication)
            assert ready is False
            assert reason == "INVALID_APPROVAL"
            assert db.query(PublicationAttempt).count() == 0
            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at
            initial_approval_decision = approval.decision
            initial_approval_draft_id = approval.draft_id
            initial_approval_revision_id = approval.revision_id
            initial_approval_creative_id = approval.creative_id

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            response = client.post(
                "/api/publications/invalid-approval-publication/publish",
                headers={"Origin": "http://localhost:5000"},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": "INVALID_APPROVAL"}
        assert "INVALID_APPROVAL_ACCESS_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "INVALID_APPROVAL_REFRESH_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "access_token" not in response.text
        assert "refresh_token" not in response.text
        assert "Authorization" not in response.text
        assert "Bearer" not in response.text
        assert "client_secret" not in response.text
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "invalid-approval-publication")
            approval = db.get(PinApproval, "invalid-approval-record")
            creative = db.get(PinCreative, "invalid-approval-creative")
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert approval.decision == initial_approval_decision == "REJECTED"
            assert approval.draft_id == initial_approval_draft_id == publication.draft_id
            assert approval.revision_id == initial_approval_revision_id == publication.revision_id
            assert approval.creative_id == initial_approval_creative_id == publication.creative_id
            assert creative.id == publication.creative_id
            assert creative.draft_id == publication.draft_id
            assert creative.source_image_id == publication.source_image_id
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_publish_invalid_creative_provenance_fails_before_claim(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        PinApproval,
        PinCreative,
        PinPublication,
        PinterestBoard,
        PinterestConnection,
        PublicationAttempt,
        PublicationStatus,
    )
    from app.services import pinterest_oauth, publication_scheduler
    from app.services.pinterest_publisher import media_publishable, preflight_publish_readiness
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is True

    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("invalid creative preflight failure must not claim publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("invalid creative preflight failure must not decrypt tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("invalid creative preflight failure must not construct a gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("invalid creative preflight failure must not call provider create_pin")

    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    granted_scopes = ["user_accounts:read", "boards:read", "pins:read", "pins:write"]
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            connection = PinterestConnection(
                id="invalid-creative-connection",
                external_user_id="invalid-creative-user",
                granted_scopes=granted_scopes,
                access_token_ciphertext="INVALID_CREATIVE_ACCESS_CIPHERTEXT_DO_NOT_USE",
                refresh_token_ciphertext="INVALID_CREATIVE_REFRESH_CIPHERTEXT_DO_NOT_USE",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="invalid-creative-board",
                connection_id=connection.id,
                external_board_id="invalid-creative-external-board",
                name="Invalid Creative Board",
                is_active=True,
                is_eligible=True,
            )
            creative = PinCreative(
                id="invalid-creative-record",
                draft_id="invalid-creative-draft",
                template_id="invalid-creative-template",
                source_image_id="changed-source-image",
                rendered_url="https://cdn.example.test/invalid-creative-rendered.jpg",
                creative_fingerprint="k" * 64,
                render_status="RENDERED",
            )
            approval = PinApproval(
                id="invalid-creative-approval",
                draft_id="invalid-creative-draft",
                revision_id=None,
                creative_id=creative.id,
                approved_version_id="original",
                decision="APPROVED",
                decided_by="invalid_creative_api_test",
            )
            publication = PinPublication(
                id="invalid-creative-publication",
                draft_id="invalid-creative-draft",
                revision_id=None,
                creative_id=creative.id,
                approval_id=approval.id,
                source_image_id="frozen-source-image",
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=board.id,
                pinterest_board_id_snapshot=board.external_board_id,
                pinterest_board_id=board.external_board_id,
                publication_fingerprint="z" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Invalid creative title",
                description_snapshot="Invalid creative description",
                alt_text_snapshot="Invalid creative alt",
                destination_url="https://diamondshelf.us/products/invalid-creative",
                utm_url="https://diamondshelf.us/products/invalid-creative?utm_source=pinterest",
                media_url_snapshot="https://cdn.example.test/invalid-creative-rendered.jpg",
            )
            db.add_all([connection, board, creative, approval, publication])
            db.commit()
            db.refresh(publication)
            assert publication.status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            assert connection.status == "CONNECTED"
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == connection.id
            assert board.is_active is True
            assert board.is_eligible is True
            assert board.external_board_id == publication.pinterest_board_id_snapshot
            assert all(
                [
                    publication.publication_fingerprint,
                    publication.pinterest_connection_id,
                    publication.pinterest_board_record_id,
                    publication.pinterest_board_id_snapshot,
                    publication.title_snapshot,
                    publication.description_snapshot,
                    publication.alt_text_snapshot,
                    publication.destination_url,
                    publication.utm_url,
                    publication.media_url_snapshot,
                ]
            )
            assert approval.id == publication.approval_id
            assert approval.decision == "APPROVED"
            assert approval.draft_id == publication.draft_id
            assert approval.revision_id == publication.revision_id
            assert approval.creative_id == publication.creative_id
            assert creative.id == publication.creative_id
            assert creative.draft_id == publication.draft_id
            assert creative.source_image_id == "changed-source-image"
            assert publication.source_image_id == "frozen-source-image"
            assert creative.source_image_id != publication.source_image_id
            assert media_publishable(publication.media_url_snapshot) is True
            assert db.query(PublicationAttempt).count() == 0
            ready, reason = preflight_publish_readiness(db, publication)
            assert ready is False
            assert reason == "INVALID_CREATIVE"
            assert db.query(PublicationAttempt).count() == 0
            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at
            initial_publication_source_image_id = publication.source_image_id
            initial_creative_source_image_id = creative.source_image_id
            initial_approval_decision = approval.decision
            initial_approval_draft_id = approval.draft_id
            initial_approval_revision_id = approval.revision_id
            initial_approval_creative_id = approval.creative_id

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            response = client.post(
                "/api/publications/invalid-creative-publication/publish",
                headers={"Origin": "http://localhost:5000"},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": "INVALID_CREATIVE"}
        assert "INVALID_CREATIVE_ACCESS_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "INVALID_CREATIVE_REFRESH_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "access_token" not in response.text
        assert "refresh_token" not in response.text
        assert "Authorization" not in response.text
        assert "Bearer" not in response.text
        assert "client_secret" not in response.text
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "invalid-creative-publication")
            approval = db.get(PinApproval, "invalid-creative-approval")
            creative = db.get(PinCreative, "invalid-creative-record")
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert publication.source_image_id == initial_publication_source_image_id == "frozen-source-image"
            assert creative.source_image_id == initial_creative_source_image_id == "changed-source-image"
            assert creative.source_image_id != publication.source_image_id
            assert approval.decision == initial_approval_decision == "APPROVED"
            assert approval.draft_id == initial_approval_draft_id == publication.draft_id
            assert approval.revision_id == initial_approval_revision_id == publication.revision_id
            assert approval.creative_id == initial_approval_creative_id == publication.creative_id
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_publication_publish_nonpublishable_media_fails_before_claim(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core import auth
    from app.core.config import get_settings
    from app.db.session import get_db
    from app.integrations.pinterest import gateway as gateway_module
    from app.main import app
    from app.models.domain import (
        Base,
        PinApproval,
        PinCreative,
        PinPublication,
        PinterestBoard,
        PinterestConnection,
        PublicationAttempt,
        PublicationStatus,
    )
    from app.services import pinterest_oauth, publication_scheduler
    from app.services.pinterest_publisher import media_publishable, preflight_publish_readiness
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().publishing_enabled is True

    claim_call_count = 0
    token_decrypt_call_count = 0
    gateway_constructor_call_count = 0
    provider_call_count = 0

    def forbidden_claim(*args, **kwargs):
        nonlocal claim_call_count
        claim_call_count += 1
        raise AssertionError("media preflight failure must not claim publications")

    def forbidden_decrypt_token(*args, **kwargs):
        nonlocal token_decrypt_call_count
        token_decrypt_call_count += 1
        raise AssertionError("media preflight failure must not decrypt tokens")

    class ForbiddenGateway:
        def __init__(self, *args, **kwargs):
            nonlocal gateway_constructor_call_count
            gateway_constructor_call_count += 1
            raise AssertionError("media preflight failure must not construct a gateway")

        async def create_pin(self, *args, **kwargs):
            nonlocal provider_call_count
            provider_call_count += 1
            raise AssertionError("media preflight failure must not call provider create_pin")

    monkeypatch.setattr(publication_scheduler, "claim", forbidden_claim)
    monkeypatch.setattr(pinterest_oauth, "decrypt_token", forbidden_decrypt_token)
    monkeypatch.setattr(gateway_module, "PinterestV5Gateway", ForbiddenGateway)

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def same_persisted_instant(value, expected):
        assert value is not None
        if value.tzinfo is not None and value.utcoffset() is not None:
            assert value.astimezone(timezone.utc) == expected
        else:
            assert value == expected.replace(tzinfo=None)

    scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=10)
    media_url = "http://cdn.example.test/media-not-publishable.jpg"
    granted_scopes = ["user_accounts:read", "boards:read", "pins:read", "pins:write"]
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestingSessionLocal() as db:
            connection = PinterestConnection(
                id="media-preflight-connection",
                external_user_id="media-preflight-user",
                granted_scopes=granted_scopes,
                access_token_ciphertext="MEDIA_PREFLIGHT_ACCESS_CIPHERTEXT_DO_NOT_USE",
                refresh_token_ciphertext="MEDIA_PREFLIGHT_REFRESH_CIPHERTEXT_DO_NOT_USE",
                status="CONNECTED",
            )
            board = PinterestBoard(
                id="media-preflight-board",
                connection_id=connection.id,
                external_board_id="media-preflight-external-board",
                name="Media Preflight Board",
                is_active=True,
                is_eligible=True,
            )
            creative = PinCreative(
                id="media-preflight-creative",
                draft_id="media-preflight-draft",
                template_id="media-preflight-template",
                source_image_id="media-preflight-source-image",
                rendered_url="https://cdn.example.test/media-preflight-rendered.jpg",
                creative_fingerprint="w" * 64,
                render_status="RENDERED",
            )
            approval = PinApproval(
                id="media-preflight-approval",
                draft_id="media-preflight-draft",
                revision_id=None,
                creative_id=creative.id,
                approved_version_id="original",
                decision="APPROVED",
                decided_by="media_preflight_api_test",
            )
            publication = PinPublication(
                id="media-preflight-publication",
                draft_id="media-preflight-draft",
                revision_id=None,
                creative_id=creative.id,
                approval_id=approval.id,
                source_image_id=creative.source_image_id,
                pinterest_connection_id=connection.id,
                pinterest_board_record_id=board.id,
                pinterest_board_id_snapshot=board.external_board_id,
                pinterest_board_id=board.external_board_id,
                publication_fingerprint="h" * 64,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=scheduled_for,
                title_snapshot="Media preflight title",
                description_snapshot="Media preflight description",
                alt_text_snapshot="Media preflight alt",
                destination_url="https://diamondshelf.us/products/media-preflight",
                utm_url="https://diamondshelf.us/products/media-preflight?utm_source=pinterest",
                media_url_snapshot=media_url,
            )
            db.add_all([connection, board, creative, approval, publication])
            db.commit()
            db.refresh(publication)
            assert publication.status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            assert connection.status == "CONNECTED"
            assert "pins:write" in connection.granted_scopes
            assert "boards:write" not in connection.granted_scopes
            assert board.connection_id == connection.id
            assert board.is_active is True
            assert board.is_eligible is True
            assert board.external_board_id == publication.pinterest_board_id_snapshot
            assert all(
                [
                    publication.publication_fingerprint,
                    publication.pinterest_connection_id,
                    publication.pinterest_board_record_id,
                    publication.pinterest_board_id_snapshot,
                    publication.title_snapshot,
                    publication.description_snapshot,
                    publication.alt_text_snapshot,
                    publication.destination_url,
                    publication.utm_url,
                    publication.media_url_snapshot,
                ]
            )
            assert approval.id == publication.approval_id
            assert approval.decision == "APPROVED"
            assert approval.draft_id == publication.draft_id
            assert approval.revision_id == publication.revision_id
            assert approval.creative_id == publication.creative_id
            assert creative.id == publication.creative_id
            assert creative.draft_id == publication.draft_id
            assert creative.source_image_id == publication.source_image_id
            assert publication.media_url_snapshot == media_url
            assert publication.media_url_snapshot.startswith("http://")
            assert media_publishable(publication.media_url_snapshot) is False
            assert db.query(PublicationAttempt).count() == 0
            ready, reason = preflight_publish_readiness(db, publication)
            assert ready is False
            assert reason == "MEDIA_NOT_PUBLISHABLE"
            assert db.query(PublicationAttempt).count() == 0
            initial_status = publication.status
            initial_scheduled_for = publication.scheduled_for
            initial_attempt_started_at = publication.attempt_started_at
            initial_error_code = publication.error_code
            initial_pinterest_pin_id = publication.pinterest_pin_id
            initial_published_at = publication.published_at
            initial_media_url_snapshot = publication.media_url_snapshot
            initial_approval_decision = approval.decision
            initial_approval_draft_id = approval.draft_id
            initial_approval_revision_id = approval.revision_id
            initial_approval_creative_id = approval.creative_id
            initial_creative_draft_id = creative.draft_id
            initial_creative_source_image_id = creative.source_image_id

        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            assert login.status_code == 200
            response = client.post(
                "/api/publications/media-preflight-publication/publish",
                headers={"Origin": "http://localhost:5000"},
            )

        assert response.status_code == 409
        assert response.json() == {"detail": "MEDIA_NOT_PUBLISHABLE"}
        assert "MEDIA_PREFLIGHT_ACCESS_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "MEDIA_PREFLIGHT_REFRESH_CIPHERTEXT_DO_NOT_USE" not in response.text
        assert "access_token" not in response.text
        assert "refresh_token" not in response.text
        assert "Authorization" not in response.text
        assert "Bearer" not in response.text
        assert "client_secret" not in response.text
        assert claim_call_count == 0
        assert token_decrypt_call_count == 0
        assert gateway_constructor_call_count == 0
        assert provider_call_count == 0

        with TestingSessionLocal() as db:
            publication = db.get(PinPublication, "media-preflight-publication")
            approval = db.get(PinApproval, "media-preflight-approval")
            creative = db.get(PinCreative, "media-preflight-creative")
            assert publication.status == initial_status == PublicationStatus.SCHEDULED
            same_persisted_instant(publication.scheduled_for, scheduled_for)
            same_persisted_instant(initial_scheduled_for, scheduled_for)
            assert publication.attempt_started_at == initial_attempt_started_at
            assert publication.error_code == initial_error_code
            assert publication.pinterest_pin_id == initial_pinterest_pin_id
            assert publication.published_at == initial_published_at
            assert publication.media_url_snapshot == initial_media_url_snapshot == media_url
            assert approval.decision == initial_approval_decision == "APPROVED"
            assert approval.draft_id == initial_approval_draft_id == publication.draft_id
            assert approval.revision_id == initial_approval_revision_id == publication.revision_id
            assert approval.creative_id == initial_approval_creative_id == publication.creative_id
            assert creative.draft_id == initial_creative_draft_id == publication.draft_id
            assert creative.source_image_id == initial_creative_source_image_id == publication.source_image_id
            assert db.query(PublicationAttempt).count() == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
