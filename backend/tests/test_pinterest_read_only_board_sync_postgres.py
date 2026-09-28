"""PostgreSQL contract for the opt-in provider-read-only board refresh."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.auth import hash_password
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.domain import PinterestBoard, PinterestBoardSection, PinterestConnection
from app.services.pinterest_boards import (
    MAX_BOARD_PAGES, MAX_READ_ONLY_BOARDS, MAX_READ_ONLY_SECTIONS, sync_boards,
)


@pytest.fixture(scope="module")
def engine():
    # CI keeps DATABASE_URL on SQLite but exposes its disposable PostgreSQL service here.
    url = os.getenv("TASK595_POSTGRES_URL") or os.getenv("DATABASE_URL")
    if not url or not url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        pytest.skip("development PostgreSQL DATABASE_URL required")
    database = f"task595b_{uuid4().hex[:16]}"
    source = make_url(url).set(drivername="postgresql+psycopg")
    admin = sa.create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
        test_engine = sa.create_engine(source.set(database=database))
        try:
            Base.metadata.create_all(test_engine)
            yield test_engine
        finally:
            test_engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(
                sa.text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname=:database AND pid <> pg_backend_pid()"),
                {"database": database},
            )
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}"'))
        admin.dispose()


@pytest.fixture
def db(engine):
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session
        session.rollback()


def connected(db, *, expiry=None):
    connection = PinterestConnection(
        external_user_id=f"acct-{uuid4().hex}",
        access_token_ciphertext="test-access",
        refresh_token_ciphertext="test-refresh",
        access_token_expires_at=expiry or datetime.now(timezone.utc) + timedelta(hours=1),
        status="CONNECTED",
    )
    db.add(connection)
    db.commit()
    return connection


class ReadOnlyClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get(self, path, token, params=None):
        assert token == "test-token"
        assert path == "/boards" or (path.startswith("/boards/") and path.endswith("/sections"))
        self.calls.append((path, (params or {}).get("bookmark")))
        return self.pages[path, (params or {}).get("bookmark")]

    async def post(self, *args, **kwargs):
        pytest.fail("provider POST is forbidden")

    async def patch(self, *args, **kwargs):
        pytest.fail("provider PATCH is forbidden")

    async def delete(self, *args, **kwargs):
        pytest.fail("provider DELETE is forbidden")


@pytest.fixture
def no_refresh(monkeypatch):
    async def blocked(*args, **kwargs):
        pytest.fail("OAuth refresh is forbidden in read-only mode")

    monkeypatch.setattr("app.services.pinterest_boards.refresh_connection", blocked)
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "test-token")


def test_read_only_sync_persists_only_bounded_routing_evidence(db, no_refresh):
    connection = connected(db)
    old = PinterestBoard(
        connection_id=connection.id, external_board_id="old", name="Old",
        is_eligible=True, routing_label="preserve",
    )
    db.add(old)
    db.flush()
    db.add(PinterestBoardSection(board_id=old.id, external_section_id="gone", name="Gone"))
    db.commit()
    client = ReadOnlyClient({
        ("/boards", None): {"items": [{"id": "old", "name": "New name"}], "bookmark": "next"},
        ("/boards", "next"): {"items": [{"id": "new", "name": "New board"}]},
        ("/boards/old/sections", None): {"items": [{"id": "s", "name": "Current"}]},
        ("/boards/new/sections", None): {"items": []},
    })
    changed = set()

    def record_changes(session, flush_context, instances):
        changed.update(type(row) for row in session.new | session.dirty | session.deleted)

    sa.event.listen(db, "before_flush", record_changes)
    try:
        assert asyncio.run(sync_boards(db, connection, client, provider_read_only=True)) == 2
    finally:
        sa.event.remove(db, "before_flush", record_changes)

    assert changed <= {PinterestConnection, PinterestBoard, PinterestBoardSection}
    assert client.calls == [
        ("/boards", None), ("/boards", "next"),
        ("/boards/old/sections", None), ("/boards/new/sections", None),
    ]
    boards = {row.external_board_id: row for row in db.query(PinterestBoard).filter_by(connection_id=connection.id)}
    assert set(boards) == {"old", "new"}
    assert boards["old"].name == "New name"
    assert boards["old"].is_eligible is True and boards["old"].routing_label == "preserve"
    assert db.query(PinterestBoardSection).filter_by(board_id=old.id, external_section_id="gone").one().is_active is False
    assert db.query(PinterestBoardSection).filter_by(board_id=old.id, external_section_id="s").one().is_active is True
    assert connection.boards_last_synced_at is not None
    assert connection.access_token_ciphertext == "test-access"
    assert connection.refresh_token_ciphertext == "test-refresh"


@pytest.mark.parametrize("expiry", [
    None,
    datetime(2030, 1, 1),  # naive / ambiguous offset
    datetime(2030, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    datetime(2030, 1, 1, tzinfo=timezone.utc).replace(fold=1),
    "unknown",
    timedelta(minutes=1),
    timedelta(minutes=7),
    timedelta(minutes=10),
])
def test_bad_or_near_expiry_fails_before_provider_or_write(db, engine, no_refresh, expiry):
    connection = connected(db)
    if isinstance(expiry, timedelta):
        expiry = datetime.now(timezone.utc) + expiry
    connection.access_token_expires_at = expiry
    client = ReadOnlyClient({})
    writes = []

    def detect_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")):
            writes.append(statement.split()[0])

    sa.event.listen(engine, "before_cursor_execute", detect_write)
    try:
        with pytest.raises(RuntimeError, match="safely fresh token"):
            asyncio.run(sync_boards(db, connection, client, provider_read_only=True))
    finally:
        sa.event.remove(engine, "before_cursor_execute", detect_write)
        db.rollback()
    assert not client.calls and not writes
    assert db.get(PinterestConnection, connection.id).boards_last_synced_at is None


def test_read_only_sync_page_limit_keeps_routing_uncommitted(db, no_refresh):
    connection = connected(db)

    class Endless(ReadOnlyClient):
        async def get(self, path, token, params=None):
            self.calls.append((path, (params or {}).get("bookmark")))
            return {"items": [], "bookmark": str(len(self.calls))}

    client = Endless({})
    with pytest.raises(RuntimeError, match="pagination limit"):
        asyncio.run(sync_boards(db, connection, client, provider_read_only=True))
    db.rollback()
    assert len(client.calls) == MAX_BOARD_PAGES
    assert db.get(PinterestConnection, connection.id).boards_last_synced_at is None


@pytest.mark.parametrize("items, expected_calls", [
    ([{"id": "bad/sections", "name": "Wrong path"}], 1),
    ([{"id": "bad?query", "name": "Wrong query"}], 1),
    ([{"id": str(i), "name": "Board"} for i in range(MAX_READ_ONLY_BOARDS + 1)], 1),
])
def test_read_only_rejects_out_of_scope_ids_and_unbounded_pages(
    db, no_refresh, items, expected_calls,
):
    connection = connected(db)
    client = ReadOnlyClient({("/boards", None): {"items": items}})
    with pytest.raises(RuntimeError, match="invalid|limit"):
        asyncio.run(sync_boards(db, connection, client, provider_read_only=True))
    db.rollback()
    assert len(client.calls) == expected_calls
    assert db.query(PinterestBoard).filter_by(connection_id=connection.id).count() == 0
    assert db.get(PinterestConnection, connection.id).boards_last_synced_at is None


def test_read_only_rejects_oversized_sections_without_committing(db, no_refresh):
    connection = connected(db)
    client = ReadOnlyClient({
        ("/boards", None): {"items": [{"id": "one", "name": "One"}]},
        ("/boards/one/sections", None): {
            "items": [{"id": str(i), "name": "Section"} for i in range(MAX_READ_ONLY_SECTIONS + 1)]
        },
    })
    with pytest.raises(RuntimeError, match="item limit"):
        asyncio.run(sync_boards(db, connection, client, provider_read_only=True))
    db.rollback()
    assert client.calls == [("/boards", None), ("/boards/one/sections", None)]
    assert db.query(PinterestBoard).filter_by(connection_id=connection.id).count() == 0
    assert (db.query(PinterestBoardSection).join(PinterestBoard)
            .filter(PinterestBoard.connection_id == connection.id).count() == 0)
    assert db.get(PinterestConnection, connection.id).boards_last_synced_at is None


@pytest.mark.parametrize("after_path", ["/boards", "/boards/one/sections"])
def test_expiry_crossing_after_get_stops_before_further_sql_writes(
    db, engine, no_refresh, after_path,
):
    connection = connected(db)
    writes_after_crossing = []
    crossed = [False]

    def detect_write(conn, cursor, statement, parameters, context, executemany):
        if crossed[0] and statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")):
            writes_after_crossing.append(statement.split()[0])

    class CrossingClient(ReadOnlyClient):
        async def get(self, path, token, params=None):
            result = await super().get(path, token, params)
            if path == after_path:
                connection.access_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
                crossed[0] = True
            return result

    client = CrossingClient({
        ("/boards", None): {"items": [{"id": "one", "name": "One"}]},
        ("/boards/one/sections", None): {"items": []},
    })
    sa.event.listen(engine, "before_cursor_execute", detect_write)
    try:
        with pytest.raises(RuntimeError, match="safely fresh token"):
            asyncio.run(sync_boards(db, connection, client, provider_read_only=True))
    finally:
        sa.event.remove(engine, "before_cursor_execute", detect_write)
        db.rollback()
    assert crossed[0] and not writes_after_crossing
    assert db.query(PinterestBoard).filter_by(connection_id=connection.id).count() == 0
    assert db.get(PinterestConnection, connection.id).boards_last_synced_at is None


def test_ordinary_sync_still_refreshes_expiring_token(db, monkeypatch):
    connection = connected(db, expiry=datetime.now(timezone.utc) + timedelta(minutes=1))
    calls = []

    async def refresh(session, row):
        calls.append("refresh")
        row.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        session.commit()

    monkeypatch.setattr("app.services.pinterest_boards.refresh_connection", refresh)
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "test-token")
    client = ReadOnlyClient({("/boards", None): {"items": []}})
    assert asyncio.run(sync_boards(db, connection, client)) == 0
    assert calls == ["refresh"] and client.calls == [("/boards", None)]


def test_new_route_requires_auth_and_origin_then_uses_read_only_mode(
    db, engine, monkeypatch, no_refresh,
):
    connection = connected(db)
    monkeypatch.delenv("REPLIT_DEV_DOMAIN", raising=False)
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("test-password"))
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-" * 3)
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    get_settings.cache_clear()
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    client = ReadOnlyClient({("/boards", None): {"items": []}})
    monkeypatch.setattr("app.services.pinterest_boards.PinterestBoardClient", lambda: client)
    try:
        with TestClient(app) as http:
            url = "/api/channels/pinterest/boards/sync-read-only"
            assert http.post(url, headers={"Origin": "http://localhost:5000"}).status_code == 401
            login = http.post("/api/auth/login", json={"username": "test-admin", "password": "test-password"})
            assert login.status_code == 200
            assert http.post(url).status_code == 403
            response = http.post(url, headers={"Origin": "http://localhost:5000"})
            assert response.status_code == 200
            assert response.json()["sync"] == {"boards_seen": 0}
            assert client.calls == [("/boards", None)]
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()