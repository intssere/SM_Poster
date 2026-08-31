import asyncio
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import BackgroundTasks

from app.api.routes import catalog
from app.core.config import Settings
from app.integrations.shopify.auth import (
    ShopifyAuthenticationError,
    ShopifyConnectionStatus,
    ShopifyInsufficientScopesError,
    ShopifyNotConfiguredError,
    ShopifyTokenProvider,
    normalize_shop_domain,
)
from app.integrations.shopify.gateway import ShopifyConfig, ShopifyGraphQLGateway


def client_factory(handler):
    transport = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=transport)


def token_payload(token="dynamic-token", expires_in=3600, scope="read_products,read_inventory"):
    return {"access_token": token, "expires_in": expires_in, "scope": scope}


def test_client_credentials_token_acquisition_and_shop_normalization():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=token_payload(), request=request)

    provider = ShopifyTokenProvider(
        shop_domain="diamond-shelf",
        client_id="client-id",
        client_secret="client-secret",
        client_factory=client_factory(handler),
    )

    token = asyncio.run(provider.get_access_token())
    body = parse_qs(requests[0].content.decode())

    assert token == "dynamic-token"
    assert str(requests[0].url) == (
        "https://diamond-shelf.myshopify.com/admin/oauth/access_token"
    )
    assert body == {
        "grant_type": ["client_credentials"],
        "client_id": ["client-id"],
        "client_secret": ["client-secret"],
    }
    assert normalize_shop_domain("diamond-shelf.myshopify.com") == (
        "diamond-shelf.myshopify.com"
    )


def test_client_credentials_token_is_cached():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=token_payload(), request=request)

    provider = ShopifyTokenProvider(
        "shop",
        "client-id",
        "client-secret",
        client_factory=client_factory(handler),
    )

    first = asyncio.run(provider.get_access_token())
    second = asyncio.run(provider.get_access_token())

    assert first == second == "dynamic-token"
    assert calls == 1


def test_expiring_token_is_renewed():
    now = [1000.0]
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=token_payload(token=f"token-{calls}", expires_in=100),
            request=request,
        )

    provider = ShopifyTokenProvider(
        "shop",
        "client-id",
        "client-secret",
        client_factory=client_factory(handler),
        clock=lambda: now[0],
        refresh_skew_seconds=10,
    )

    assert asyncio.run(provider.get_access_token()) == "token-1"
    now[0] = 1085
    assert asyncio.run(provider.get_access_token()) == "token-1"
    now[0] = 1090
    assert asyncio.run(provider.get_access_token()) == "token-2"
    assert calls == 2


def test_authentication_failure_is_sanitized_and_can_retry():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                401,
                json={"error": "client-secret"},
                request=request,
            )
        return httpx.Response(200, json=token_payload(), request=request)

    provider = ShopifyTokenProvider(
        "shop",
        "client-id",
        "client-secret",
        client_factory=client_factory(handler),
    )

    with pytest.raises(ShopifyAuthenticationError) as exc:
        asyncio.run(provider.get_access_token())
    assert "client-secret" not in str(exc.value)
    assert asyncio.run(provider.connection_status()).status == "CONNECTED"
    assert calls == 2


def test_access_token_is_fallback_when_client_credentials_are_incomplete():
    provider = ShopifyTokenProvider(
        "shop",
        client_id="client-id",
        access_token="legacy-token",
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("Client Credentials request should not run")
        ),
    )

    assert provider.authentication_method == "ACCESS_TOKEN"
    assert asyncio.run(provider.get_access_token()) == "legacy-token"


def test_complete_client_credentials_take_priority_over_access_token():
    def handler(request):
        return httpx.Response(200, json=token_payload(), request=request)

    provider = ShopifyTokenProvider(
        "shop",
        "client-id",
        "client-secret",
        access_token="legacy-token",
        client_factory=client_factory(handler),
    )

    assert provider.authentication_method == "CLIENT_CREDENTIALS"
    assert asyncio.run(provider.get_access_token()) == "dynamic-token"


def test_missing_credentials_report_not_configured():
    provider = ShopifyTokenProvider(None)

    with pytest.raises(ShopifyNotConfiguredError):
        asyncio.run(provider.get_access_token())
    status = asyncio.run(provider.connection_status())
    assert status.status == "NOT_CONFIGURED"
    assert status.connected is False


def test_missing_required_scope_is_reported():
    def handler(request):
        return httpx.Response(
            200,
            json=token_payload(scope="read_products"),
            request=request,
        )

    provider = ShopifyTokenProvider(
        "shop",
        "client-id",
        "client-secret",
        client_factory=client_factory(handler),
    )

    with pytest.raises(ShopifyInsufficientScopesError):
        asyncio.run(provider.get_access_token())
    status = asyncio.run(provider.connection_status())
    assert status.status == "INSUFFICIENT_SCOPES"
    assert status.missing_scopes == ("read_inventory",)


