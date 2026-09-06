import asyncio
import copy
import json

import httpx
import pytest

from app.core.config import Settings
from app.models.domain import PinPublication
from app.integrations.buffer.gateway import BufferGateway, BufferConfigurationError
from app.services.buffer_pinterest_adapter import build_pinterest_payload, verify_destination


def settings():
    return Settings(_env_file=None, DATABASE_URL="sqlite+pysqlite:///:memory:",
        buffer_api_key="fake-test-only-key", buffer_api_base="https://api.buffer.com",
        buffer_organization_id="org", buffer_pinterest_channel_id="channel",
        publishing_enabled=False, buffer_publishing_enabled=False)


def publication():
    return PinPublication(pinterest_board_id_snapshot="board-123", title_snapshot="Exact title",
        description_snapshot="Exact description", destination_url="https://diamondshelf.us/products/item",
        utm_url="https://diamondshelf.us/products/item?utm_source=pinterest",
        media_url_snapshot="https://cdn.shopify.com/image.jpg", alt_text_snapshot="Exact alt text")


def test_exact_snapshot_mapping_is_pure():
    p = publication()
    before = dict(p.__dict__)
    result = build_pinterest_payload(p, settings()).to_input()
    assert result == {"channelId": "channel", "text": "Exact description", "schedulingType": "automatic", "mode": "shareNow",
        "assets": [{"image": {"url": p.media_url_snapshot, "metadata": {"altText": "Exact alt text"}}}],
        "metadata": {"pinterest": {"boardServiceId": "board-123", "title": "Exact title", "url": p.utm_url}}}
    assert p.__dict__ == before
    assert result["metadata"]["pinterest"]["url"] != p.destination_url


@pytest.mark.parametrize("field", ["pinterest_board_id_snapshot", "title_snapshot", "description_snapshot", "utm_url", "media_url_snapshot", "alt_text_snapshot"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_incomplete_snapshot_rejected(field, value):
    p = publication()
    setattr(p, field, value)
    with pytest.raises(BufferConfigurationError):
        build_pinterest_payload(p, settings())


@pytest.mark.parametrize("url", ["http://cdn.shopify.com/image.jpg", "https://localhost/image", "https://host.local/image",
    "https://127.0.0.1/image", "https://10.1.2.3/image", "https://169.254.169.254/image", "https://[::1]/image",
    "https://cdn.shopify.com:abc/image", "https://cdn.shopify.com:99999/image", "https://user:pass@cdn.shopify.com/image",
    "file:///image", "https://example.test/image", "https://2130706433/image"])
def test_unsafe_media_rejected_without_http(url):
    p = publication()
    p.media_url_snapshot = url
    with pytest.raises(BufferConfigurationError):
        build_pinterest_payload(p, settings())


@pytest.mark.parametrize("url", ["https://other.us/products/item?utm_source=pinterest", "https://diamondshelf.us/other",
    "https://diamondshelf.us:8443/products/item", "https://diamondshelf.us:abc/products/item"])
def test_utm_target_must_match_snapshot(url):
    p = publication()
    p.utm_url = url
    with pytest.raises(BufferConfigurationError):
        build_pinterest_payload(p, settings())


def test_default_https_port_matches_without_rewriting_utm():
    p = publication()
    p.utm_url = "https://diamondshelf.us:443/products/item?utm_source=pinterest"
    assert build_pinterest_payload(p, settings()).url == p.utm_url


def test_copy_is_not_truncated_or_rewritten():
    p = publication()
    p.title_snapshot = " Title " * 100
    p.description_snapshot = 'Description "quoted"\n' * 1000
    p.alt_text_snapshot = " Alt " * 100
    result = build_pinterest_payload(p, settings())
    assert (result.title, result.text, result.alt_text) == (p.title_snapshot, p.description_snapshot, p.alt_text_snapshot)


def test_utm_preserves_destination_variant_query():
    p = publication()
    p.destination_url += "?variant=123"
    with pytest.raises(BufferConfigurationError, match="BUFFER_DESTINATION_MISMATCH"):
        build_pinterest_payload(p, settings())
    p.utm_url += "&variant=123"
    assert build_pinterest_payload(p, settings()).url == p.utm_url


@pytest.mark.parametrize("case,reason", [("valid", None), ("missing-org", "BUFFER_ORGANIZATION_NOT_FOUND"),
    ("missing-channel", "BUFFER_CHANNEL_NOT_FOUND"), ("disconnected", "BUFFER_CHANNEL_UNAVAILABLE"),
    ("locked", "BUFFER_CHANNEL_UNAVAILABLE"), ("other-service", "BUFFER_CHANNEL_UNAVAILABLE"),
    ("wrong-board", "BUFFER_BOARD_NOT_FOUND"), ("name-only", "BUFFER_BOARD_NOT_FOUND")])
def test_destination_preflight_uses_exact_server_identity_and_board_service_id(case, reason):
    p = publication()
    before = dict(p.__dict__)
    calls = []
    raw_channel = {"id": "channel", "name": "Board account", "service": "pinterest", "isDisconnected": False,
        "isLocked": False, "metadata": {"boards": [{"serviceId": "board-123", "name": "Board"}]}}
    if case == "disconnected":
        raw_channel["isDisconnected"] = True
    if case == "locked":
        raw_channel["isLocked"] = True
    if case == "other-service":
        raw_channel["service"] = "instagram"
    if case in {"wrong-board", "name-only"}:
        raw_channel["metadata"]["boards"] = [{"serviceId": "wrong", "name": "board-123" if case == "name-only" else "Board"}]
    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        assert "mutation" not in data["query"]
        if "account" in data["query"]:
            body = {"account": {"organizations": [{"id": "other" if case == "missing-org" else "org", "name": "Org"}]}}
        else:
            assert data["variables"] == {"input": {"organizationId": "org"}}
            body = {"channels": [] if case == "missing-channel" else [copy.deepcopy(raw_channel)]}
        return httpx.Response(200, json={"data": body})
    async def run():
        config = settings()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = BufferGateway(config, client=client)
            if reason:
                with pytest.raises(BufferConfigurationError, match=f"^{reason}$"):
                    await verify_destination(p, config, gateway)
            else:
                assert await verify_destination(p, config, gateway) == {"organization_id": "org", "channel_id": "channel", "board_service_id": "board-123"}
    asyncio.run(run())
    assert len(calls) == (1 if case == "missing-org" else 2)
    assert p.__dict__ == before
