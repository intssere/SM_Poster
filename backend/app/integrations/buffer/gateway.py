"""Isolated Buffer foundation. No persistence, dispatch wiring, or retries."""
from dataclasses import dataclass
from datetime import datetime
import ipaddress
import json
import re
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings


class BufferConfigurationError(RuntimeError):
    pass


class BufferReadError(RuntimeError):
    pass


class BufferDefinitiveRejection(RuntimeError):
    pass


class BufferAmbiguousFailure(RuntimeError):
    """Outcome cannot be proven; never automatically retry."""


def required(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BufferConfigurationError("BUFFER_SNAPSHOT_INCOMPLETE")
    return value


def public_url(value: str, *, https_only: bool = True) -> tuple:
    """Structural public-host check only; never resolves DNS or fetches media."""
    try:
        required(value)
        parsed = urlsplit(value)
        port = parsed.port  # May raise for malformed/out-of-range ports.
        host = (parsed.hostname or "").lower().rstrip(".")
        if (any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value
                or parsed.username is not None or parsed.password is not None
                or parsed.scheme not in ({"https"} if https_only else {"http", "https"})
                or not host or "%" in host or port == 0):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example"))
                    or host in {"example.com", "example.net", "example.org"}
                    or re.fullmatch(r"[0-9.]+", host)
                    or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        return parsed.scheme, host, port if port is not None else (443 if parsed.scheme == "https" else 80), parsed.path
    except (ValueError, TypeError, BufferConfigurationError):
        raise BufferConfigurationError("BUFFER_URL_INVALID") from None


@dataclass(frozen=True)
class BufferPinterestPostPayload:
    channel_id: str
    board_service_id: str
    title: str
    text: str
    url: str
    image_url: str
    alt_text: str

    def to_input(self) -> dict:
        for value in self.__dict__.values():
            required(value)
        public_url(self.url, https_only=False)
        public_url(self.image_url)
        return {
            "channelId": self.channel_id, "text": self.text,
            "schedulingType": "automatic", "mode": "shareNow", "needsApproval": False,
            "assets": [{"image": {"url": self.image_url, "metadata": {"altText": self.alt_text}}}],
            "metadata": {"pinterest": {"boardServiceId": self.board_service_id, "title": self.title, "url": self.url}},
        }


@dataclass(frozen=True)
class BufferPostResult:
    buffer_post_id: str
    status: str
    channel_id: str
    created_at: str | None
    due_at: str | None
    sent_at: str | None
    external_link: str | None


@dataclass(frozen=True)
class BufferPostSnapshot(BufferPostResult):
    channel_service: str
    text: str
    pinterest_board_service_id: str
    pinterest_title: str
    pinterest_url: str
    image_url: str
    image_alt_text: str


SINGLE_POST = """query ExactPost($input: PostInput!) {
  post(input: $input) {
    id status channelId channelService text createdAt dueAt sentAt externalLink
    metadata { ... on PinterestPostMetadata { board { serviceId } title url } }
    assets { __typename source ... on ImageAsset { image { altText } } }
  }
}"""


CHANNEL_FIELDS = """id name displayName service isDisconnected isLocked metadata {
  ... on PinterestMetadata { boards { serviceId name } }
}"""
POST_FIELDS = "id status channelId createdAt dueAt sentAt externalLink"
CREATE_POST = """mutation CreatePinterestPost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status channelId createdAt dueAt sentAt externalLink } }
    ... on MutationError { errorMessage: message }
  }
}"""
POST_STATUSES = frozenset({"draft", "error", "needs_approval", "scheduled", "sending", "sent"})
TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class BufferGateway:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        self._key = settings.buffer_api_key
        self._base = settings.buffer_api_base
        self._enabled = settings.publishing_enabled is True and settings.buffer_publishing_enabled is True
        self._channel = settings.buffer_pinterest_channel_id
        self._client = client

    async def _request(self, query: str, variables: dict, *, write: bool = False) -> dict:
        if write and not self._enabled:
            raise BufferConfigurationError("BUFFER_PUBLISHING_DISABLED")
        # Never send credentials to an alternate origin or follow redirects.
        if (self._base not in {"https://api.buffer.com", "https://api.buffer.com/"}
                or not isinstance(self._key, str) or not self._key.strip()
                or any(c.isspace() for c in self._key)):
            raise BufferConfigurationError("BUFFER_CONFIGURATION_REQUIRED")
        if self._key in json.dumps({"query": query, "variables": variables}):
            raise BufferConfigurationError("BUFFER_SECRET_IN_PAYLOAD")
        failure = BufferAmbiguousFailure if write else BufferReadError
        try:
            async def send(client):
                return await client.post(self._base, json={"query": query, "variables": variables},
                    headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                    timeout=TIMEOUT, follow_redirects=False)
            if self._client is None:
                async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
                    response = await send(client)
            else:
                response = await send(self._client)
            if response.status_code != 200:
                # Authentication rejection with no data cannot represent a success.
                if write and response.status_code in {400, 401, 403}:
                    body = response.json()
                    if (isinstance(body, dict) and not body.get("data") and body.get("errors")
                            and isinstance(body["errors"], list)
                            and all(isinstance(e, dict) and e.get("extensions", {}).get("code") in {
                                "GRAPHQL_PARSE_FAILED", "GRAPHQL_VALIDATION_FAILED", "UNAUTHORIZED", "FORBIDDEN"
                            } for e in body["errors"])):
                        raise BufferDefinitiveRejection("BUFFER_REQUEST_REJECTED")
                raise failure("BUFFER_RESPONSE_UNCERTAIN" if write else "BUFFER_READ_FAILED")
            body = response.json()
            if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
                raise failure("BUFFER_RESPONSE_UNCERTAIN" if write else "BUFFER_READ_FAILED")
            return body["data"]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise failure("BUFFER_RESPONSE_UNCERTAIN" if write else "BUFFER_READ_FAILED") from None

    def _text(self, value, *, identifier=False, content=False):
        if (not isinstance(value, str) or not value.strip() or len(value) > 2048
                or self._key and self._key in value or any(ord(c) < 32 and not (content and c in "\n\r\t") for c in value)
                or identifier and not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value)):
            raise ValueError
        return value

    def _channel_result(self, item):
        result = {k: self._text(item[k], identifier=k in {"id", "service"}) for k in ("id", "name", "service")}
        display_name = item.get("displayName")
        result["displayName"] = None if display_name is None else self._text(display_name)
        for key in ("isDisconnected", "isLocked"):
            if type(item[key]) is not bool:
                raise ValueError
            result[key] = item[key]
        result["boards"] = []
        if result["service"] == "pinterest":
            boards = item["metadata"]["boards"]
            if not isinstance(boards, list) or len(boards) > 1000:
                raise ValueError
            result["boards"] = [{"serviceId": self._text(b["serviceId"], identifier=True), "name": self._text(b["name"])} for b in boards]
        return result

    def _post_result(self, item, channel_id):
        post_id = self._text(item["id"], identifier=True)
        if item["status"] not in POST_STATUSES or item["channelId"] != channel_id:
            raise ValueError
        times = []
        for key in ("createdAt", "dueAt", "sentAt"):
            value = item.get(key)
            if value is not None:
                self._text(value)
                if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError
            times.append(value)
        link = item.get("externalLink")
        if link is not None:
            self._text(link)
            public_url(link, https_only=False)
        return BufferPostResult(post_id, item["status"], channel_id, *times, link)

    async def organizations(self) -> list[dict]:
        data = await self._request("query { account { organizations { id name } } }", {})
        try:
            rows = data["account"]["organizations"]
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError
            return [{"id": self._text(r["id"], identifier=True), "name": self._text(r["name"])} for r in rows]
        except (KeyError, TypeError, ValueError):
            raise BufferReadError("BUFFER_READ_INVALID") from None

    async def channels(self, organization_id: str) -> list[dict]:
        data = await self._request("query Channels($input: ChannelsInput!) { channels(input: $input) { " + CHANNEL_FIELDS + " } }",
                                   {"input": {"organizationId": required(organization_id)}})
        try:
            rows = data["channels"]
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError
            return [self._channel_result(r) for r in rows]
        except (KeyError, TypeError, ValueError):
            raise BufferReadError("BUFFER_READ_INVALID") from None

    async def channel(self, channel_id: str) -> dict:
        data = await self._request("query Channel($input: ChannelInput!) { channel(input: $input) { " + CHANNEL_FIELDS + " } }",
                                   {"input": {"id": required(channel_id)}})
        try:
            result = self._channel_result(data["channel"])
            if result["id"] != channel_id:
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError):
            raise BufferReadError("BUFFER_READ_INVALID") from None

    async def recent_posts(self, organization_id: str, channel_id: str, *, first: int = 20, status: str = "sent") -> list[BufferPostResult]:
        if type(first) is not int or not 1 <= first <= 100 or status not in POST_STATUSES:
            raise BufferConfigurationError("BUFFER_POST_FILTER_INVALID")
        data = await self._request("query Posts($input: PostsInput!, $first: Int!) { posts(input: $input, first: $first) { edges { node { " + POST_FIELDS + " } } } }",
            {"first": first, "input": {"organizationId": required(organization_id), "filter": {
                "channelIds": [required(channel_id)], "status": [status]}, "sort": [{"field": "createdAt", "direction": "desc"}]}})
        try:
            edges = data["posts"]["edges"]
            if not isinstance(edges, list) or len(edges) > first:
                raise ValueError
            posts = [self._post_result(e["node"], channel_id) for e in edges]
            if any(p.status != status for p in posts):
                raise ValueError
            return posts  # A bounded page, never duplicate-safe authorization.
        except (KeyError, TypeError, ValueError, BufferConfigurationError):
            raise BufferReadError("BUFFER_READ_INVALID") from None

    async def create_pinterest_post(self, payload: BufferPinterestPostPayload) -> BufferPostResult:
        if not self._enabled:
            raise BufferConfigurationError("BUFFER_PUBLISHING_DISABLED")
        if not self._channel or payload.channel_id != self._channel:
            raise BufferConfigurationError("BUFFER_CHANNEL_MISMATCH")
        data = await self._request(CREATE_POST, {"input": payload.to_input()}, write=True)
        try:
            result = data["createPost"]
            if (result.get("__typename") != "PostActionSuccess" and "post" not in result
                    and isinstance(result.get("errorMessage"), str)):
                raise BufferDefinitiveRejection("BUFFER_MUTATION_REJECTED")
            if result.get("__typename") != "PostActionSuccess" or "errorMessage" in result:
                raise ValueError
            return self._post_result(result["post"], payload.channel_id)
        except (KeyError, TypeError, ValueError, AttributeError, BufferConfigurationError):
            raise BufferAmbiguousFailure("BUFFER_RESPONSE_UNCERTAIN") from None

    async def post(self, post_id: str) -> BufferPostSnapshot:
        """One exact read. Never scan history, retry, or return raw provider data."""
        try:
            self._text(post_id, identifier=True)
        except (ValueError, TypeError):
            raise BufferReadError("BUFFER_OPERATION_ID_REQUIRED") from None
        data = await self._request(SINGLE_POST, {"input": {"id": post_id}})
        try:
            item = data["post"]
            channel_id = self._text(item["channelId"], identifier=True)
            result = self._post_result(item, channel_id)
            if result.buffer_post_id != post_id or item["channelService"] != "pinterest":
                raise ValueError
            metadata, assets = item["metadata"], item["assets"]
            if not isinstance(assets, list) or len(assets) != 1 or assets[0]["__typename"] != "ImageAsset":
                raise ValueError
            url = self._text(metadata["url"])
            image_url = self._text(assets[0]["source"])
            public_url(url)
            public_url(image_url)
            return BufferPostSnapshot(**result.__dict__, channel_service="pinterest",
                text=self._text(item["text"], content=True),
                pinterest_board_service_id=self._text(metadata["board"]["serviceId"], identifier=True),
                pinterest_title=self._text(metadata["title"]), pinterest_url=url,
                image_url=image_url, image_alt_text=self._text(assets[0]["image"]["altText"]))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, BufferConfigurationError):
            raise BufferReadError("BUFFER_READ_INVALID") from None