def test_access_token_scope_error_is_classified_without_exposing_token():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "errors": [{
                    "message": (
                        "Access denied for products field. Required access: "
                        "read_products access scope."
                    ),
                }],
            },
            request=request,
        )

    gateway = ShopifyGraphQLGateway(
        ShopifyConfig(
            shop_domain="shop.myshopify.com",
            api_version="2026-07",
            access_token="legacy-token",
        ),
        http_client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    status = asyncio.run(gateway.connection_status())

    assert status.status == "INSUFFICIENT_SCOPES"
    assert status.missing_scopes == ("read_products",)
    assert "legacy-token" not in str(status.as_dict())


def test_access_token_authentication_failure_is_classified():
    def handler(request):
        return httpx.Response(401, request=request)

    gateway = ShopifyGraphQLGateway(
        ShopifyConfig(
            shop_domain="shop.myshopify.com",
            api_version="2026-07",
            access_token="legacy-token",
        ),
        http_client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    status = asyncio.run(gateway.connection_status())

    assert status.status == "AUTHENTICATION_FAILED"
    assert status.authentication_method == "ACCESS_TOKEN"


def test_client_credentials_graphql_401_invalidates_token_and_failed_refresh_stays_failed():
    token_calls = 0

    def token_handler(request):
        nonlocal token_calls
        token_calls += 1
        if token_calls == 1:
            return httpx.Response(200, json=token_payload(), request=request)
        return httpx.Response(401, request=request)

    def graphql_handler(request):
        return httpx.Response(401, request=request)

    provider = ShopifyTokenProvider(
        "shop",
        "client-id",
        "client-secret",
        client_factory=client_factory(token_handler),
    )
    gateway = ShopifyGraphQLGateway(
        ShopifyConfig(
            shop_domain="shop.myshopify.com",
            api_version="2026-07",
            token_provider=provider,
        ),
        http_client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(graphql_handler)
        ),
    )

    assert asyncio.run(provider.get_access_token()) == "dynamic-token"
    with pytest.raises(ShopifyAuthenticationError):
        asyncio.run(gateway._graphql("{ shop { id } }"))

    status = asyncio.run(gateway.connection_status())

    assert token_calls == 2
    assert status.status == "AUTHENTICATION_FAILED"
    assert status.connected is False


def test_authentication_failure_prevents_sync_job_creation(monkeypatch):
    class FailedGateway:
        async def connection_status(self):
            return ShopifyConnectionStatus(
                status="AUTHENTICATION_FAILED",
                authentication_method="ACCESS_TOKEN",
                message="Shopify authentication failed.",
            )

    monkeypatch.setattr(catalog, "_gateway", lambda settings: FailedGateway())
    monkeypatch.setattr(
        catalog,
        "CatalogSyncService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Sync service must not be created")
        ),
    )
    settings = Settings(
        DATABASE_URL="sqlite://",
        SHOPIFY_SHOP="shop",
        SHOPIFY_ACCESS_TOKEN="legacy-token",
    )

    response = asyncio.run(
        catalog.start_catalog_sync(BackgroundTasks(), settings)
    )

    assert response == {
        "accepted": False,
        "status": "AUTHENTICATION_FAILED",
        "message": "Shopify authentication failed.",
    }


def test_public_status_shape_never_contains_credentials():
    secret_values = ("client-id-value", "client-secret-value", "access-token-value")
    status = ShopifyConnectionStatus(
        status="CONNECTED",
        authentication_method="CLIENT_CREDENTIALS",
        message="Connected — Client Credentials",
        scopes=("read_inventory", "read_products"),
    ).as_dict()
    serialized = str(status)

    assert all(secret not in serialized for secret in secret_values)
    assert set(status) == {
        "status",
        "connected",
        "authentication_method",
        "message",
        "scopes",
        "missing_scopes",
    }


def test_status_endpoint_payload_does_not_expose_configured_secrets(monkeypatch):
    class FakeGateway:
        async def connection_status(self):
            return ShopifyConnectionStatus(
                status="CONNECTED",
                authentication_method="CLIENT_CREDENTIALS",
                message="Connected — Client Credentials",
                scopes=("read_inventory", "read_products"),
            )

    monkeypatch.setattr(catalog, "_gateway", lambda settings: FakeGateway())
    settings = Settings(
        DATABASE_URL="sqlite://",
        SHOPIFY_SHOP="diamond-shelf",
        SHOPIFY_CLIENT_ID="client-id-value",
        SHOPIFY_CLIENT_SECRET="client-secret-value",
        SHOPIFY_ACCESS_TOKEN="access-token-value",
    )

    payload = asyncio.run(catalog._shopify_connection_payload(settings))
    serialized = str(payload)

    assert payload["status"] == "CONNECTED"
    assert payload["authentication_method"] == "CLIENT_CREDENTIALS"
    assert "client-id-value" not in serialized
    assert "client-secret-value" not in serialized
    assert "access-token-value" not in serialized


def test_settings_accept_shopify_shop_and_legacy_domain_alias():
    primary = Settings(
        DATABASE_URL="sqlite://",
        SHOPIFY_SHOP="diamond-shelf",
    )
    legacy = Settings(
        DATABASE_URL="sqlite://",
        SHOPIFY_SHOP_DOMAIN="legacy.myshopify.com",
    )

    assert primary.shopify_shop == "diamond-shelf"
    assert legacy.shopify_shop == "legacy.myshopify.com"