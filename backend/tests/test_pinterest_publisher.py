from app.services.pinterest_publisher import media_publishable

def test_media_url_rejects_private_and_accepts_public():
    assert not media_publishable("https://localhost/a")
    assert not media_publishable("http://example.test/a")
    assert not media_publishable("https://127.0.0.1/a")
    assert media_publishable("https://cdn.example.test/a")

import pytest
from app.services.pinterest_publisher import sanitize_metadata
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.services.publication_scheduler import request_fingerprint_for
from test_pin_proposals import setup_service
import asyncio

@pytest.mark.parametrize("value", ["http://x", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.1/x", "https://x.local/a"])
def test_media_publishability_rejects_non_public(value):
    assert not media_publishable(value)

@pytest.mark.parametrize("value", ["https://cdn.example.test/a", "https://images.example.org/pin.png"])
def test_media_publishability_accepts_public_https(value):
    assert media_publishable(value)

def test_metadata_allowlist_removes_credentials_and_raw_payloads():
    data = {"access_token":"secret", "refresh_token":"secret2", "Authorization":"Bearer x", "raw_body":"x", "validated_pin_id":"pin123", "http_status":201, "request_id":"r"}
    assert sanitize_metadata(data) == {"validated_pin_id":"pin123", "http_status":201, "request_id":"r"}

@pytest.mark.parametrize("value", [None, [], ["secret"], "secret", 123])
def test_metadata_allowlist_handles_none_and_non_mapping(value):
    assert sanitize_metadata(value) == {}

def test_db_backed_publish_success_records_attempt_and_provider_pin(monkeypatch):
    db, _, _ = setup_service()
    publication = PinPublication(draft_id="draft", creative_id="creative", approval_id="approval", source_image_id="image", publication_fingerprint="f"*64, status=PublicationStatus.PUBLISHING, pinterest_board_id_snapshot="board", title_snapshot="Title", description_snapshot="Description", destination_url="https://example.test", media_url_snapshot="https://cdn.example.test/image.png")
    db.add(publication); db.commit(); db.refresh(publication)
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=1, status="STARTED", request_fingerprint=request_fingerprint_for(publication), safe_response_metadata={})
    db.add(attempt); db.commit(); db.refresh(attempt)
    monkeypatch.setattr("app.services.pinterest_publisher.execution_publish_readiness", lambda db, pub, att: (True, None))
    class Gateway:
        calls = 0
        async def create_pin(self, payload):
            self.calls += 1
            return {"id": "pin123"}
    gateway = Gateway()
    result = asyncio.run(__import__("app.services.pinterest_publisher", fromlist=["publish_once"]).publish_once(db, publication, gateway, attempt))
    assert result["id"] == "pin123" and gateway.calls == 1
    assert publication.status == PublicationStatus.PUBLISHED
    assert attempt.status == "SUCCEEDED" and attempt.provider_pin_id == "pin123"
    db.close()
