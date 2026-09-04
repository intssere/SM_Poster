"""Deterministic Pinterest publication quality checks for Task #39.

The service validates immutable ``PinPublication`` snapshots and local
provenance only.  It performs no AI work, no network fetches, and no provider
HTTP.  Provider hard limits are intentionally separated from Diamond Shelf
editorial policy and warning-only creative preferences.
"""
from __future__ import annotations

import ipaddress
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.models.domain import PinCreative, PinPublication, PinterestBoard

PINTEREST_QUALITY_V1 = "PINTEREST_QUALITY_V1"

# Pinterest/public Pin field limits.  Do not silently truncate.
PIN_TITLE_MAX = 100
PIN_DESCRIPTION_MAX = 800
PIN_ALT_TEXT_MAX = 500

RECOMMENDED_ASPECT_RATIO = 2 / 3
ASPECT_RATIO_TOLERANCE = 0.08
RECOMMENDED_WIDTH = 1000
RECOMMENDED_HEIGHT = 1500

CANONICAL_DESTINATION_HOSTS = {"diamondshelf.us", "www.diamondshelf.us", "diamondshelf.test"}
SHORTENER_HOSTS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly"}
UNSUPPORTED_CLAIM_PATTERNS = (
    re.compile(r"\bguaranteed\b", re.I),
    re.compile(r"\bcures?\b", re.I),
    re.compile(r"\bmiracle\b", re.I),
    re.compile(r"\b100%\s+effective\b", re.I),
    re.compile(r"\bbest\s+in\s+the\s+world\b", re.I),
)
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]{2,}")


@dataclass(frozen=True)
class QualityCheck:
    code: str
    severity: str
    passed: bool
    message: str
    safe_details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "passed": self.passed,
            "message": self.message,
            "safe_details": dict(self.safe_details),
        }


def _check(code: str, severity: str, passed: bool, message: str, **safe_details: Any) -> QualityCheck:
    return QualityCheck(code, severity, passed, message, {k: v for k, v in safe_details.items() if _safe_value(v)})


def _safe_value(value: Any) -> bool:
    if isinstance(value, str):
        lower = value.lower()
        forbidden = ("access_token", "refresh_token", "authorization", "bearer ", "client_secret", "ciphertext", "raw_body", "raw_json", "traceback")
        return not any(item in lower for item in forbidden)
    if isinstance(value, (int, float, bool)) or value is None:
        return True
    if isinstance(value, (list, tuple)):
        return all(_safe_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _safe_value(k) and _safe_value(v) for k, v in value.items())
    return False


def _has_control_chars(value: str | None) -> bool:
    return bool(value and CONTROL_CHAR_RE.search(value))


def _keyword_stuffed(value: str | None) -> bool:
    tokens = [token.lower() for token in TOKEN_RE.findall(value or "")]
    if len(tokens) < 8:
        return False
    counts = Counter(tokens)
    most_common = counts.most_common(1)[0][1]
    return most_common >= 5 or most_common / max(len(tokens), 1) >= 0.45


def _unsupported_claim(value: str | None) -> bool:
    return any(pattern.search(value or "") for pattern in UNSUPPORTED_CLAIM_PATTERNS)


def _text_checks(label: str, value: str | None, max_len: int, *, required: bool = True) -> list[QualityCheck]:
    checks: list[QualityCheck] = []
    present = isinstance(value, str) and bool(value.strip())
    checks.append(_check(f"{label}_REQUIRED", "FAIL", (not required) or present, f"{label.lower()} must be present"))
    if value is None:
        return checks
    checks.append(_check(f"{label}_WITHIN_PROVIDER_LIMIT", "FAIL", len(value) <= max_len, f"{label.lower()} must be within Pinterest provider limit", max_length=max_len, actual_length=len(value)))
    checks.append(_check(f"{label}_NO_CONTROL_CHARACTERS", "FAIL", not _has_control_chars(value), f"{label.lower()} must not contain control characters"))
    checks.append(_check(f"{label}_NO_KEYWORD_STUFFING", "FAIL", not _keyword_stuffed(value), f"{label.lower()} must not be keyword stuffed"))
    checks.append(_check(f"{label}_NO_UNSUPPORTED_CLAIMS", "FAIL", not _unsupported_claim(value), f"{label.lower()} must not contain deterministic unsupported claims"))
    return checks


