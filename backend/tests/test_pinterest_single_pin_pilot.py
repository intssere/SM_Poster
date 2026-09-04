from types import SimpleNamespace
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.services import pinterest_single_pin_pilot as pilot_service
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_single_pin_pilot import validate_pilot_candidate

def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _publication(db):
    row = PinPublication(id="pilot-pub", draft_id="draft", revision_id="revision", creative_id="creative", approval_id="approval", source_image_id="source", template_id="template", template_key="key", template_version=1, text_fingerprint="text-fp", creative_fingerprint="creative-fp", pinterest_connection_id="connection", pinterest_board_record_id="board", pinterest_board_id_snapshot="external", status=PublicationStatus.SCHEDULED, publication_fingerprint="pub-fp", title_snapshot="Title", description_snapshot="Description", alt_text_snapshot="Alt", destination_url="https://diamondshelf.us/p", utm_url="https://diamondshelf.us/p?utm_source=pinterest", media_url_snapshot="https://cdn.shopify.com/p.jpg")
    db.add(row); db.commit(); return row

def _settings(pub, *, enabled=True, pilot=True, publication_id=None, publication_fingerprint=None, request_fingerprint=None):
    request_fp = request_fingerprint_for(pub)
    return SimpleNamespace(publishing_enabled=enabled, pinterest_single_pin_pilot_enabled=pilot, pinterest_single_pin_pilot_publication_id=pub.id if publication_id is None else publication_id, pinterest_single_pin_pilot_publication_fingerprint=pub.publication_fingerprint if publication_fingerprint is None else publication_fingerprint, pinterest_single_pin_pilot_request_fingerprint=request_fp if request_fingerprint is None else request_fingerprint)

def test_pilot_requires_exact_server_binding(monkeypatch):
    db = _db(); pub = _publication(db)
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub, pilot=False))
    assert validate_pilot_candidate(db, pub) == (False, "PILOT_DISABLED")
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub, enabled=False))
    assert validate_pilot_candidate(db, pub) == (False, "PILOT_DISABLED")

def test_pilot_rejects_any_prior_attempt(monkeypatch):
    db = _db(); pub = _publication(db)
    db.add(PublicationAttempt(publication_id=pub.id, attempt_number=1, status="FAILED", request_fingerprint="old")); db.commit()
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub))
    assert validate_pilot_candidate(db, pub) == (False, "PILOT_ALREADY_ATTEMPTED")

def test_pilot_accepts_exact_unattempted_candidate(monkeypatch):
    db = _db(); pub = _publication(db)
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub))
    assert validate_pilot_candidate(db, pub) == (True, "READY")

@pytest.mark.parametrize("field", ["publication_id", "publication_fingerprint", "request_fingerprint"])
def test_pilot_rejects_incomplete_binding(monkeypatch, field):
    db = _db(); pub = _publication(db)
    values = {"publication_id": pub.id, "publication_fingerprint": pub.publication_fingerprint, "request_fingerprint": request_fingerprint_for(pub)}
    values[field] = ""
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub, **values))
    assert validate_pilot_candidate(db, pub)[0] is False
    assert validate_pilot_candidate(db, pub)[1] == "PILOT_BINDING_INCOMPLETE"

@pytest.mark.parametrize("status", ["STARTED", "FAILED", "UNKNOWN", "SUCCEEDED"])
def test_pilot_blocks_every_prior_attempt(monkeypatch, status):
    db = _db(); pub = _publication(db)
    db.add(PublicationAttempt(publication_id=pub.id, attempt_number=1, status=status, request_fingerprint="old")); db.commit()
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub))
    assert validate_pilot_candidate(db, pub) == (False, "PILOT_ALREADY_ATTEMPTED")

@pytest.mark.parametrize("kwargs,reason", [
    ({"publication_id": "other"}, "PILOT_PUBLICATION_MISMATCH"),
    ({"publication_fingerprint": "other"}, "PILOT_PUBLICATION_FINGERPRINT_MISMATCH"),
    ({"request_fingerprint": "other"}, "PILOT_REQUEST_FINGERPRINT_MISMATCH"),
])
def test_pilot_rejects_binding_drift(monkeypatch, kwargs, reason):
    db = _db(); pub = _publication(db)
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub, **kwargs))
    assert validate_pilot_candidate(db, pub) == (False, reason)

def test_post_claim_pilot_requires_exact_single_started_attempt(monkeypatch):
    db = _db(); pub = _publication(db)
    pub.status = PublicationStatus.PUBLISHING
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub))
    attempt = PublicationAttempt(publication_id=pub.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(pub)); db.add(attempt); db.commit()
    assert pilot_service.validate_post_claim_pilot(db, pub, attempt) == (True, "READY")
    attempt.status = "FAILED"; db.commit()
    assert pilot_service.validate_post_claim_pilot(db, pub, attempt)[0] is False

def test_post_claim_pilot_rejects_disabled_and_drift(monkeypatch):
    db = _db(); pub = _publication(db)
    attempt = PublicationAttempt(publication_id=pub.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(pub)); db.add(attempt); db.commit()
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub, pilot=False))
    assert pilot_service.validate_post_claim_pilot(db, pub, attempt) == (False, "PILOT_DISABLED")
    monkeypatch.setattr(pilot_service, "get_settings", lambda: _settings(pub, request_fingerprint="drift"))
    assert pilot_service.validate_post_claim_pilot(db, pub, attempt) == (False, "PILOT_BINDING_MISMATCH")
