from datetime import datetime, timezone, timedelta
import asyncio
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models.domain import Base, PinterestConnection, PinterestBoard, PinterestBoardSection
from app.services.pinterest_boards import sync_boards, eligible_boards
from app.core.config import get_settings
from app.main import app
from app.db.session import get_db
from fastapi.testclient import TestClient

class FakeClient:
    def __init__(self, pages): self.pages = pages; self.calls = []
    async def get(self, path, token, params=None):
        self.calls.append((path, params)); return self.pages.get((path, (params or {}).get("bookmark")), self.pages.get((path, None), {"items": []}))

@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine); Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session
    engine.dispose()

@pytest.fixture
def app_db():
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine); Session = sessionmaker(bind=engine)
    def override():
        s = Session()
        try: yield s
        finally: s.close()
    app.dependency_overrides[get_db] = override
    try: yield Session
    finally: app.dependency_overrides.pop(get_db, None); engine.dispose()

def auth_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:"); monkeypatch.setenv("APP_ENV", "development"); monkeypatch.setenv("AUTH_DISABLED", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()

def connected(db):
    row = PinterestConnection(external_user_id="acct", access_token_ciphertext="x", refresh_token_ciphertext="y", status="CONNECTED")
    db.add(row); db.commit(); return row

def test_paginated_sync_upserts_and_preserves_local_config(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db); fake = FakeClient({("/v5/boards", None): {"items": [{"id":"b1","name":"One","privacy":"PUBLIC"}], "bookmark":"next"}, ("/v5/boards","next"): {"items":[{"id":"b2","name":"Two"}]}, ("/v5/boards/b1/sections", None): {"items":[{"id":"s1","name":"Section"}]}, ("/v5/boards/b2/sections", None): {"items":[]}})
    asyncio.run(sync_boards(db, conn, fake))
    b = db.query(PinterestBoard).filter_by(external_board_id="b1").one(); b.is_eligible = True; b.routing_label = "hair"; db.commit()
    asyncio.run(sync_boards(db, conn, fake))
    assert db.query(PinterestBoard).count() == 2 and db.query(PinterestBoardSection).count() == 1
    assert db.query(PinterestBoard).filter_by(external_board_id="b1").one().routing_label == "hair"
    assert len(fake.calls) >= 4

def test_successful_full_sync_inactivates_missing_boards_and_sections(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db); old = PinterestBoard(connection_id=conn.id, external_board_id="old", name="Old"); db.add(old); db.flush(); db.add(PinterestBoardSection(board_id=old.id, external_section_id="gone", name="Gone")); db.commit()
    fake = FakeClient({("/v5/boards", None): {"items": [{"id":"new","name":"New"}]}, ("/v5/boards/new/sections", None): {"items":[]}})
    asyncio.run(sync_boards(db, conn, fake))
    assert db.query(PinterestBoard).filter_by(external_board_id="old").one().is_active is False
    assert db.query(PinterestBoardSection).one().is_active is False

def test_partial_provider_failure_does_not_inactivate_existing(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db); old = PinterestBoard(connection_id=conn.id, external_board_id="old", name="Old"); db.add(old); db.commit()
    class Broken(FakeClient):
        async def get(self, path, token, params=None): raise RuntimeError("sanitized")
    with pytest.raises(RuntimeError): asyncio.run(sync_boards(db, conn, Broken({})))
    assert db.query(PinterestBoard).one().is_active is True

def test_eligible_board_boundary(db):
    conn = connected(db); db.add_all([PinterestBoard(connection_id=conn.id, external_board_id="a", name="A", is_active=True, is_eligible=True), PinterestBoard(connection_id=conn.id, external_board_id="b", name="B", is_active=False, is_eligible=True)]); db.commit()
    assert [x.external_board_id for x in eligible_boards(db, conn.id)] == ["a"]

def test_disconnected_connection_fails_closed(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db); conn.status = "DISCONNECTED"; db.commit()
    with pytest.raises(RuntimeError, match="not connected"): asyncio.run(sync_boards(db, conn, FakeClient({})))

def test_repeated_board_bookmark_rejected(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db); fake = FakeClient({("/v5/boards", None): {"items": [], "bookmark": "loop"}, ("/v5/boards", "loop"): {"items": [], "bookmark": "loop"}})
    with pytest.raises(RuntimeError, match="pagination"): asyncio.run(sync_boards(db, conn, fake))
    assert len(fake.calls) == 2

