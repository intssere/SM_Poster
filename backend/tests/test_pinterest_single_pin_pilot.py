from types import SimpleNamespace
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.services import publication_dispatch_authorization as auth_service
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_single_pin_pilot import validate_pilot_candidate

def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _publication(db):
    row = PinPublication(id="pilot-pub", draft_id="draft", revision_id="revision", creative_id="creative", approval_id="approval", source_image_id="source", template_id="template", template_key="key", template_version=1, text_fingerprint="text-fp", creative_fingerprint="creative-fp", pinterest_connection_id="connection", pinterest_board_record_id="board", pinterest_board_id_snapshot="external", status=PublicationStatus.SCHEDULED, publication_fingerprint="pub-fp", title_snapshot="Title", description_snapshot="Description", alt_text_snapshot="Alt", destination_url="https://diamondshelf.us/p", utm_url="https://diamondshelf.us/p?utm_source=pinterest", media_url_snapshot="https://cdn.shopify.com/p.jpg")
    db.add(row); db.commit(); return row

def _settings(pub, *, enabled=True, pilot=True, prior=False):
    request_fp = request_fingerprint_for(pub)
    return SimpleNamespace(publishing_enabled=enabled, pinterest_single_pin_pilot_enabled=pilot, pinterest_single_pin_pilot_publication_id=pub.id, pinterest_single_pin_pilot_publication_fingerprint=pub.publication_fingerprint, pinterest_single_pin_pilot_request_fingerprint=request_fp)

def test_pilot_requires_exact_server_binding(monkeypatch):
    db = _db(); pub = _publication(db)
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(pub, pilot=False))
    assert validate_pilot_candidate(db, pub) == (False, "PILOT_DISABLED")
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(pub, enabled=False))
    assert validate_pilot_candidate(db, pub) == (False, "PILOT_DISABLED")

def test_pilot_rejects_any_prior_attempt(monkeypatch):
    db = _db(); pub = _publication(db)
    db.add(PublicationAttempt(publication_id=pub.id, attempt_number=1, status="FAILED", request_fingerprint="old")); db.commit()
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(pub))
    assert validate_pilot_candidate(db, pub) == (False, "PILOT_ALREADY_ATTEMPTED")

def test_pilot_accepts_exact_unattempted_candidate(monkeypatch):
    db = _db(); pub = _publication(db)
    monkeypatch.setattr(auth_service, "get_settings", lambda: _settings(pub))
    assert validate_pilot_candidate(db, pub) == (True, "READY")
