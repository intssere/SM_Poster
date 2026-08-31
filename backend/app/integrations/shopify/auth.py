from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

REQUIRED_SHOPIFY_SCOPES = frozenset({"read_products", "read_inventory"})


class ShopifyAuthenticationError(RuntimeError):
    pass


class ShopifyInsufficientScopesError(RuntimeError):
    pass


class ShopifyNotConfiguredError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShopifyConnectionStatus:
    status: str
    authentication_method: str | None
    message: str
    scopes: tuple[str, ...] = ()
    missing_scopes: tuple[str, ...] = ()

    @property
    def connected(self) -> bool:
        return self.status == "CONNECTED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "connected": self.connected,
            "authentication_method": self.authentication_method,
            "message": self.message,
            "scopes": list(self.scopes),
            "missing_scopes": list(self.missing_scopes),
        }


@dataclass(frozen=True)
class _CachedToken:
    value: str
    expires_at: float
    scopes: frozenset[str]


def normalize_shop_domain(value: str | None) -> str | None:
    if not value:
        return None
    domain = value.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    if domain.endswith(".myshopify.com"):
        return domain
    return f"{domain}.myshopify.com"


class ShopifyTokenProvider:
    """Server-side Shopify token acquisition, caching, and renewal."""

    def __init__(
        self,
        shop_domain: str | None,
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        required_scopes: frozenset[str] = REQUIRED_SHOPIFY_SCOPES,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.time,
        refresh_skew_seconds: int = 60,
    ):
        self.shop_domain = normalize_shop_domain(shop_domain)
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.required_scopes = required_scopes
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30))
        self._clock = clock
        self._refresh_skew_seconds = refresh_skew_seconds
        self._cached_token: _CachedToken | None = None
        self._lock = asyncio.Lock()
        self._last_failure: ShopifyConnectionStatus | None = None

    @property
    def client_credentials_configured(self) -> bool:
        return bool(self.shop_domain and self.client_id and self.client_secret)

    @property
    def authentication_method(self) -> str | None:
        if self.client_credentials_configured:
            return "CLIENT_CREDENTIALS"
        if self.shop_domain and self.access_token:
            return "ACCESS_TOKEN"
        return None

    def missing_configuration(self) -> list[str]:
        if self.authentication_method:
            return []
        missing = []
        if not self.shop_domain:
            missing.append("SHOPIFY_SHOP")
        if not self.client_id:
            missing.append("SHOPIFY_CLIENT_ID")
        if not self.client_secret:
            missing.append("SHOPIFY_CLIENT_SECRET")
        if not self.access_token:
            missing.append("SHOPIFY_ACCESS_TOKEN")
        return missing

    async def get_access_token(self, force_refresh: bool = False) -> str:
        if self.authentication_method == "ACCESS_TOKEN":
            return self.access_token or ""
        if not self.client_credentials_configured:
            raise ShopifyNotConfiguredError("Shopify is not configured.")
        if (
            not force_refresh
            and self._last_failure
            and self._last_failure.status == "INSUFFICIENT_SCOPES"
        ):
            raise ShopifyInsufficientScopesError(
                "Shopify token is missing required access scopes."
            )

        now = self._clock()
        if (
            not force_refresh
            and self._cached_token
            and now < self._cached_token.expires_at - self._refresh_skew_seconds
        ):
            return self._cached_token.value

        async with self._lock:
            now = self._clock()
            if (
                not force_refresh
                and self._cached_token
                and now < self._cached_token.expires_at - self._refresh_skew_seconds
            ):
                return self._cached_token.value
            return await self._request_client_credentials_token()

    async def _request_client_credentials_token(self) -> str:
        endpoint = f"https://{self.shop_domain}/admin/oauth/access_token"
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
        except httpx.HTTPError as exc:
            self._last_failure = ShopifyConnectionStatus(
                status="AUTHENTICATION_FAILED",
                authentication_method="CLIENT_CREDENTIALS",
                message="Shopify authentication failed.",
            )
            raise ShopifyAuthenticationError(
                "Shopify authentication request failed."
            ) from exc

        if response.status_code >= 400:
            self._last_failure = ShopifyConnectionStatus(
                status="AUTHENTICATION_FAILED",
                authentication_method="CLIENT_CREDENTIALS",
                message="Shopify authentication failed.",
            )
            raise ShopifyAuthenticationError(
                f"Shopify authentication failed (HTTP {response.status_code})."
            )

        try:
            payload = response.json()
            token = str(payload["access_token"])
            expires_in = max(1, int(payload["expires_in"]))
        except (KeyError, TypeError, ValueError) as exc:
            self._last_failure = ShopifyConnectionStatus(
                status="AUTHENTICATION_FAILED",
                authentication_method="CLIENT_CREDENTIALS",
                message="Shopify returned an invalid authentication response.",
            )
            raise ShopifyAuthenticationError(
                "Shopify returned an invalid authentication response."
            ) from exc

        scopes = frozenset(
            scope.strip()
            for scope in str(payload.get("scope") or "").split(",")
            if scope.strip()
        )
        missing = tuple(sorted(self.required_scopes - scopes)) if scopes else ()
        self._cached_token = _CachedToken(
            value=token,
            expires_at=self._clock() + expires_in,
            scopes=scopes,
        )
        self._last_failure = None
        if missing:
            self._last_failure = ShopifyConnectionStatus(
                status="INSUFFICIENT_SCOPES",
                authentication_method="CLIENT_CREDENTIALS",
                message="Shopify token is missing required access scopes.",
                scopes=tuple(sorted(scopes)),
                missing_scopes=missing,
            )
            raise ShopifyInsufficientScopesError(
                f"Shopify token is missing required scopes: {', '.join(missing)}."
            )
        return token

    def record_authentication_failure(self) -> None:
        self._last_failure = ShopifyConnectionStatus(
            status="AUTHENTICATION_FAILED",
            authentication_method=self.authentication_method,
            message="Shopify authentication failed.",
        )

    def invalidate_cached_token(self) -> None:
        self._cached_token = None

    def record_scope_failure(self, missing_scopes: tuple[str, ...] = ()) -> None:
        self._last_failure = ShopifyConnectionStatus(
            status="INSUFFICIENT_SCOPES",
            authentication_method=self.authentication_method,
            message="Shopify token is missing required access scopes.",
            missing_scopes=missing_scopes,
        )

    async def connection_status(self) -> ShopifyConnectionStatus:
        method = self.authentication_method
        if not method:
            return ShopifyConnectionStatus(
                status="NOT_CONFIGURED",
                authentication_method=None,
                message="Shopify not configured.",
            )
        if self._last_failure and (
            self._last_failure.status == "INSUFFICIENT_SCOPES"
            or method == "ACCESS_TOKEN"
        ):
            return self._last_failure
        try:
            await self.get_access_token()
        except ShopifyInsufficientScopesError:
            return self._last_failure or ShopifyConnectionStatus(
                status="INSUFFICIENT_SCOPES",
                authentication_method=method,
                message="Shopify token is missing required access scopes.",
            )
        except ShopifyAuthenticationError:
            return self._last_failure or ShopifyConnectionStatus(
                status="AUTHENTICATION_FAILED",
                authentication_method=method,
                message="Shopify authentication failed.",
            )
        except ShopifyNotConfiguredError:
            return ShopifyConnectionStatus(
                status="NOT_CONFIGURED",
                authentication_method=None,
                message="Shopify not configured.",
            )

        scopes = self._cached_token.scopes if self._cached_token else ()
        label = "Client Credentials" if method == "CLIENT_CREDENTIALS" else "Access Token"
        return ShopifyConnectionStatus(
            status="CONNECTED",
            authentication_method=method,
            message=f"Connected — {label}",
            scopes=tuple(sorted(scopes)),
        )