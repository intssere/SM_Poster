from app.services.pinterest_publisher import media_publishable

def test_media_url_rejects_private_and_accepts_public():
    assert not media_publishable("https://localhost/a")
    assert not media_publishable("http://example.test/a")
    assert not media_publishable("https://127.0.0.1/a")
    assert media_publishable("https://cdn.example.test/a")

import pytest
from app.services.pinterest_publisher import sanitize_metadata

@pytest.mark.parametrize("value", ["http://x", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.1/x", "https://x.local/a"])
def test_media_publishability_rejects_non_public(value):
    assert not media_publishable(value)

@pytest.mark.parametrize("value", ["https://cdn.example.test/a", "https://images.example.org/pin.png"])
def test_media_publishability_accepts_public_https(value):
    assert media_publishable(value)

def test_metadata_allowlist_removes_credentials_and_raw_payloads():
    data = {"access_token":"secret", "refresh_token":"secret2", "Authorization":"Bearer x", "raw_body":"x", "validated_pin_id":"pin123", "http_status":201, "request_id":"r"}
    assert sanitize_metadata(data) == {"validated_pin_id":"pin123", "http_status":201, "request_id":"r"}

def test_metadata_allowlist_handles_none_and_non_mapping():
    assert sanitize_metadata(None) == {}
