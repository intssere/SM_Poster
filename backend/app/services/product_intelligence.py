from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any, AsyncIterator

from app.services.product_taxonomy import classify_vendor


CATEGORY_FRAGRANCE = "fragrance"
CATEGORY_GIFT_SET = "gift_set"
CATEGORY_BATH_BODY = "bath_body"
CATEGORY_BEAUTY = "beauty"
CATEGORY_HOME_FRAGRANCE = "home_fragrance"
CATEGORY_OTHER = "other"

NORMALIZATION_FIELDS = (
    "brand",
    "audience",
    "designer",
    "niche",
    "arabian_classification",
    "fragrance_family",
    "fragrance_notes",
    "concentration",
    "size",
    "price_band",
    "gift_suitability",
    "season",
    "occasion",
)

# These rules deliberately do not require optional enrichment such as designer,
# niche, notes, season, occasion, or size when the source does not provide it.
CATEGORY_REQUIRED_FIELDS = {
    CATEGORY_FRAGRANCE: ("brand", "audience", "concentration", "fragrance_family", "price_band"),
    CATEGORY_GIFT_SET: ("brand", "gift_suitability", "price_band"),
    CATEGORY_BATH_BODY: ("brand", "price_band"),
    CATEGORY_BEAUTY: ("brand", "price_band"),
    CATEGORY_HOME_FRAGRANCE: ("brand", "price_band"),
    CATEGORY_OTHER: ("brand", "price_band"),
}

_SIZE_PATTERN = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*[-]?\s*"
    r"(?P<unit>fluid\s+ounces?|fl\.?\s*oz|ounces?|oz|ml|grams?|g|pieces?|pcs?)\b",
    re.IGNORECASE,
)

_CONCENTRATION_PATTERNS = (
    ("Extrait de Parfum", re.compile(r"\bextrait\s+de\s+parfum\b", re.IGNORECASE)),
    ("Eau de Parfum", re.compile(r"\b(?:eau\s+de\s+parfum|edp)\b", re.IGNORECASE)),
    ("Eau de Toilette", re.compile(r"\b(?:eau\s+de\s+toilette|edt)\b", re.IGNORECASE)),
    ("Eau de Cologne", re.compile(r"\b(?:eau\s+de\s+cologne|edc)\b", re.IGNORECASE)),
    ("Perfumed Deodorant", re.compile(r"\bperfumed\s+deodorant\b", re.IGNORECASE)),
    ("Perfume Oil", re.compile(r"\bperfume\s+oil\b", re.IGNORECASE)),
    ("Body Mist", re.compile(r"\bbody\s+mist\b", re.IGNORECASE)),
    ("Body Spray", re.compile(r"\bbody\s+spray\b", re.IGNORECASE)),
    ("After Shave", re.compile(r"\bafter[-\s]?shave\b", re.IGNORECASE)),
    ("Parfum", re.compile(r"\bparfum\b", re.IGNORECASE)),
    ("Cologne", re.compile(r"\bcologne\b", re.IGNORECASE)),
)

