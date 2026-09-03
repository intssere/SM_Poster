import asyncio

import pytest

from app.integrations.pinterest import gateway as gateway_module
from app.integrations.pinterest.gateway import PinterestPinPayload, PinterestV5Gateway


def test_create_pin_disabled_blocks_before_http_client(monkeypatch):
    http_client_constructions = []

    def forbidden_async_client(*args, **kwargs):
        http_client_constructions.append((args, kwargs))
        raise AssertionError("httpx.AsyncClient must not be constructed while publishing is disabled")

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", forbidden_async_client)
    gateway = PinterestV5Gateway(
        access_token="test-secret-token",
        publishing_enabled=False,
    )
    payload = PinterestPinPayload(
        board_id="board123",
        title="Test Pin",
        description="Test description",
        link="https://diamondshelf.us/products/example",
        image_url="https://cdn.example.test/example.jpg",
        alt_text="Example product image",
    )

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(gateway.create_pin(payload))

    assert str(raised.value) == "Pinterest publishing is disabled by configuration"
    assert "test-secret-token" not in str(raised.value)
    assert http_client_constructions == []