def test_repeated_section_bookmark_rejected(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db); fake = FakeClient({("/v5/boards", None): {"items": [{"id":"b1", "name":"Board"}]}, ("/v5/boards/b1/sections", None): {"items": [], "bookmark": "loop"}, ("/v5/boards/b1/sections", "loop"): {"items": [], "bookmark": "loop"}})
    with pytest.raises(RuntimeError, match="pagination"): asyncio.run(sync_boards(db, conn, fake))

def test_provider_metadata_updates_and_local_config_survives(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db); fake = FakeClient({("/v5/boards", None): {"items": [{"id":"b1", "name":"New", "privacy":"SECRET"}]}, ("/v5/boards/b1/sections", None): {"items": [{"id":"s1", "name":"Updated"}]}})
    asyncio.run(sync_boards(db, conn, fake)); board = db.query(PinterestBoard).one(); board.is_eligible = True; board.routing_label = "hair"; db.commit()
    asyncio.run(sync_boards(db, conn, fake)); board = db.query(PinterestBoard).one(); section = db.query(PinterestBoardSection).one()
    assert board.name == "New" and board.privacy == "SECRET" and board.is_eligible and board.routing_label == "hair" and section.name == "Updated"

def test_malformed_board_and_section_payloads_fail_safely(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "token")
    conn = connected(db)
    with pytest.raises(RuntimeError, match="invalid"): asyncio.run(sync_boards(db, conn, FakeClient({("/v5/boards", None): {"items": [{"name":"missing-id"}]}})))
    with pytest.raises(RuntimeError, match="invalid"): asyncio.run(sync_boards(db, conn, FakeClient({("/v5/boards", None): {"items": [{"id":"b", "name":"Board"}]}, ("/v5/boards/b/sections", None): {"items": [{"name":"missing-id"}]}})))

def test_provider_failure_does_not_expose_credentials(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "secret-token")
    conn = connected(db)
    class Broken(FakeClient):
        async def get(self, path, token, params=None): raise RuntimeError(f"provider body {token}")
    with pytest.raises(RuntimeError) as exc: asyncio.run(sync_boards(db, conn, Broken({})))
    assert "secret-token" not in str(exc.value)

def _payload(**fields):
    value = {"id":"v", "name":"Board"}; value.update(fields); return value

@pytest.mark.parametrize("field,value", [("description",None),("description","ok"),("privacy","PUBLIC")])
def test_nullable_provider_strings_accept_valid_values(db, monkeypatch, field, value):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "t"); conn = connected(db)
    fake = FakeClient({("/v5/boards",None): {"items":[_payload(**{field:value})]}, ("/v5/boards/v/sections",None): {"items":[]}})
    asyncio.run(sync_boards(db, conn, fake)); assert db.query(PinterestBoard).one()

@pytest.mark.parametrize("field,value", [("description",{}),("description",[]),("description",True),("description",1),("privacy","x"*41),("privacy",{}),("privacy",[]),("privacy",False),("privacy",1),("owner",[]),("owner","x"),("owner",{"username":{}}),("owner",{"username":[]}), ("owner",{"username":True}), ("owner",{"username":"x"*256}), ("media",[]),("media","x"),("media",{"image_cover_url":{}}),("media",{"image_cover_url":[]}), ("media",{"image_cover_url":False}),("media",{"image_cover_url":1})])
def test_malformed_nullable_provider_strings_fail_closed(db, monkeypatch, field, value):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "t"); conn = connected(db)
    with pytest.raises(RuntimeError, match="invalid"):
        asyncio.run(sync_boards(db, conn, FakeClient({("/v5/boards",None): {"items":[_payload(**{field:value})]}})))

@pytest.mark.parametrize("name", [None,"","   ",{},[],True,1,"x"*256])
def test_malformed_section_names_preserve_existing_state(db, monkeypatch, name):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "t"); conn = connected(db)
    with pytest.raises(RuntimeError, match="invalid"):
        asyncio.run(sync_boards(db, conn, FakeClient({("/v5/boards",None): {"items":[_payload()]}, ("/v5/boards/v/sections",None): {"items":[{"id":"s", "name":name}]}})))
    assert conn.boards_last_synced_at is None

