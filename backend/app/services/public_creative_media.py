"""Offline, digest-addressed representation of persisted rendered PNGs."""
import hashlib
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import get_settings

PUBLIC_CREATIVE_PATH = re.compile(r"/api/pins/public-creatives/([A-Za-z0-9_-]{1,36})/([a-f0-9]{64})\.png\Z")


def public_origin(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or "?" in value or "#" in value
                or any(c.isspace() for c in value)):
            return None
        host = host.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host:
                return None
        return f"https://{parsed.netloc}".rstrip("/")
    except (ValueError, TypeError):
        return None


def public_creative_url(creative, *, settings=None):
    settings = settings or get_settings()
    origin = public_origin(settings.public_media_base_url)
    if (not origin or creative.render_status != "RENDERED"
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", creative.id or "")
            or not re.fullmatch(r"[a-f0-9]{64}", creative.sha256 or "")):
        return None
    return f"{origin}/api/pins/public-creatives/{creative.id}/{creative.sha256}.png"


def public_creative_url_matches(creative, value, *, settings=None):
    expected = public_creative_url(creative, settings=settings)
    return expected is not None and value == expected


def snapshot_media_url(creative, *, settings=None):
    if creative.rendered_url == f"/api/pins/creatives/{creative.id}/image":
        return public_creative_url(creative, settings=settings) or creative.rendered_url
    return creative.rendered_url


def verified_png(creative, digest, *, root=None):
    if (creative is None or creative.render_status != "RENDERED"
            or not re.fullmatch(r"[a-f0-9]{64}", digest) or creative.sha256 != digest
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", creative.id or "")):
        return None
    root = (root or Path(__file__).resolve().parents[2] / "generated-creatives").resolve()
    path = (root / f"{creative.id}.png").resolve()
    if path.parent != root:
        return None
    try:
        contents = path.read_bytes()
    except OSError:
        return None
    if not contents.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(contents).hexdigest() != digest:
        return None
    return contents
