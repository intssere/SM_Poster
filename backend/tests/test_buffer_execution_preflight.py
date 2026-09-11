import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.services.buffer_execution_preflight import (
    BufferPreflightError, build_buffer_execution_evidence, verify_live_image,
)


async def public_resolver(*args, **kwargs):
    return [(None, None, None, None, ("93.184.216.34", 443))]


def response(status=200, headers=None, body=b"image"):
    async def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "images.examplecdn.com"
        assert request.extensions["sni_hostname"] == "images.examplecdn.com"
        return httpx.Response(status, headers=headers or {"content-type": "image/png"}, content=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.parametrize("url", [
    "http://images.example.test/pin.png",
    "https://127.0.0.1/pin.png",
    "https://localhost/pin.png",
])
def test_media_rejects_unsafe_destinations(url):
    with pytest.raises(BufferPreflightError) as error:
        asyncio.run(verify_live_image(url, client=response(), resolver=public_resolver))
    assert str(error.value) == "BUFFER_MEDIA_DESTINATION_UNSAFE"


@pytest.mark.parametrize(("status", "headers", "body", "code"), [
    (302, {"location": "https://images.example.test/other.png"}, b"", "BUFFER_MEDIA_REDIRECT_REJECTED"),
    (200, {"content-type": "text/html"}, b"x", "BUFFER_MEDIA_NOT_IMAGE"),
    (200, {"content-type": "image/png", "content-length": "10485761"}, b"x", "BUFFER_MEDIA_TOO_LARGE"),
])
def test_media_fail_closed(status, headers, body, code):
    client = response(status, headers, body)
    try:
        with pytest.raises(BufferPreflightError) as error:
            asyncio.run(verify_live_image("https://images.examplecdn.com/pin.png", client=client, resolver=public_resolver))
        assert str(error.value) == code
    finally:
        asyncio.run(client.aclose())


def test_preflight_derives_server_owned_evidence(monkeypatch):
    publication = type("Publication", (), {
        "id": "publication", "publication_fingerprint": "fingerprint",
        "pinterest_board_id_snapshot": "board", "title_snapshot": "title",
        "description_snapshot": "description", "alt_text_snapshot": "alt",
        "destination_url": "https://shop.example.test/product",
        "utm_url": "https://shop.example.test/product?utm_source=pinterest",
        "media_url_snapshot": "https://images.examplecdn.com/pin.png",
    })()
    settings = type("Settings", (), {
        "buffer_api_key": "configured", "buffer_organization_id": "organization",
        "buffer_pinterest_channel_id": "channel",
    })()
    monkeypatch.setattr("app.services.buffer_execution_preflight.active_authorization", lambda db, pid: object())
    monkeypatch.setattr("app.services.buffer_execution_preflight.validate_authorization",
                        lambda *args, **kwargs: {"valid": True})
    monkeypatch.setattr("app.services.buffer_execution_preflight.validate_pilot",
                        lambda *args, **kwargs: (True, "READY"))
    async def verify(*args):
        return {"organization_id": "organization", "channel_id": "channel", "board_service_id": "board"}
    async def media(*args, **kwargs):
        return None
    monkeypatch.setattr("app.services.buffer_execution_preflight.verify_destination", verify)
    monkeypatch.setattr("app.services.buffer_execution_preflight.verify_live_image", media)
    monkeypatch.setattr("app.services.buffer_execution_preflight.request_fingerprint_for", lambda row: "request")
    evidence = asyncio.run(build_buffer_execution_evidence(
        object(), publication, settings=settings, gateway=object(),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    assert evidence.publication_id == "publication"
    assert evidence.request_fingerprint == "request"
    assert evidence.board_service_id == "board"
    assert evidence.provider_destination_live_verified
    assert evidence.media_live_fetch_verified