_FAMILY_PATTERNS = (
    ("Aromatic & Fougère", re.compile(r"\baromatic\s*(?:&|and)\s*foug[eè]re\b", re.IGNORECASE)),
    ("Floral", re.compile(r"\bfloral\b", re.IGNORECASE)),
    ("Woody", re.compile(r"\bwoody\b", re.IGNORECASE)),
    ("Amber", re.compile(r"\bamber\b", re.IGNORECASE)),
    ("Fresh", re.compile(r"\bfresh\b", re.IGNORECASE)),
    ("Citrus", re.compile(r"\bcitrus\b", re.IGNORECASE)),
    ("Fruity", re.compile(r"\bfruity\b", re.IGNORECASE)),
    ("Gourmand", re.compile(r"\bgourmand\b", re.IGNORECASE)),
    ("Aquatic", re.compile(r"\baquatic\b", re.IGNORECASE)),
    ("Green", re.compile(r"\bgreen\b", re.IGNORECASE)),
    ("Musky", re.compile(r"\bmusky\b", re.IGNORECASE)),
    ("Spicy", re.compile(r"\bspicy\b", re.IGNORECASE)),
    ("Leather", re.compile(r"\bleather\b", re.IGNORECASE)),
    ("Chypre", re.compile(r"\bchypre\b", re.IGNORECASE)),
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _metafields(product: dict[str, Any]) -> dict[str, str]:
    fields = product.get("metafields", product.get("_metafields", []))
    if isinstance(fields, dict):
        return {
            str(key).lower(): str(value)
            for key, value in fields.items()
            if value is not None
        }
    values: dict[str, str] = {}
    for item in fields or []:
        if "node" in item:
            item = item["node"]
        namespace = _text(item.get("namespace")) or ""
        key = _text(item.get("key"))
        value = _text(item.get("value"))
        if key and value is not None:
            values[f"{namespace}.{key}".lower()] = value
            values.setdefault(key.lower(), value)
    return values


def _field(fields: dict[str, str], *names: str) -> str | None:
    for name in names:
        if name.lower() in fields:
            return fields[name.lower()]
    return None


def _values(product: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = product.get(key, product.get(f"_{key}", []))
    if isinstance(value, dict):
        value = value.get("edges", value.get("nodes", []))
    result = []
    for item in value or []:
        result.append(item.get("node", item) if isinstance(item, dict) else item)
    return result


def _tags(product: dict[str, Any]) -> set[str]:
    return {str(tag).strip().lower() for tag in product.get("tags", []) if str(tag).strip()}


def _tag_value(tags: set[str], *values: str) -> str | None:
    wanted = {value.lower() for value in values}
    for tag in tags:
        if tag in wanted:
            return tag
    return None


def _collection_titles(product: dict[str, Any]) -> list[str]:
    collections = product.get("collections", product.get("_collections", []))
    if isinstance(collections, dict):
        collections = collections.get("edges", collections.get("nodes", []))
    titles = []
    for item in collections or []:
        if isinstance(item, dict):
            item = item.get("node", item)
            title = _text(item.get("title"))
        else:
            title = _text(item)
        if title:
            titles.append(title)
    return titles


def _canonical_audience(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    lowered = text.lower()
    if re.search(r"\bunisex\b", lowered):
        return "unisex"
    if re.search(r"\b(?:kids?|children|childrens?)\b", lowered):
        return "kids"
    if re.search(r"\b(?:women|woman|womens|female|ladies)\b", lowered):
        return "women"
    if re.search(r"\b(?:men|man|mens|male)\b", lowered):
        return "men"
    return None


def _audience(
    product: dict[str, Any],
    fields: dict[str, str],
    tags: set[str],
    collections: list[str],
) -> str | None:
    for value in (_field(fields, "gender", "audience"),):
        result = _canonical_audience(value)
        if result:
            return result

    for tag in sorted(tags):
        if tag.startswith(("gender:", "audience:")):
            result = _canonical_audience(tag.split(":", 1)[1])
            if result:
                return result
        result = _canonical_audience(tag)
        if result and tag in {"men", "man", "male", "women", "woman", "female", "unisex", "kids", "kid"}:
            return result

    for collection in collections:
        result = _canonical_audience(collection)
        if result:
            return result
    return _canonical_audience(product.get("title"))


def _canonical_concentration(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    for canonical, pattern in _CONCENTRATION_PATTERNS:
        if pattern.search(text):
            return canonical
    return None


def _concentration(
    product: dict[str, Any],
    fields: dict[str, str],
    tags: set[str],
) -> str | None:
    field_value = _field(fields, "concentration")
    result = _canonical_concentration(field_value)
    if result:
        return result
    for tag in sorted(tags):
        if tag.startswith(("concentration:", "strength:")):
            result = _canonical_concentration(tag.split(":", 1)[1])
            if result:
                return result
    return _canonical_concentration(product.get("title"))


def _normalize_size_match(match: re.Match[str]) -> tuple[str, float | int, str]:
    raw_value = match.group("value").replace(",", ".")
    numeric = float(raw_value) if "." in raw_value else int(raw_value)
    unit = re.sub(r"\s+", " ", match.group("unit").lower().replace(".", "")).strip()
    if unit in {"fluid ounces", "fl oz", "ounces", "oz"}:
        unit = "fl oz" if "fluid" in unit or unit == "fl oz" else "oz"
    elif unit in {"grams", "gram", "g"}:
        unit = "g"
    elif unit in {"pieces", "piece", "pcs"}:
        unit = "pieces"
    else:
        unit = "ml"
    display_value = str(numeric).removesuffix(".0")
    return f"{display_value} {unit}", numeric, unit


def _extract_size(value: Any) -> tuple[str | None, str | None, float | int | None, str | None]:
    source = _text(value)
    if not source:
        return None, None, None, None
    match = _SIZE_PATTERN.search(source)
    if not match:
        return None, None, None, None
    normalized, numeric, unit = _normalize_size_match(match)
    return normalized, match.group(0), numeric, unit


def _size(
    product: dict[str, Any],
    fields: dict[str, str],
    tags: set[str],
    variants: list[dict[str, Any]],
) -> tuple[str | None, str | None, float | int | None, str | None]:
    sources: list[Any] = [
        _field(fields, "size", "volume", "net_content", "content"),
    ]
    sources.extend(variant.get("title") for variant in variants)
    sources.extend(sorted(tags))
    sources.append(product.get("title"))
    for source in sources:
        normalized, raw, numeric, unit = _extract_size(source)
        if normalized:
            return normalized, raw, numeric, unit
    return None, None, None, None


def _is_gift_set(
    product: dict[str, Any],
    fields: dict[str, str],
    tags: set[str],
    collections: list[str],
) -> str | None:
    explicit_field = _field(fields, "gift_suitability", "gift")
    if explicit_field and explicit_field.lower() not in {"false", "no", "0"}:
        return "gift_set"

    sources = [
        _text(product.get("productType")),
        _text(product.get("title")),
        *sorted(tags),
        *collections,
    ]
    for source in sources:
        if source and re.search(r"\bgift\s*sets?\b|\b\d+\s*[- ]?piece\b|\bset\b", source, re.IGNORECASE):
            return "gift_set"
    return None


def _fragrance_family(
    product: dict[str, Any],
    fields: dict[str, str],
    tags: set[str],
    collections: list[str],
) -> str | None:
    explicit = _field(fields, "fragrance_family", "fragrance.family", "family")
    if explicit:
        return explicit
    sources = [
        tag.split(":", 1)[1] if ":" in tag and tag.split(":", 1)[0] in {"family", "fragrance family"} else tag
        for tag in sorted(tags)
    ]
    sources.extend(collections)
    for source in sources:
        for canonical, pattern in _FAMILY_PATTERNS:
            if pattern.search(source):
                return canonical
    return None


def _category(
    product: dict[str, Any],
    tags: set[str],
    collections: list[str],
    gift_suitability: str | None,
) -> str:
    product_type = (_text(product.get("productType", product.get("product_type"))) or "").lower()
    taxonomy = " ".join([product_type, *[tag.lower() for tag in tags], *[item.lower() for item in collections]])
    if gift_suitability:
        return CATEGORY_GIFT_SET
    if any(term in taxonomy for term in ("home fragrance", "candle", "diffuser")):
        return CATEGORY_HOME_FRAGRANCE
    if any(term in taxonomy for term in ("bath & body", "bath and body", "body lotion", "body wash", "deodorant")):
        return CATEGORY_BATH_BODY
    if any(term in taxonomy for term in ("beauty", "skin care", "skincare", "hair", "cosmetic", "eye care", "nail care")):
        return CATEGORY_BEAUTY
    if any(term in taxonomy for term in ("fragrance", "perfume", "parfum", "cologne", "after shave")):
        return CATEGORY_FRAGRANCE
    return CATEGORY_OTHER


def _money(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _image_quality(images: list[dict[str, Any]]) -> float:
    if not images:
        return 0.0
    image = images[0].get("image", images[0])
    width = image.get("width") or 0
    height = image.get("height") or 0
    try:
        largest_side = max(int(width), int(height))
    except (TypeError, ValueError):
        largest_side = 0
    if largest_side >= 1000:
        return 1.0
    if largest_side >= 600:
        return 0.75
    return 0.5


def _recently_created(value: Any) -> float:
    if not value:
        return 0.0
    try:
        created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days
        return 1.0 if age_days <= 90 else 0.0
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class NormalizedProduct:
    brand: str | None
    audience: str | None
    designer: str | None
    niche: str | None
    arabian_classification: str | None
    fragrance_family: str | None
    fragrance_notes: list[str]
    concentration: str | None
    size: str | None
    price_band: str | None
    gift_suitability: str | None
    season: str | None
    occasion: str | None
    image_quality: float
    image_available: bool
    inventory_eligible: bool
    eligibility_score: float
    eligibility_status: str
    eligibility_reasons: list[str]
    normalization_status: str
    normalization_category: str
    required_fields: list[str]
    missing_required_fields: list[str]
    size_source: str | None
    size_value: float | int | None
    size_unit: str | None
    eligibility_positive_reasons: list[str]
    eligibility_blocking_reasons: list[str]
    qa_warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_shopify_product(product: dict[str, Any]) -> NormalizedProduct:
    tags = _tags(product)
    fields = _metafields(product)
    variants = _values(product, "variants")
    images = _values(product, "media")
    if not images:
        images = _values(product, "images")

    vendor = _text(product.get("vendor"))
    product_type = _text(product.get("productType", product.get("product_type")))
    status = str(product.get("status", "")).upper()
    total_inventory = product.get("totalInventory", product.get("inventory_total", 0)) or 0
    try:
        inventory_available = int(total_inventory) > 0
    except (TypeError, ValueError):
        inventory_available = False

    prices = [_money(variant.get("price")) for variant in variants]
    prices = [price for price in prices if price is not None]
    price = min(prices) if prices else _money(product.get("price"))
    if price is None:
        price_band = None
    elif price < 50:
        price_band = "under_50"
    elif price < 100:
        price_band = "50_to_99"
    elif price < 250:
        price_band = "100_to_249"
    else:
        price_band = "250_plus"

    collections = _collection_titles(product)
    brand = _field(fields, "brand", "designer") or vendor
    audience = _audience(product, fields, tags, collections)
    taxonomy = classify_vendor(vendor)
    designer = taxonomy["designer"]
    niche = taxonomy["niche"]
    arabian = taxonomy["arabian_classification"]
    family = _fragrance_family(product, fields, tags, collections)
    notes_value = _field(fields, "fragrance_notes", "fragrance.notes", "notes")
    notes = [note.strip() for note in (notes_value or "").split(",") if note.strip()]
    concentration = _concentration(product, fields, tags)
    size, size_source, size_value, size_unit = _size(product, fields, tags, variants)
    gift = _is_gift_set(product, fields, tags, collections)
    season = _field(fields, "season")
    occasion = _field(fields, "occasion")
    category = _category(product, tags, collections, gift)

    image_quality = _image_quality(images)
    positive_reasons = []
    blocking_reasons = []
    if inventory_available:
        positive_reasons.append("Product has available inventory.")
    else:
        blocking_reasons.append("Product has no available inventory.")
    if status == "ACTIVE":
        positive_reasons.append("Product is active in Shopify.")
    else:
        blocking_reasons.append("Product is not active in Shopify.")
    if image_quality:
        positive_reasons.append("Product has an authentic catalog image.")
    else:
        blocking_reasons.append("Product has no catalog image.")
    if vendor:
        positive_reasons.append("Product has a vendor or brand.")
    else:
        blocking_reasons.append("Product has no vendor or brand.")

    active = status == "ACTIVE"
    newness = _recently_created(product.get("createdAt", product.get("shopify_created_at")))
    score = round(
        (25 if inventory_available else 0)
        + (25 if active else 0)
        + (20 * image_quality)
        + (10 if vendor else 0)
        + (10 if product_type else 0)
        + (5 if price is not None else 0)
        + (5 * newness),
        2,
    )
    eligible = inventory_available and active and bool(images)
    required_fields = list(CATEGORY_REQUIRED_FIELDS[category])
    field_values = {
        "brand": brand,
        "audience": audience,
        "designer": designer,
        "niche": niche,
        "arabian_classification": arabian,
        "fragrance_family": family,
        "fragrance_notes": notes,
        "concentration": concentration,
        "size": size,
        "price_band": price_band,
        "gift_suitability": gift,
        "season": season,
        "occasion": occasion,
    }
    missing_required_fields = [
        field for field in required_fields
        if not field_values[field]
    ]
    source_evidence = any([
        _text(product.get("title")),
        vendor,
        product_type,
        tags,
        collections,
        fields,
        variants,
        images,
        price,
    ])
    if not source_evidence:
        normalization_status = "UNKNOWN"
    else:
        normalization_status = "COMPLETE" if not missing_required_fields else "PARTIAL"

    qa_warnings = []
    taxonomy_text = " ".join([product_type or "", *collections, *tags]).lower()
    title = (_text(product.get("title")) or "").lower()
    if any(term in title for term in ("duffle bag", "backpack", "luggage", "handbag")) and any(
        term in taxonomy_text for term in ("perfume", "cologne", "fragrance")
    ):
        qa_warnings.append("Product title suggests a bag or luggage item but source taxonomy classifies it as fragrance.")
    if any(term in (product_type or "").lower() for term in ("skin care", "skincare")) and any(
        re.search(r"\bhair\b", collection, re.IGNORECASE) for collection in collections
    ):
        qa_warnings.append("Source product type is skin care while a source collection identifies hair care.")
    reasons = positive_reasons + blocking_reasons

    return NormalizedProduct(
        brand=brand,
        audience=audience,
        designer=designer,
        niche=niche,
        arabian_classification=arabian,
        fragrance_family=family,
        fragrance_notes=notes,
        concentration=concentration,
        size=size,
        price_band=price_band,
        gift_suitability=gift,
        season=season,
        occasion=occasion,
        image_quality=image_quality,
        image_available=bool(images),
        inventory_eligible=inventory_available and status == "ACTIVE",
        eligibility_score=score,
        eligibility_status="ELIGIBLE" if eligible else "INELIGIBLE",
        eligibility_reasons=reasons,
        normalization_status=normalization_status,
        normalization_category=category,
        required_fields=required_fields,
        missing_required_fields=missing_required_fields,
        size_source=size_source,
        size_value=size_value,
        size_unit=size_unit,
        eligibility_positive_reasons=positive_reasons,
        eligibility_blocking_reasons=blocking_reasons,
        qa_warnings=qa_warnings,
    )


def assemble_bulk_products(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild product children from Shopify's flattened JSONL bulk format."""
    products: dict[str, dict[str, Any]] = {}
    for row in rows:
        row = dict(row)
        parent_id = row.pop("__parentId", None)
        if not parent_id:
            product_id = row.get("id")
            if product_id:
                product = row
                product.setdefault("variants", [])
                product.setdefault("media", [])
                product.setdefault("collections", [])
                product.setdefault("metafields", [])
                products[product_id] = product
            continue
        product = products.get(parent_id)
        if not product:
            continue
        typename = str(row.get("__typename", ""))
        if typename == "ProductVariant" or "inventoryQuantity" in row or "sku" in row:
            product["variants"].append(row)
        elif typename == "MediaImage" or "image" in row:
            product["media"].append(row)
        elif typename == "Metafield" or "namespace" in row:
            product["metafields"].append(row)
        elif typename == "Collection" or ("handle" in row and "title" in row):
            product["collections"].append(row)
    return list(products.values())


async def stream_bulk_products(
    rows: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Yield one grouped Shopify product at a time without buffering the catalog."""
    current: dict[str, Any] | None = None
    async for source_row in rows:
        row = dict(source_row)
        parent_id = row.pop("__parentId", None)
        if not parent_id:
            if current is not None:
                yield current
            current = row
            current.setdefault("variants", [])
            current.setdefault("media", [])
            current.setdefault("collections", [])
            current.setdefault("metafields", [])
            continue
        if current is None or current.get("id") != parent_id:
            continue
        typename = str(row.get("__typename", ""))
        if typename == "ProductVariant" or "inventoryQuantity" in row or "sku" in row:
            current["variants"].append(row)
        elif typename == "MediaImage" or "image" in row:
            current["media"].append(row)
        elif typename == "Metafield" or "namespace" in row:
            current["metafields"].append(row)
        elif typename == "Collection" or ("handle" in row and "title" in row):
            current["collections"].append(row)
    if current is not None:
        yield current