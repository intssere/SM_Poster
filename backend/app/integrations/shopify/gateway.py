from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from typing import Any, AsyncIterator, Callable

import httpx

from app.integrations.shopify.auth import (
    ShopifyAuthenticationError,
    ShopifyConnectionStatus,
    ShopifyInsufficientScopesError,
    ShopifyTokenProvider,
)


@dataclass
class ShopifyConfig:
    shop_domain: str
    api_version: str
    access_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_provider: ShopifyTokenProvider | None = None


class ShopifyGateway(ABC):
    @abstractmethod
    async def start_catalog_bulk_export(self) -> str: ...

    @abstractmethod
    async def get_bulk_operation(self, operation_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def download_bulk_result(self, url: str) -> AsyncIterator[dict[str, Any]]: ...


class ShopifyGraphQLGateway(ShopifyGateway):
    """Thin Shopify GraphQL boundary. No storefront scraping."""

    def __init__(
        self,
        config: ShopifyConfig,
        http_client_factory: Callable[[int], httpx.AsyncClient] | None = None,
    ):
        self.config = config
        self.endpoint = f"https://{config.shop_domain}/admin/api/{config.api_version}/graphql.json"
        self.auth = config.token_provider or ShopifyTokenProvider(
            shop_domain=config.shop_domain,
            client_id=config.client_id,
            client_secret=config.client_secret,
            access_token=config.access_token,
        )
        self._http_client_factory = http_client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )

    async def connection_status(self) -> ShopifyConnectionStatus:
        status = await self.auth.connection_status()
        if not status.connected or status.authentication_method != "ACCESS_TOKEN":
            return status
        query = """query ConnectionStatus {
          currentAppInstallation { accessScopes { handle } }
        }"""
        try:
            data = await self._graphql(query)
        except (ShopifyAuthenticationError, ShopifyInsufficientScopesError):
            return await self.auth.connection_status()
        scopes = frozenset(
            item.get("handle")
            for item in (
                data.get("currentAppInstallation", {}).get("accessScopes", [])
                if data.get("currentAppInstallation")
                else []
            )
            if item.get("handle")
        )
        missing = tuple(sorted(self.auth.required_scopes - scopes))
        if missing:
            self.auth.record_scope_failure(missing)
            return await self.auth.connection_status()
        return ShopifyConnectionStatus(
            status="CONNECTED",
            authentication_method="ACCESS_TOKEN",
            message="Connected — Access Token",
            scopes=tuple(sorted(scopes)),
        )

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        token = await self.auth.get_access_token()
        try:
            async with self._http_client_factory(30) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "X-Shopify-Access-Token": token,
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "variables": variables or {}},
                )
        except httpx.HTTPError as exc:
            raise RuntimeError("Shopify Admin API request failed.") from exc

        if response.status_code == 401:
            if self.auth.authentication_method == "CLIENT_CREDENTIALS":
                self.auth.invalidate_cached_token()
            self.auth.record_authentication_failure()
            raise ShopifyAuthenticationError("Shopify rejected the Admin API token.")
        if response.status_code == 403:
            self.auth.record_scope_failure()
            raise ShopifyInsufficientScopesError(
                "Shopify token is missing required access scopes."
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            messages = [
                str(error.get("message", "")).lower()
                for error in payload["errors"]
                if isinstance(error, dict)
            ]
            if any("access denied" in message or "scope" in message for message in messages):
                combined = " ".join(messages)
                missing = tuple(
                    sorted(
                        scope
                        for scope in self.auth.required_scopes
                        if scope.lower() in combined
                    )
                )
                self.auth.record_scope_failure(missing)
                raise ShopifyInsufficientScopesError(
                    "Shopify token is missing required access scopes."
                )
            raise RuntimeError("Shopify Admin API returned GraphQL errors.")
        return payload["data"]

    async def start_catalog_bulk_export(self) -> str:
        product_query = r'''{
          products {
            edges {
              node {
                id handle title vendor productType status tags
                onlineStoreUrl
                totalInventory
                 createdAt updatedAt
                 collections(first: 100) { edges { node { __typename id title handle } } }
                 metafields(first: 50) { edges { node { __typename namespace key value type } } }
                 variants { edges { node { __typename id sku title price compareAtPrice inventoryQuantity } } }
                 media { edges { node { __typename ... on MediaImage { id image { url width height altText } } } } }
              }
            }
          }
        }'''
        mutation = '''mutation Bulk($query: String!) {
          bulkOperationRunQuery(query: $query, groupObjects: true) {
            bulkOperation { id status }
            userErrors { field message }
          }
        }'''
        data = await self._graphql(mutation, {"query": product_query})
        errors = data["bulkOperationRunQuery"]["userErrors"]
        if errors:
            raise RuntimeError(f"Shopify bulk export rejected: {errors}")
        return data["bulkOperationRunQuery"]["bulkOperation"]["id"]

    async def get_bulk_operation(self, operation_id: str) -> dict[str, Any]:
        query = '''query BulkNode($id: ID!) {
          node(id: $id) {
            ... on BulkOperation { id status errorCode objectCount url partialDataUrl createdAt completedAt }
          }
        }'''
        data = await self._graphql(query, {"id": operation_id})
        return data["node"]

    async def download_bulk_result(self, url: str) -> AsyncIterator[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.strip():
                        yield json.loads(line)