def test_pre_sync_refreshes_expiring_token_once(db, monkeypatch):
    conn = connected(db); conn.access_token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1); db.commit(); calls = []
    async def refresh(session, connection): calls.append(1); connection.access_token_ciphertext = "new"; connection.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1); session.commit(); return connection
    monkeypatch.setattr("app.services.pinterest_boards.refresh_connection", refresh); monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda token: "token")
    asyncio.run(sync_boards(db, conn, FakeClient({("/v5/boards",None): {"items":[]}}))); assert calls == [1]

def test_pre_sync_refresh_failure_makes_no_discovery_call(db, monkeypatch):
    conn = connected(db); conn.access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1); db.commit(); calls = []
    async def fail(*args): raise RuntimeError("safe refresh failure")
    class Spy(FakeClient):
        async def get(self, *args, **kwargs): calls.append(1); return {"items":[]}
    monkeypatch.setattr("app.services.pinterest_boards.refresh_connection", fail)
    with pytest.raises(RuntimeError): asyncio.run(sync_boards(db, conn, Spy({})))
    assert not calls

@pytest.mark.parametrize("value", ["true", "false", 1, 0])
def test_patch_rejects_non_boolean_is_eligible(monkeypatch, app_db, value):
    auth_env(monkeypatch)
    with app_db() as s:
        c = connected(s); b = PinterestBoard(connection_id=c.id, external_board_id="p", name="P"); s.add(b); s.commit(); bid = b.id
    response = TestClient(app).patch(f"/api/channels/pinterest/boards/{bid}", json={"is_eligible": value}, headers={"Origin":"http://localhost:5000"})
    assert response.status_code == 422

@pytest.mark.parametrize("value", [True, False])
def test_patch_accepts_real_boolean_is_eligible(monkeypatch, app_db, value):
    auth_env(monkeypatch)
    with app_db() as s:
        c = connected(s); b = PinterestBoard(connection_id=c.id, external_board_id="p", name="P"); s.add(b); s.commit(); bid = b.id
    response = TestClient(app).patch(f"/api/channels/pinterest/boards/{bid}", json={"is_eligible": value}, headers={"Origin":"http://localhost:5000"})
    assert response.status_code == 200 and response.json()["is_eligible"] is value

@pytest.mark.parametrize("payload", [{"name":"x"},{"external_board_id":"x"},{"is_eligible":True,"name":"x"}])
def test_patch_rejects_provider_owned_fields_without_partial_apply(monkeypatch, app_db, payload):
    auth_env(monkeypatch)
    with app_db() as s:
        c = connected(s); b = PinterestBoard(connection_id=c.id, external_board_id="p", name="P", is_eligible=False); s.add(b); s.commit(); bid = b.id
    response = TestClient(app).patch(f"/api/channels/pinterest/boards/{bid}", json=payload, headers={"Origin":"http://localhost:5000"})
    assert response.status_code == 422
    with app_db() as s: assert s.get(PinterestBoard, bid).is_eligible is False

def test_publication_validator_explicit_boundaries(db):
    from app.services.pinterest_boards import validate_publication_board
    c1, c2 = connected(db), connected(db); c2.status = "DISCONNECTED"
    good = PinterestBoard(connection_id=c1.id, external_board_id="good", name="Good", is_active=True, is_eligible=True)
    inactive = PinterestBoard(connection_id=c1.id, external_board_id="inactive", name="Inactive", is_active=False, is_eligible=True)
    ineligible = PinterestBoard(connection_id=c1.id, external_board_id="ineligible", name="Ineligible", is_active=True, is_eligible=False)
    wrong = PinterestBoard(connection_id=c2.id, external_board_id="wrong", name="Wrong", is_active=True, is_eligible=True); db.add_all([good,inactive,ineligible,wrong]); db.commit()
    assert validate_publication_board(db, c1.id, good.id).id == good.id
    for cid, bid in [(c1.id,"unknown"),(c1.id,inactive.id),(c1.id,ineligible.id),(c2.id,wrong.id),("missing",good.id)]:
        with pytest.raises(ValueError): validate_publication_board(db, cid, bid)

def test_board_api_security_origin_matrix(monkeypatch, app_db):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:"); monkeypatch.setenv("APP_ENV", "development"); monkeypatch.setenv("AUTH_DISABLED", "false"); monkeypatch.setenv("ADMIN_USERNAME", "admin"); monkeypatch.setenv("ADMIN_PASSWORD", "pw"); get_settings.cache_clear()
    client = TestClient(app)
    assert client.get("/api/channels/pinterest/boards").status_code == 401
    assert client.post("/api/channels/pinterest/boards/sync").status_code == 403
    assert client.post("/api/channels/pinterest/boards/sync", headers={"Origin":"https://evil.example"}).status_code == 403
    assert client.patch("/api/channels/pinterest/boards/missing", json={"is_eligible":True}).status_code == 403
    assert client.patch("/api/channels/pinterest/boards/missing", json={"is_eligible":True}, headers={"Origin":"https://evil.example"}).status_code == 403

