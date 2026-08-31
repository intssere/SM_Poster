"""Reviewed, deterministic brand taxonomy used by ProductIntelligence.

This module is intentionally data-only and easy to review. A vendor is classified
only when its normalized name is present in one of these curated sets.
"""

from __future__ import annotations

import re


DESIGNER_BRANDS = frozenset({
    "burberry",
    "calvin klein",
    "carolina herrera",
    "dolce & gabbana",
    "givenchy",
    "gucci",
    "hermes",
    "paco rabanne",
    "versace",
})

NICHE_BRANDS = frozenset({
    "amouage",
    "creed",
    "mancera",
    "mind games",
    "montale",
    "parfums de marly",
    "xerjoff",
})

ARABIAN_BRANDS = frozenset({
    "afnan",
    "ajmal",
    "al haramain",
    "armaf",
    "lattafa",
    "rasasi",
    "swiss arabian",
})


def normalize_vendor_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def classify_vendor(vendor: str | None) -> dict[str, str | None]:
    """Return only classifications supported by the reviewed vendor taxonomy."""
    normalized = normalize_vendor_name(vendor)
    return {
        "designer": vendor.strip() if normalized in DESIGNER_BRANDS and vendor else None,
        "niche": vendor.strip() if normalized in NICHE_BRANDS and vendor else None,
        "arabian_classification": "arabian" if normalized in ARABIAN_BRANDS else None,
    }