def _alt_structure_checks(value: str | None) -> list[QualityCheck]:
    if not isinstance(value, str):
        return []
    stripped = value.strip()
    prompt_like = bool(re.search(r"\b(prompt|system|internal id|metadata)\b", stripped, re.I))
    url_only = stripped.startswith(("http://", "https://")) and " " not in stripped
    tokens = TOKEN_RE.findall(stripped)
    keyword_dump = len(tokens) >= 8 and "," in stripped and stripped.count(",") >= 4
    return [
        _check("ALT_NOT_URL_ONLY", "FAIL", not url_only, "alt text must not be URL-only"),
        _check("ALT_NO_INTERNAL_PROMPT_METADATA", "FAIL", not prompt_like, "alt text must not expose prompt or internal metadata"),
        _check("ALT_NOT_KEYWORD_DUMP", "FAIL", not keyword_dump, "alt text must not be a keyword dump"),
    ]


def _host_is_public(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    if normalized == "localhost" or normalized.endswith(".localhost") or normalized.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast)


def _url_checks(label: str, value: str | None, *, canonical_destination: bool) -> list[QualityCheck]:
    try:
        parsed = urlsplit(value or "")
    except ValueError:
        parsed = None
    host = parsed.hostname.lower().rstrip(".") if parsed and parsed.hostname else None
    checks = [
        _check(f"{label}_HTTPS", "FAIL", bool(parsed and parsed.scheme == "https"), f"{label.lower()} must use HTTPS"),
        _check(f"{label}_HOST_PRESENT", "FAIL", bool(host), f"{label.lower()} must include a hostname"),
        _check(f"{label}_NO_CREDENTIALS", "FAIL", bool(parsed) and not parsed.username and not parsed.password, f"{label.lower()} must not contain credentials"),
        _check(f"{label}_PUBLIC_HOST", "FAIL", bool(host and _host_is_public(host)), f"{label.lower()} must not target localhost or private/internal IPs"),
    ]
    if canonical_destination:
        checks.append(_check(f"{label}_CANONICAL_DIAMOND_SHELF_HOST", "FAIL", bool(host in CANONICAL_DESTINATION_HOSTS), f"{label.lower()} must use a canonical Diamond Shelf host", host=host or ""))
        checks.append(_check(f"{label}_NO_SHORTENER", "FAIL", bool(host and host not in SHORTENER_HOSTS), f"{label.lower()} must not use an unapproved shortener", host=host or ""))
        query_pairs = parse_qsl(parsed.query if parsed else "", keep_blank_values=True)
        utm_seen: dict[str, str] = {}
        conflict = False
        for key, item_value in query_pairs:
            if key.startswith("utm_"):
                if key in utm_seen and utm_seen[key] != item_value:
                    conflict = True
                utm_seen[key] = item_value
        checks.append(_check(f"{label}_NO_CONFLICTING_UTM_KEYS", "FAIL", not conflict, f"{label.lower()} must not contain duplicate conflicting UTM keys", utm_keys=sorted(utm_seen)))
    return checks


