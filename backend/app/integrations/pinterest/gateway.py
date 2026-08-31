from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx


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
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.api_base}/boards?page_size=250", headers=self.headers)
            response.raise_for_status()
            return response.json().get("items", [])

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
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{self.api_base}/pins", headers=self.headers, json=body)
            response.raise_for_status()
            return response.json()
