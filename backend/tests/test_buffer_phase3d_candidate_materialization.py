from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models.domain import AuditLog, Board
from app.services.buffer_pilot_candidate_materialization import (
    BufferDestinationBindingEvidence, bind_buffer_destination,
)
from app.services.public_creative_media import public_origin, public_creative_url
from test_manual_publication_dispatch import _db


def _settings():
    return Settings(_env_file=None, DATABASE_URL="sqlite+pysqlite:///:memory:",
                    buffer_organization_id="org", buffer_pinterest_channel_id="channel",
                    public_media_base_url="https://media.example.com")


def test_public_origin_is_https_and_reserved_host_safe():
    assert public_origin("https://media.example.com") == "https://media.example.com"
    assert public_origin("http://media.example.com") is None
    assert public_origin("https://localhost") is None
    assert public_origin("https://media.example.com/path") is None
    assert public_origin("https://media.example.com:abc") is None


def test_buffer_binding_requires_fresh_explicit_evidence_and_is_idempotent(tmp_path):
    Session, engine = _db(tmp_path / "binding.db")
    with Session() as db:
        board = Board(store_id="store", name="Fragrance", slug="fragrance", active=True)
        db.add(board); db.commit()
        settings = _settings()
        evidence = BufferDestinationBindingEvidence("org", "channel", "pinterest-board-1", datetime.now(timezone.utc), True)
        bound = bind_buffer_destination(db, board.id, actor="operator", evidence=evidence, settings=settings)
        assert bound.pinterest_board_id == "pinterest-board-1"
        assert bind_buffer_destination(db, board.id, actor="operator", evidence=evidence, settings=settings).pinterest_board_id == "pinterest-board-1"
        assert db.scalar(select(AuditLog).where(AuditLog.entity_id == board.id)).metadata_json == {
            "board_id": board.id, "board_service_id": "pinterest-board-1"
        }
    engine.dispose()


@pytest.mark.parametrize("case", ["missing", "unverified", "stale", "future"])
def test_buffer_binding_rejects_missing_false_or_stale_evidence(tmp_path, case):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    evidence = {
        "missing": None,
        "unverified": BufferDestinationBindingEvidence("org", "channel", "id", now, False),
        "stale": BufferDestinationBindingEvidence("org", "channel", "id", now - timedelta(days=1), True),
        "future": BufferDestinationBindingEvidence("org", "channel", "id", now + timedelta(seconds=1), True),
    }[case]
    Session, engine = _db(tmp_path / "binding-invalid.db")
    with Session() as db:
        board = Board(store_id="store", name="Fragrance", slug="fragrance", active=True)
        db.add(board); db.commit()
        with pytest.raises(RuntimeError):
            bind_buffer_destination(db, board.id, actor="operator", evidence=evidence, settings=_settings(), now=now)
        assert board.pinterest_board_id is None
    engine.dispose()


def test_public_creative_url_requires_digest_and_exact_identity():
    class Creative:
        id = "creative-1"; sha256 = "a" * 64; render_status = "RENDERED"
    assert public_creative_url(Creative(), settings=_settings()) == "https://media.example.com/api/pins/public-creatives/creative-1/" + "a" * 64 + ".png"


@pytest.mark.parametrize("mutation", ["missing", "inactive", "conflict"])
def test_buffer_binding_rejects_missing_inactive_or_conflicting_board(tmp_path, mutation):
    Session, engine = _db(tmp_path / f"binding-{mutation}.db")
    with Session() as db:
        board = Board(store_id="store", name="Fragrance", slug="fragrance", active=mutation != "inactive")
        db.add(board); db.commit()
        evidence = BufferDestinationBindingEvidence("org", "channel", "service-a", datetime.now(timezone.utc), True)
        if mutation == "missing":
            db.delete(board); db.commit()
            board_id = board.id
        else:
            board_id = board.id
            if mutation == "conflict":
                board.pinterest_board_id = "service-existing"; db.commit()
        with pytest.raises(RuntimeError):
            bind_buffer_destination(db, board_id, actor="operator", evidence=evidence, settings=_settings())
    engine.dispose()


def test_public_origin_requires_configured_base_and_exact_digest_binding():
    class Creative:
        id = "creative-2"; sha256 = "b" * 64; render_status = "PENDING"
    assert public_origin(None) is None
    assert public_creative_url(Creative(), settings=_settings()) is None


@pytest.mark.parametrize("value", ["https://127.0.0.1", "https://10.0.0.1", "https://192.168.1.1", "https://[::1]", "https://user:pass@example.com", "https://example.com/?q=1", "https://example.com/#frag"])
def test_public_origin_rejects_non_public_or_ambiguous_urls(value):
    assert public_origin(value) is None


@pytest.mark.parametrize("value", ["", "bad id", "bad!id", "a" * 256])
def test_buffer_binding_rejects_malformed_board_service_id(tmp_path, value):
    Session, engine = _db(tmp_path / "binding-malformed.db")
    with Session() as db:
        board = Board(store_id="store", name="Fragrance", slug="fragrance", active=True)
        db.add(board); db.commit()
        evidence = BufferDestinationBindingEvidence("org", "channel", value, datetime.now(timezone.utc), True)
        with pytest.raises(RuntimeError):
            bind_buffer_destination(db, board.id, actor="operator", evidence=evidence, settings=_settings())
    engine.dispose()


@pytest.mark.parametrize("field", ["organization_id", "channel_id"])
def test_buffer_binding_rejects_provider_identity_mismatch(tmp_path, field):
    Session, engine = _db(tmp_path / "binding-mismatch.db")
    with Session() as db:
        board = Board(store_id="store", name="Fragrance", slug="fragrance", active=True)
        db.add(board); db.commit()
        kwargs = {"buffer_organization_id": "org", "buffer_pinterest_channel_id": "channel", "board_service_id": "id", "observed_at": datetime.now(timezone.utc), "provider_destination_live_verified": True}
        kwargs["buffer_organization_id" if field == "organization_id" else "buffer_pinterest_channel_id"] = "other"
        with pytest.raises(RuntimeError):
            bind_buffer_destination(db, board.id, actor="operator", evidence=BufferDestinationBindingEvidence(**kwargs), settings=_settings())
    engine.dispose()
