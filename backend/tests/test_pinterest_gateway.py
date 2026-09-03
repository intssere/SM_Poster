import asyncio

import pytest

from app.integrations.pinterest import gateway as gateway_module
from app.integrations.pinterest.gateway import PinterestAmbiguousFailure, PinterestDefinitiveRejection, PinterestPinPayload, PinterestV5Gateway


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


def test_create_pin_http_4xx_is_definitive_rejection_without_leaks(monkeypatch):
    http_client_constructions = []
    post_calls = []

    class FakeResponse:
        status_code = 400
        text = "RAW_PROVIDER_BODY_DO_NOT_LEAK"

        def json(self):
            return {
                "message": "RAW_PROVIDER_BODY_DO_NOT_LEAK",
                "access_token": "provider-body-token",
            }

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, headers, json):
            post_calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    def fake_async_client(*args, **kwargs):
        http_client_constructions.append((args, kwargs))
        return FakeAsyncClient()

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", fake_async_client)
    gateway = PinterestV5Gateway(
        access_token="test-secret-token",
        publishing_enabled=True,
    )
    payload = PinterestPinPayload(
        board_id="board123",
        title="Test Pin",
        description="Test description",
        link="https://diamondshelf.us/products/example",
        image_url="https://cdn.example.test/example.jpg",
        alt_text="Example product image",
    )

    with pytest.raises(PinterestDefinitiveRejection) as raised:
        asyncio.run(gateway.create_pin(payload))

    assert raised.value.code == "PROVIDER_REJECTED"
    assert raised.value.status_code == 400
    assert str(raised.value) == "PROVIDER_REJECTED"
    assert len(http_client_constructions) == 1
    assert len(post_calls) == 1
    request = post_calls[0]
    assert request["url"] == "https://api.pinterest.com/v5/pins"
    assert request["headers"] == {
        "Authorization": "Bearer test-secret-token",
        "Content-Type": "application/json",
    }
    assert request["json"] == {
        "board_id": "board123",
        "title": "Test Pin",
        "description": "Test description",
        "link": "https://diamondshelf.us/products/example",
        "media_source": {
            "source_type": "image_url",
            "url": "https://cdn.example.test/example.jpg",
        },
        "alt_text": "Example product image",
    }
    exception_text = str(raised.value)
    assert "test-secret-token" not in exception_text
    assert "RAW_PROVIDER_BODY_DO_NOT_LEAK" not in exception_text
    assert "provider-body-token" not in exception_text


def test_create_pin_http_5xx_is_ambiguous_failure_without_leaks(monkeypatch):
    http_client_constructions = []
    post_calls = []

    class FakeResponse:
        status_code = 503
        text = "RAW_PROVIDER_503_BODY_DO_NOT_LEAK"

        def json(self):
            return {
                "message": "RAW_PROVIDER_503_BODY_DO_NOT_LEAK",
                "access_token": "provider-503-secret-token",
            }

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, headers, json):
            post_calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    def fake_async_client(*args, **kwargs):
        http_client_constructions.append((args, kwargs))
        return FakeAsyncClient()

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", fake_async_client)
    gateway = PinterestV5Gateway(
        access_token="test-secret-token",
        publishing_enabled=True,
    )
    payload = PinterestPinPayload(
        board_id="board123",
        title="Test Pin",
        description="Test description",
        link="https://diamondshelf.us/products/example",
        image_url="https://cdn.example.test/example.jpg",
        alt_text="Example product image",
    )

    with pytest.raises(PinterestAmbiguousFailure) as raised:
        asyncio.run(gateway.create_pin(payload))

    assert raised.value.code == "PROVIDER_AMBIGUOUS"
    assert raised.value.status_code == 503
    assert str(raised.value) == "PROVIDER_AMBIGUOUS"
    assert len(http_client_constructions) == 1
    assert len(post_calls) == 1
    request = post_calls[0]
    assert request["url"] == "https://api.pinterest.com/v5/pins"
    assert request["headers"] == {
        "Authorization": "Bearer test-secret-token",
        "Content-Type": "application/json",
    }
    assert request["json"] == {
        "board_id": "board123",
        "title": "Test Pin",
        "description": "Test description",
        "link": "https://diamondshelf.us/products/example",
        "media_source": {
            "source_type": "image_url",
            "url": "https://cdn.example.test/example.jpg",
        },
        "alt_text": "Example product image",
    }
    exception_text = str(raised.value)
    assert "test-secret-token" not in exception_text
    assert "RAW_PROVIDER_503_BODY_DO_NOT_LEAK" not in exception_text
    assert "provider-503-secret-token" not in exception_text


def test_create_pin_timeout_is_ambiguous_failure_without_retry_or_leaks(monkeypatch):
    http_client_constructions = []
    post_calls = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, headers, json):
            post_calls.append({"url": url, "headers": headers, "json": json})
            request = gateway_module.httpx.Request("POST", url)
            raise gateway_module.httpx.ReadTimeout(
                "RAW_TIMEOUT_DETAIL_DO_NOT_LEAK",
                request=request,
            )

    def fake_async_client(*args, **kwargs):
        http_client_constructions.append((args, kwargs))
        return FakeAsyncClient()

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", fake_async_client)
    gateway = PinterestV5Gateway(
        access_token="test-secret-token",
        publishing_enabled=True,
    )
    payload = PinterestPinPayload(
        board_id="board123",
        title="Test Pin",
        description="Test description",
        link="https://diamondshelf.us/products/example",
        image_url="https://cdn.example.test/example.jpg",
        alt_text="Example product image",
    )

    assert issubclass(gateway_module.httpx.ReadTimeout, gateway_module.httpx.TimeoutException)
    with pytest.raises(PinterestAmbiguousFailure) as raised:
        asyncio.run(gateway.create_pin(payload))

    assert raised.value.code == "PROVIDER_AMBIGUOUS"
    assert raised.value.status_code is None
    assert str(raised.value) == "PROVIDER_AMBIGUOUS"
    assert len(http_client_constructions) == 1
    assert len(post_calls) == 1
    request = post_calls[0]
    assert request["url"] == "https://api.pinterest.com/v5/pins"
    assert request["headers"] == {
        "Authorization": "Bearer test-secret-token",
        "Content-Type": "application/json",
    }
    assert request["json"] == {
        "board_id": "board123",
        "title": "Test Pin",
        "description": "Test description",
        "link": "https://diamondshelf.us/products/example",
        "media_source": {
            "source_type": "image_url",
            "url": "https://cdn.example.test/example.jpg",
        },
        "alt_text": "Example product image",
    }
    exception_text = str(raised.value)
    assert "test-secret-token" not in exception_text
    assert "RAW_TIMEOUT_DETAIL_DO_NOT_LEAK" not in exception_text
    assert "Authorization" not in exception_text
    assert "Bearer" not in exception_text
