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
