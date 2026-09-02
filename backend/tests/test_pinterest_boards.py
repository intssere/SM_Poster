from datetime import datetime, timezone
import asyncio
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models.domain import Base, PinterestConnection, PinterestBoard, PinterestBoardSection
from app.services.pinterest_boards import sync_boards, eligible_boards

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