def _creative_checks(db: Any, publication: PinPublication) -> list[QualityCheck]:
    creative = db.get(PinCreative, publication.creative_id) if publication.creative_id else None
    checks = [
        _check("CREATIVE_PRESENT", "FAIL", creative is not None, "approved creative must exist"),
    ]
    if creative is None:
        return checks
    checks.extend([
        _check("CREATIVE_DRAFT_MATCH", "FAIL", creative.draft_id == publication.draft_id, "creative must belong to publication draft"),
        _check("SOURCE_IMAGE_MATCH", "FAIL", creative.source_image_id == publication.source_image_id, "creative source image must match immutable snapshot"),
        _check("CREATIVE_FINGERPRINT_MATCH", "FAIL", creative.creative_fingerprint == publication.creative_fingerprint, "creative fingerprint must match immutable snapshot"),
        _check("CREATIVE_DIMENSIONS_VALID", "FAIL", bool((creative.width or 0) > 0 and (creative.height or 0) > 0), "creative dimensions must be known and positive", width=creative.width, height=creative.height),
    ])
    if (creative.width or 0) > 0 and (creative.height or 0) > 0:
        ratio = creative.width / creative.height
        checks.append(_check("CREATIVE_ASPECT_RATIO_RECOMMENDED", "WARNING", abs(ratio - RECOMMENDED_ASPECT_RATIO) <= ASPECT_RATIO_TOLERANCE, "2:3 creative is recommended for Pinterest", width=creative.width, height=creative.height, recommended_width=RECOMMENDED_WIDTH, recommended_height=RECOMMENDED_HEIGHT))
    return checks


def _board_relevance_checks(db: Any, publication: PinPublication) -> list[QualityCheck]:
    board = db.get(PinterestBoard, publication.pinterest_board_record_id) if publication.pinterest_board_record_id else None
    if not board:
        return [_check("BOARD_RELEVANCE_UNKNOWN", "WARNING", False, "board relevance has no persisted Pinterest board context")]
    label = (board.routing_label or "").strip().lower()
    if not label:
        return [_check("BOARD_RELEVANCE_WEAK_SIGNAL", "WARNING", False, "board relevance has no local routing label", board_id=board.id)]
    text = " ".join((publication.title_snapshot or "", publication.description_snapshot or "")).lower()
    normalized_label = label.replace("_", " ")
    passed = any(part and part in text for part in normalized_label.split())
    return [_check("BOARD_RELEVANCE_PASS" if passed else "BOARD_ROUTING_LABEL_MISMATCH", "WARNING", passed, "board routing label should align with publication copy", routing_label=board.routing_label)]


def validate_publication_quality(db: Any, publication: PinPublication) -> dict[str, Any]:
    checks: list[QualityCheck] = []
    checks.extend(_text_checks("TITLE", publication.title_snapshot, PIN_TITLE_MAX))
    checks.extend(_text_checks("DESCRIPTION", publication.description_snapshot, PIN_DESCRIPTION_MAX))
    checks.extend(_text_checks("ALT", publication.alt_text_snapshot, PIN_ALT_TEXT_MAX))
    checks.extend(_alt_structure_checks(publication.alt_text_snapshot))
    checks.extend(_url_checks("DESTINATION_URL", publication.destination_url, canonical_destination=True))
    if publication.utm_url:
        checks.extend(_url_checks("UTM_URL", publication.utm_url, canonical_destination=True))
    checks.extend(_url_checks("MEDIA_URL", publication.media_url_snapshot, canonical_destination=False))
    checks.extend([
        _check("PUBLICATION_CREATIVE_ID_PRESENT", "FAIL", bool(publication.creative_id), "publication must snapshot creative identity"),
        _check("PUBLICATION_SOURCE_IMAGE_ID_PRESENT", "FAIL", bool(publication.source_image_id), "publication must snapshot source image identity"),
        _check("PUBLICATION_CREATIVE_FINGERPRINT_PRESENT", "FAIL", bool(publication.creative_fingerprint), "publication must snapshot creative fingerprint"),
        _check("PUBLICATION_TEMPLATE_PRESENT", "FAIL", bool(publication.template_id and publication.template_key), "publication must snapshot template identity"),
    ])
    checks.extend(_creative_checks(db, publication))
    checks.extend(_board_relevance_checks(db, publication))

    failed = any((not check.passed) and check.severity == "FAIL" for check in checks)
    warned = any((not check.passed) and check.severity == "WARNING" for check in checks)
    status = "FAIL" if failed else "WARNING" if warned else "PASS"
    return {
        "status": status,
        "policy_version": PINTEREST_QUALITY_V1,
        "checks": [check.as_dict() for check in checks],
    }
