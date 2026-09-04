from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

class PinterestDefinitiveRejection(RuntimeError):
    def __init__(self, code="PROVIDER_REJECTED", status_code=None):
        self.code, self.status_code = code, status_code
        super().__init__(code)

class PinterestAmbiguousFailure(RuntimeError):
    def __init__(self, code="PROVIDER_AMBIGUOUS", status_code=None):
        self.code, self.status_code = code, status_code
        super().__init__(code)


@dataclass
class PinterestPinPayload:
    board_id: str
    title: str
    description: str
    link: str
    image_url: str
    alt_text: str | None = None


class PinterestGateway(ABC):
    @abstractmethod
    async def list_boards(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def create_pin(self, payload: PinterestPinPayload) -> dict[str, Any]: ...


class PinterestV5Gateway(PinterestGateway):
    """Official API v5 boundary. Keep all provider semantics inside this adapter."""

    def __init__(self, *, access_token: str, api_base: str = "https://api.pinterest.com/v5", publishing_enabled: bool = False):
        self.access_token = access_token
        self.api_base = api_base.rstrip("/")
        self.publishing_enabled = publishing_enabled

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    async def list_boards(self) -> list[dict[str, Any]]:
        timeout = httpx.Timeout(30.0, connect=10.0, read=30.0, write=30.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{self.api_base}/boards?page_size=250", headers=self.headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise PinterestAmbiguousFailure() from None
        if response.status_code >= 400:
            if response.status_code < 500: raise PinterestDefinitiveRejection(status_code=response.status_code)
            raise PinterestAmbiguousFailure(status_code=response.status_code)
        try:
            return response.json().get("items", [])
        except Exception:
            raise PinterestAmbiguousFailure() from None

    async def create_pin(self, payload: PinterestPinPayload) -> dict[str, Any]:
        if not self.publishing_enabled:
            raise RuntimeError("Pinterest publishing is disabled by configuration")
        body = {
            "board_id": payload.board_id,
            "title": payload.title,
            "description": payload.description,
            "link": payload.link,
            "media_source": {"source_type": "image_url", "url": payload.image_url},
        }
        if payload.alt_text:
            body["alt_text"] = payload.alt_text
        timeout = httpx.Timeout(45.0, connect=10.0, read=45.0, write=45.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{self.api_base}/pins", headers=self.headers, json=body)
        except (httpx.TimeoutException, httpx.TransportError):
            raise PinterestAmbiguousFailure() from None
        if response.status_code >= 400:
            if response.status_code < 500: raise PinterestDefinitiveRejection(status_code=response.status_code)
            raise PinterestAmbiguousFailure(status_code=response.status_code)
        try: return response.json()
        except Exception: raise PinterestAmbiguousFailure() from None
