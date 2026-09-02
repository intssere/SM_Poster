def test_publications_router_is_registered():
    import os
    os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    from app.main import app
    assert any(getattr(route, "path", "") == "/api/publications" for route in app.routes)
