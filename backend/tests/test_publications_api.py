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