def test_official_nested_metadata_persists(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "t"); conn = connected(db)
    fake = FakeClient({("/v5/boards", None): {"items":[{"id":"official","name":"Catalog","owner":{"username":"owner"},"media":{"image_cover_url":"https://img"},"pin_count":1,"follower_count":2,"collaborator_count":0,"is_ads_only":False,"board_pins_modified_at":"2026-01-01T00:00:00Z","created_at":"2025-01-01T00:00:00Z"}]}, ("/v5/boards/official/sections", None): {"items":[]}})
    asyncio.run(sync_boards(db, conn, fake)); row = db.query(PinterestBoard).one()
    assert (row.owner_username,row.image_cover_url,row.pin_count,row.follower_count,row.collaborator_count,row.is_ads_only) == ("owner","https://img",1,2,0,False)
    assert row.board_pins_modified_at and row.provider_created_at and row.last_synced_at

def test_hard_board_and_section_page_limits(db, monkeypatch):
    monkeypatch.setattr("app.services.pinterest_boards.decrypt_token", lambda _: "t"); conn = connected(db)
    from app.services.pinterest_boards import MAX_BOARD_PAGES, MAX_SECTION_PAGES
    class Endless(FakeClient):
        async def get(self, path, token, params=None):
            self.calls.append((path,params));
            if path == "/v5/boards": return {"items": [], "bookmark": str(len(self.calls))}
            return {"items": [], "bookmark": str(len(self.calls))}
    with pytest.raises(RuntimeError, match="limit"): asyncio.run(sync_boards(db, conn, Endless({})))
    class SectionEndless(Endless):
        async def get(self, path, token, params=None):
            self.calls.append((path,params)); return {"items":[{"id":"b","name":"B"}]} if path == "/v5/boards" else {"items":[],"bookmark":str(len(self.calls))}
    with pytest.raises(RuntimeError, match="limit"): asyncio.run(sync_boards(db, conn, SectionEndless({})))
    assert MAX_BOARD_PAGES > 0 and MAX_SECTION_PAGES > 0

def test_board_api_auth_and_patch_local_only(monkeypatch, app_db):
    auth_env(monkeypatch)
    with app_db() as s:
        conn = PinterestConnection(external_user_id="acct", access_token_ciphertext="secret-access", refresh_token_ciphertext="secret-refresh"); s.add(conn); s.flush(); board = PinterestBoard(connection_id=conn.id, external_board_id="b", name="Board"); s.add(board); s.commit(); board_id = board.id
    client = TestClient(app)
    assert client.get("/api/channels/pinterest/boards").status_code == 200
    assert client.get("/api/channels/pinterest/boards").json()["boards"][0]["external_board_id"] == "b"
    calls = []
    monkeypatch.setattr("app.api.routes.channels.sync_boards", lambda *a, **k: calls.append(1))
    response = client.patch(f"/api/channels/pinterest/boards/{board_id}", json={"is_eligible": True, "routing_label": "hair"}, headers={"Origin":"http://localhost:5000"})
    assert response.status_code == 200 and response.json()["is_eligible"] and response.json()["routing_label"] == "hair" and not calls
    assert client.patch(f"/api/channels/pinterest/boards/{board_id}", json={"name":"provider"}, headers={"Origin":"http://localhost:5000"}).status_code == 422
    body = str(client.get("/api/channels/pinterest/boards").json()); assert all(x not in body for x in ("secret-access", "secret-refresh", "access_token_ciphertext", "refresh_token_ciphertext", "Authorization"))

def test_board_api_anonymous_denied_when_auth_enabled(monkeypatch, app_db):
    auth_env(monkeypatch); monkeypatch.setenv("AUTH_DISABLED", "false"); monkeypatch.setenv("ADMIN_PASSWORD", "pw"); monkeypatch.setenv("ADMIN_USERNAME", "admin")
    get_settings.cache_clear()
    assert TestClient(app).get("/api/channels/pinterest/boards").status_code == 401
