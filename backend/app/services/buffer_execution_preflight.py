"""Server-owned, read-only evidence collection for one Buffer pilot dispatch."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import Settings, get_settings
from app.integrations.buffer.gateway import BufferGateway, public_url, required
from app.models.domain import PinPublication
from app.services.buffer_pilot_execution_gate import BufferPilotExecutionEvidence
from app.services.buffer_pilot_activation import active_activation, validate_activation
from app.services.buffer_pinterest_adapter import verify_destination
from app.services.buffer_single_pin_pilot import validate_pilot
from app.services.publication_dispatch_authorization import active_authorization, validate_authorization
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import normalize_persisted_utc


MAX_MEDIA_BYTES = 10 * 1024 * 1024
MEDIA_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


class BufferPreflightError(RuntimeError):
    pass


async def _resolve_public_host(host: str, port: int, *, resolver=None) -> str:
    resolver = resolver or asyncio.get_running_loop().getaddrinfo
    try:
        rows = await resolver(host, port, type=socket.SOCK_STREAM)
        addresses = {row[4][0].split("%", 1)[0] for row in rows}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError
        return sorted(addresses)[0]
    except (OSError, TypeError, ValueError):
        raise BufferPreflightError("BUFFER_MEDIA_DESTINATION_UNSAFE") from None


async def verify_live_image(
    media_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    resolver=None,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> None:
    try:
        scheme, host, port, _ = public_url(required(media_url), https_only=True)
    except Exception:
        raise BufferPreflightError("BUFFER_MEDIA_DESTINATION_UNSAFE") from None
    if scheme != "https":
        raise BufferPreflightError("BUFFER_MEDIA_DESTINATION_UNSAFE")
    address = await _resolve_public_host(host, port, resolver=resolver)
    parsed = urlsplit(media_url)
    pinned_host = f"[{address}]" if ":" in address else address
    pinned_url = urlunsplit((parsed.scheme, f"{pinned_host}:{port}", parsed.path, parsed.query, ""))
    host_header = host if port == 443 else f"{host}:{port}"
    owned = client is None
    client = client or httpx.AsyncClient(timeout=MEDIA_TIMEOUT, follow_redirects=False, trust_env=False)
    try:
        async with client.stream(
            "GET", pinned_url, headers={"Host": host_header},
            extensions={"sni_hostname": host},
        ) as response:
            if 300 <= response.status_code < 400:
                raise BufferPreflightError("BUFFER_MEDIA_REDIRECT_REJECTED")
            if response.status_code < 200 or response.status_code >= 300:
                raise BufferPreflightError("BUFFER_MEDIA_FETCH_FAILED")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                raise BufferPreflightError("BUFFER_MEDIA_NOT_IMAGE")
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) < 0 or int(length) > max_bytes:
                        raise BufferPreflightError("BUFFER_MEDIA_TOO_LARGE")
                except ValueError:
                    raise BufferPreflightError("BUFFER_MEDIA_FETCH_INVALID") from None
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > max_bytes:
                    raise BufferPreflightError("BUFFER_MEDIA_TOO_LARGE")
            if received == 0:
                raise BufferPreflightError("BUFFER_MEDIA_FETCH_INVALID")
    except BufferPreflightError:
        raise
    except (httpx.TimeoutException, httpx.TransportError):
        raise BufferPreflightError("BUFFER_MEDIA_FETCH_FAILED") from None
    finally:
        if owned:
            await client.aclose()


async def build_buffer_execution_evidence(
    db,
    publication: PinPublication,
    *,
    settings: Settings | None = None,
    gateway: BufferGateway | None = None,
    media_client: httpx.AsyncClient | None = None,
    resolver=None,
    now: datetime | None = None,
) -> BufferPilotExecutionEvidence:
    settings = settings or get_settings()
    if not isinstance(settings.buffer_api_key, str) or not settings.buffer_api_key.strip():
        raise BufferPreflightError("BUFFER_CONFIGURATION_REQUIRED")
    authorization = active_authorization(db, publication.id)
    activation_ok, _ = validate_activation(db, publication, active_activation(db), now=now)
    authorized = validate_authorization(
        db, publication, authorization, now=now, dispatch_provider="buffer",
    )
    pilot_ok, _ = validate_pilot(db, publication, settings)
    if not authorized["valid"] or not pilot_ok or not activation_ok:
        raise BufferPreflightError("BUFFER_PREFLIGHT_NOT_AUTHORIZED")
    gateway = gateway or BufferGateway(settings)
    try:
        destination = await verify_destination(publication, settings, gateway)
        await verify_live_image(publication.media_url_snapshot, client=media_client, resolver=resolver)
    except BufferPreflightError:
        raise
    except Exception:
        raise BufferPreflightError("BUFFER_PREFLIGHT_FAILED") from None
    observed_at = normalize_persisted_utc(now or datetime.now(timezone.utc))
    return BufferPilotExecutionEvidence(
        publication_id=publication.id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        buffer_organization_id=destination["organization_id"],
        buffer_pinterest_channel_id=destination["channel_id"],
        board_service_id=destination["board_service_id"],
        media_url=publication.media_url_snapshot,
        observed_at=observed_at,
        write_credential_authorized=authorized["valid"] and pilot_ok,
        provider_destination_live_verified=True,
        media_live_fetch_verified=True,
    )