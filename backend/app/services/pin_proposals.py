from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any, Callable

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import (
    Board,
    Campaign,
    ContentAngle,
    ContentRevision,
    ContentVersionSelection,
    CreativeTemplate,
    DraftStatus,
    KeywordCluster,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    Product,
    ProductImage,
    ProductIntelligence,
    Store,
)
from app.services.board_mapping import display_board_name
from app.services.fingerprints import concept_fingerprint, text_fingerprint
from app.services.utm import build_pinterest_utm_url


MAX_CONTROLLED_PRODUCTS = 20
MAX_PROPOSALS_PER_PRODUCT = 2
CAMPAIGN_KEY = "pinterest-editorial-v1"

CREATIVE_TEMPLATES = {
    "luxury_product_spotlight": "Luxury Product Spotlight",
    "product_classification": "Product + Classification",
    "gift_guide_gift_set": "Gift Guide / Gift Set",
    "editorial_product_pick": "Editorial Product Pick",
}

UNSUPPORTED_CLAIM_PATTERNS = (
    re.compile(r"\b(?:best|#1|number one|popular|trending|viral|bestseller)\b", re.I),
    re.compile(r"\b(?:sale|discount|deal|save|limited time|exclusive)\b", re.I),
    re.compile(r"\b(?:long[- ]lasting|long lasting|all[- ]day|projection|sillage|compliment)\b", re.I),
    re.compile(r"\b(?:mood|seasonal|for summer|for winter|date night)\b", re.I),
)


@dataclass(frozen=True)
class ProposalProduct:
    product: Product
    intelligence: ProductIntelligence
    image: ProductImage
    normalized: dict[str, Any]

    @property
    def category(self) -> str:
        return self.normalized.get("normalization_category", "other")

    @property
    def status(self) -> str:
        return self.intelligence.normalization_status or "UNKNOWN"


@dataclass(frozen=True)
class Angle:
    key: str
    label: str
    template_key: str
    board_key: str
    keywords: tuple[str, ...]
    reason: str
    facts_used: tuple[str, ...]


@dataclass(frozen=True)
class RankedAngle:
    angle: Angle
    score: int
    factors: dict[str, int]
    intent_group: str
    selection_reason: str


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:70]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _display_category(category: str) -> str:
    return {
        "fragrance": "fragrance",
        "gift_set": "gift set",
        "bath_body": "bath and body",
        "beauty": "beauty",
        "home_fragrance": "home fragrance",
        "other": "product",
    }.get(category, category.replace("_", " "))


def _tag_and_collection_text(item: ProposalProduct) -> list[str]:
    tags = item.product.tags or []
    collections = item.product.collections or []
    values: list[str] = [str(tag) for tag in tags]
    for collection in collections:
        if isinstance(collection, dict):
            values.append(str(collection.get("title") or ""))
        else:
            values.append(str(collection))
    return [value.strip().lower() for value in values if value.strip()]


def _is_new_arrival(item: ProposalProduct) -> bool:
    return any("new arrival" in value or "new-arrivals" in value for value in _tag_and_collection_text(item))


def _is_explicitly_luxury(item: ProposalProduct) -> bool:
    catalog_values = [
        item.product.title,
        item.product.product_type or "",
        *_tag_and_collection_text(item),
    ]
    return any(re.search(r"\bluxury\b", value, re.IGNORECASE) for value in catalog_values)


def _classification(item: ProposalProduct) -> tuple[str | None, str | None]:
    if item.intelligence.arabian_classification:
        return "arabian", item.intelligence.arabian_classification
    if item.intelligence.designer:
        return "designer", item.intelligence.designer
    if item.intelligence.niche:
        return "niche", item.intelligence.niche
    return None, None


def _board_key(item: ProposalProduct, angle_key: str) -> str:
    classification, _ = _classification(item)
    if classification == "arabian":
        return "arabian-fragrance"
    if classification == "designer":
        return "designer-fragrances"
    if classification == "niche":
        return "niche-fragrances"
    if item.category == "gift_set":
        return "gift-sets"
    if item.category == "home_fragrance":
        return "home-fragrance"
    if item.category in {"beauty", "bath_body"}:
        return "beauty-body"
    if angle_key == "new-arrival":
        return "new-arrivals"
    if item.intelligence.audience == "women":
        return "womens-fragrance"
    if item.intelligence.audience == "men":
        return "mens-fragrance"
    if item.intelligence.audience == "unisex":
        return "fragrance-guides"
    return "fragrance-guides" if item.category == "fragrance" else "editorial-picks"


def _keywords(item: ProposalProduct, angle_key: str) -> tuple[str, ...]:
    values: list[str] = ["diamond shelf"]
    if item.product.vendor:
        values.append(item.product.vendor.lower())
    if item.category == "fragrance":
        values.append("fragrance")
        product_type = (item.product.product_type or "").lower()
        if any(term in product_type for term in ("perfume", "parfum", "cologne", "fragrance")):
            values.append("perfume")
    else:
        values.append(_display_category(item.category))
    if item.intelligence.audience:
        values.append(f"{item.intelligence.audience} {values[-1]}")
    if item.intelligence.concentration:
        values.append(item.intelligence.concentration.lower())
    if item.intelligence.fragrance_family:
        values.append(f"{item.intelligence.fragrance_family.lower()} fragrance")
    if item.intelligence.gift_suitability:
        values.append("fragrance gift set")
    classification, label = _classification(item)
    if classification and label:
        values.append(label.lower())
    if angle_key == "luxury-product-spotlight":
        values.append("luxury product")
    if item.intelligence.price_band in {"under_50", "50_to_99"}:
        values.append(f"products under {50 if item.intelligence.price_band == 'under_50' else 100}")
    return tuple(dict.fromkeys(value for value in values if value))


def _angle_candidates(item: ProposalProduct) -> list[Angle]:
    category = item.category
    intelligence = item.intelligence
    candidates: list[Angle] = []
    if _is_explicitly_luxury(item):
        candidates.append(Angle(
            "luxury-product-spotlight",
            "Luxury Product Spotlight",
            "luxury_product_spotlight",
            _board_key(item, "luxury-product-spotlight"),
            _keywords(item, "luxury-product-spotlight"),
            "The source catalog explicitly labels this product or collection as luxury.",
            ("catalog_luxury_label", "brand"),
        ))
    if _is_new_arrival(item):
        candidates.append(Angle(
            "new-arrival",
            "New Arrival",
            "editorial_product_pick",
            "new-arrivals",
            _keywords(item, "new-arrival"),
            "The catalog explicitly marks this product as a new arrival.",
            ("product_tags_or_collections",),
        ))
    if category == "fragrance" and intelligence.audience in {"men", "women", "unisex"}:
        audience_label = {
            "men": "Men's Fragrance",
            "women": "Women's Fragrance",
            "unisex": "Unisex Fragrance",
        }[intelligence.audience]
        candidates.append(Angle(
            f"{intelligence.audience}-fragrance",
            audience_label,
            "product_classification",
            _board_key(item, f"{intelligence.audience}-fragrance"),
            _keywords(item, f"{intelligence.audience}-fragrance"),
            f"The catalog explicitly identifies the audience as {intelligence.audience}.",
            ("audience", "normalization_category"),
        ))
    classification, classification_label = _classification(item)
    if category == "fragrance" and classification and classification_label:
        label = classification.title()
        candidates.append(Angle(
            f"{classification}-fragrance",
            f"{label} Fragrance",
            "product_classification",
            _board_key(item, f"{classification}-fragrance"),
            _keywords(item, f"{classification}-fragrance"),
            f"The reviewed vendor taxonomy explicitly classifies this vendor as {classification}.",
            (classification, "brand"),
        ))
    if intelligence.gift_suitability:
        candidates.append(Angle(
            "gift-set",
            "Gift Set",
            "gift_guide_gift_set",
            "gift-sets",
            _keywords(item, "gift-set"),
            "The product type, title, collection, tag, or gift field explicitly identifies a set.",
            ("gift_suitability", "normalization_category"),
        ))
    if category == "home_fragrance":
        candidates.append(Angle(
            "home-fragrance",
            "Home Fragrance",
            "editorial_product_pick",
            "home-fragrance",
            _keywords(item, "home-fragrance"),
            "Product taxonomy explicitly identifies home fragrance.",
            ("normalization_category",),
        ))
    if category in {"beauty", "bath_body"}:
        candidates.append(Angle(
            "beauty-body",
            "Beauty / Body",
            "editorial_product_pick",
            "beauty-body",
            _keywords(item, "beauty-body"),
            "Product taxonomy explicitly identifies beauty, hair, skin, or body care.",
            ("normalization_category",),
        ))
    if intelligence.price_band in {"under_50", "50_to_99"}:
        candidates.append(Angle(
            "price-focused",
            "Price-Focused Product Pick",
            "editorial_product_pick",
            _board_key(item, "price-focused"),
            _keywords(item, "price-focused"),
            "The persisted catalog price band supports a price-focused edit.",
            ("price_band",),
        ))
    candidates.append(Angle(
        "editorial-product-pick",
        "Editorial Product Pick",
        "editorial_product_pick",
        _board_key(item, "editorial-product-pick"),
        _keywords(item, "editorial-product-pick"),
        "Eligible product with an authentic catalog image and a direct product destination.",
        ("title", "brand", "product_url", "image"),
    ))
    seen: set[str] = set()
    return [angle for angle in candidates if not (angle.key in seen or seen.add(angle.key))]


def _angle_intent_group(angle: Angle) -> str:
    if angle.key in {"arabian-fragrance", "designer-fragrance", "niche-fragrance"}:
        return "classification"
    if angle.key in {"men-fragrance", "women-fragrance", "unisex-fragrance"}:
        return "audience"
    if angle.key == "new-arrival":
        return "recency"
    if angle.key == "price-focused":
        return "price"
    if angle.key in {"gift-set", "home-fragrance", "beauty-body"}:
        return "category"
    if angle.key == "luxury-product-spotlight":
        return "premium"
    return "editorial"


def _rank_angles(
    item: ProposalProduct,
    angle_usage: Counter[str] | None = None,
    limit: int = MAX_PROPOSALS_PER_PRODUCT,
) -> tuple[list[RankedAngle], list[dict[str, Any]]]:
    """Rank only evidence-supported angles with stable, explainable factors."""
    usage = angle_usage or Counter()
    candidates: list[RankedAngle] = []
    for angle in _angle_candidates(item):
        group = _angle_intent_group(angle)
        is_classification = group == "classification"
        is_category = group == "category"
        is_audience = group == "audience"
        specificity = (
            52 if is_classification else
            47 if angle.key == "gift-set" else
            44 if is_category else
            41 if is_audience else
            45 if group == "premium" else
            28 if angle.key == "new-arrival" else
            21 if angle.key == "price-focused" else
            12
        )
        classification_factor = 30 if is_classification else 0
        editorial_usefulness = (
            17 if is_classification else
            16 if is_category else
            14 if is_audience else
            16 if group == "premium" else
            8 if angle.key == "new-arrival" else
            6 if angle.key == "price-focused" else
            3
        )
        usage_penalty = min(usage[angle.key] * 6, 30)
        score = specificity + classification_factor + editorial_usefulness - usage_penalty
        factors = {
            "specificity": specificity,
            "classification": classification_factor,
            "editorial_usefulness": editorial_usefulness,
            "portfolio_diversity": -usage_penalty,
        }
        reason = (
            f"Supported by {', '.join(angle.facts_used)}; "
            f"specificity {specificity}, editorial usefulness {editorial_usefulness}, "
            f"and current-batch usage penalty {usage_penalty}."
        )
        candidates.append(RankedAngle(angle, score, factors, group, reason))

    # The explicit key tie-breaker makes the result deterministic without randomness.
    candidates.sort(
        key=lambda ranked: (
            -ranked.score,
            -ranked.factors["specificity"],
            ranked.angle.key,
        )
    )
    selected: list[RankedAngle] = []
    selected_groups: set[str] = set()
    rejected: list[dict[str, Any]] = []
    for ranked in candidates:
        if len(selected) < limit and ranked.intent_group not in selected_groups:
            selected.append(ranked)
            selected_groups.add(ranked.intent_group)
            continue
        if ranked.intent_group in selected_groups:
            reason = "A higher-ranked candidate with the same editorial intent was selected for this product."
        else:
            reason = "The controlled per-product proposal limit was reached."
        rejected.append({
            "angle_key": ranked.angle.key,
            "angle": ranked.angle.label,
            "score": ranked.score,
            "factors": ranked.factors,
            "intent_group": ranked.intent_group,
            "reason": reason,
        })
    return selected, rejected


def _rank_batch(
    products: list[ProposalProduct],
    max_proposals_per_product: int,
) -> tuple[dict[str, list[RankedAngle]], list[dict[str, Any]]]:
    usage: Counter[str] = Counter()
    ranked_by_product: dict[str, list[RankedAngle]] = {}
    diagnostics: list[dict[str, Any]] = []
    for item in products:
        selected, rejected = _rank_angles(item, usage, max_proposals_per_product)
        ranked_by_product[item.product.id] = selected
        for ranked in selected:
            usage[ranked.angle.key] += 1
        candidate_details = []
        for ranked in selected:
            candidate_details.append({
                "angle_key": ranked.angle.key,
                "angle": ranked.angle.label,
                "score": ranked.score,
                "factors": ranked.factors,
                "intent_group": ranked.intent_group,
                "supported": True,
                "selected": True,
                "reason": ranked.selection_reason,
            })
        candidate_details.extend({
            **candidate,
            "supported": True,
            "selected": False,
        } for candidate in rejected)
        diagnostics.append({
            "product_id": item.product.id,
            "product_title": item.product.title,
            "candidate_angles": candidate_details,
            "selected_angles": [ranked.angle.label for ranked in selected],
            "rejected_angles": [candidate["angle"] for candidate in rejected],
        })
    return ranked_by_product, diagnostics


def _preview_proposal(item: ProposalProduct, ranked: RankedAngle) -> dict[str, Any]:
    angle = ranked.angle
    copy = _copy(item, angle)
    board_key = _board_key(item, angle.key)
    keywords = tuple(dict.fromkeys(_keywords(item, angle.key)))
    fingerprint = concept_fingerprint(
        product_ids=[item.product.id],
        content_angle=angle.key,
        keyword_cluster="|".join(keywords),
        board_id=board_key,
    )
    utm_url = build_pinterest_utm_url(
        item.product.product_url,
        campaign=CAMPAIGN_KEY,
        content=f"{_slugify(item.product.handle or item.product.title)}-{angle.key}",
    )
    missing = _missing_facts(item)
    warnings = []
    if item.status != "COMPLETE":
        warnings.append(f"Normalization is {item.status}; unavailable fields were omitted from copy.")
    if missing:
        warnings.append("Unavailable catalog facts were not used: " + ", ".join(missing) + ".")
    return {
        "id": None,
        "concept_id": None,
        "product_id": item.product.id,
        "product_title": item.product.title,
        "vendor": item.product.vendor,
        "image_url": item.image.source_url,
        "headline": copy["headline"],
        "title": copy["title"],
        "description": copy["description"],
        "alt_text": copy["alt_text"],
        "cta": copy["cta"],
        "canonical_url": item.product.product_url,
        "utm_url": utm_url,
        "keywords": list(keywords),
        "content_angle": angle.label,
        "content_angle_key": angle.key,
        "creative_template": CREATIVE_TEMPLATES[angle.template_key],
        "creative_template_key": angle.template_key,
        "intended_board": {
            "key": board_key,
            "name": display_board_name(board_key),
            "pinterest_board_id": None,
        },
        "intelligence_facts_used": _facts_used(item, angle),
        "warnings": warnings,
        "missing_facts": missing,
        "unsupported_claims": _unsupported_claims(copy, item),
        "duplicate_fingerprint": fingerprint,
        "text_fingerprint": text_fingerprint(
            title=copy["title"],
            description=copy["description"],
            alt_text=copy["alt_text"],
        ),
        "normalization_status": item.status,
        "approval_status": "REVIEW",
        "variation_reason": None,
        "created_at": None,
        "persisted": False,
        "selection_reason": ranked.selection_reason,
        "ranking_score": ranked.score,
        "ranking_factors": ranked.factors,
    }


def _copy(item: ProposalProduct, angle: Angle) -> dict[str, str]:
    title = item.product.title.strip()
    vendor = _text(item.intelligence.brand or item.product.vendor)
    headline = f"{angle.label}: {vendor or title}"
    pin_title = f"{title} | {angle.label}"
    clauses = [f"Explore {title}{f' by {vendor}' if vendor and vendor.lower() not in title.lower() else ''} at Diamond Shelf."]
    if item.intelligence.audience:
        clauses.append(f"The catalog lists this product for {item.intelligence.audience}.")
    if item.intelligence.concentration:
        clauses.append(f"Listed concentration: {item.intelligence.concentration}.")
    if item.intelligence.fragrance_family and item.category == "fragrance":
        clauses.append(f"Listed fragrance family: {item.intelligence.fragrance_family}.")
    if item.intelligence.size:
        clauses.append(f"Catalog size: {item.intelligence.size}.")
    if item.intelligence.gift_suitability:
        clauses.append("The catalog identifies this as a gift set.")
    clauses.append("View the authentic product image, current price, and availability on the product page.")
    return {
        "headline": " ".join(headline.split())[:140].rstrip(),
        "title": " ".join(pin_title.split())[:500].rstrip(),
        "description": " ".join(clauses)[:800].rstrip(),
        "cta": "Shop the authentic product",
        "alt_text": f"{title}{f' by {vendor}' if vendor else ''} — authentic Shopify product image for Diamond Shelf",
    }


def _unsupported_claims(copy: dict[str, str], item: ProposalProduct) -> list[str]:
    text = " ".join(copy.values())
    for source_value in (item.product.title, item.product.vendor):
        if source_value:
            text = re.sub(re.escape(source_value), "", text, flags=re.IGNORECASE)
    return sorted({
        pattern.pattern
        for pattern in UNSUPPORTED_CLAIM_PATTERNS
        if pattern.search(text)
    })


def _missing_facts(item: ProposalProduct) -> list[str]:
    missing = list(item.normalized.get("missing_required_fields") or [])
    if item.category == "fragrance":
        for field in ("fragrance_notes", "season", "occasion"):
            if not getattr(item.intelligence, field, None):
                missing.append(field)
    return list(dict.fromkeys(missing))


def _facts_used(item: ProposalProduct, angle: Angle) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "title": item.product.title,
        "product_url": item.product.product_url,
        "normalization_category": item.category,
        "normalization_status": item.status,
        "image": item.image.source_url,
    }
    for field in (
        "brand",
        "audience",
        "designer",
        "niche",
        "arabian_classification",
        "fragrance_family",
        "concentration",
        "size",
        "price_band",
        "gift_suitability",
    ):
        value = getattr(item.intelligence, field, None)
        if value:
            facts[field] = value
    facts["angle_supporting_fields"] = list(angle.facts_used)
    return facts


def _ensure_angle(db: Any, angle: Angle) -> ContentAngle:
    row = db.scalar(select(ContentAngle).where(ContentAngle.key == angle.key))
    if not row:
        row = ContentAngle(
            key=angle.key,
            name=angle.label,
            description=angle.reason,
            rules={"facts_required": list(angle.facts_used)},
            active=True,
        )
        db.add(row)
        db.flush()
    return row


def _ensure_template(db: Any, key: str) -> CreativeTemplate:
    row = db.scalar(select(CreativeTemplate).where(
        CreativeTemplate.key == key,
        CreativeTemplate.version == 1,
    ))
    if not row:
        row = CreativeTemplate(
            key=key,
            version=1,
            name=CREATIVE_TEMPLATES[key],
            renderer="none",
            definition={"renderer_active": False, "proposal_only": True},
            active=True,
        )
        db.add(row)
        db.flush()
    return row


def _ensure_board(db: Any, store_id: str, board_key: str) -> Board:
    row = db.scalar(select(Board).where(Board.store_id == store_id, Board.slug == board_key))
    if not row:
        row = Board(
            store_id=store_id,
            name=display_board_name(board_key),
            slug=board_key,
            rules={"source": "deterministic_internal_recommendation"},
            active=True,
        )
        db.add(row)
        db.flush()
    return row


def _ensure_keyword_cluster(db: Any, keywords: tuple[str, ...], angle: Angle) -> KeywordCluster:
    key = "|".join(keywords)[:120]
    row = db.scalar(select(KeywordCluster).where(KeywordCluster.key == key))
    if not row:
        row = KeywordCluster(
            key=key,
            label=angle.label,
            keywords=list(keywords),
            intent="pinterest_editorial",
        )
        db.add(row)
        db.flush()
    return row


def _ensure_campaign(db: Any, store_id: str) -> Campaign:
    row = db.scalar(select(Campaign).where(
        Campaign.store_id == store_id,
        Campaign.slug == CAMPAIGN_KEY,
    ))
    if not row:
        row = Campaign(
            store_id=store_id,
            name="Diamond Shelf Pinterest Editorial",
            slug=CAMPAIGN_KEY,
            utm_campaign=CAMPAIGN_KEY,
            active=True,
        )
        db.add(row)
        db.flush()
    return row


def _segment_keys(item: ProposalProduct) -> list[str]:
    keys: list[str] = []
    classification, _ = _classification(item)
    if classification:
        keys.append(classification)
    if item.intelligence.audience:
        keys.append(item.intelligence.audience)
    keys.append(item.category)
    if item.intelligence.price_band:
        keys.append(item.intelligence.price_band)
    return keys


def _select_products(
    candidates: list[ProposalProduct],
    limit: int,
) -> list[ProposalProduct]:
    buckets: dict[str, list[ProposalProduct]] = defaultdict(list)
    for item in candidates:
        for key in _segment_keys(item):
            buckets[key].append(item)
    ordered_keys = (
        "designer", "arabian", "niche", "men", "women", "unisex",
        "gift_set", "home_fragrance", "beauty", "bath_body",
        "under_50", "50_to_99", "100_to_249", "250_plus",
        "fragrance", "other",
    )
    selected: list[ProposalProduct] = []
    seen: set[str] = set()
    while len(selected) < limit:
        added = False
        for key in ordered_keys:
            for item in buckets.get(key, []):
                if item.product.id not in seen:
                    selected.append(item)
                    seen.add(item.product.id)
                    added = True
                    break
            if len(selected) >= limit:
                break
        if not added:
            break
    for item in candidates:
        if len(selected) >= limit:
            break
        if item.product.id not in seen:
            selected.append(item)
            seen.add(item.product.id)
    return selected


def _creative_payload(creative: Any) -> dict[str, Any] | None:
    if not creative:
        return None
    return {
        "id": creative.id,
        "status": creative.render_status,
        "image_url": creative.rendered_url,
        "error": creative.render_error,
        "width": creative.width,
        "height": creative.height,
        "size_bytes": creative.size_bytes,
        "render_duration_ms": creative.render_duration_ms,
        "duration_ms": creative.render_duration_ms,
        "creative_fingerprint": creative.creative_fingerprint,
        "sha256": creative.sha256,
        "template_version": (creative.render_spec or {}).get("template_version"),
        "specification": creative.render_spec,
    }


def _version_payload(revision: ContentRevision, creative: Any, active_id: str | None) -> dict[str, Any]:
    return {
        "id": revision.id,
        "version": revision.version,
        "kind": revision.revision_kind,
        "status": revision.status,
        "parent_revision_id": revision.parent_revision_id,
        "active": revision.id == active_id,
        "headline": revision.headline,
        "title": revision.title,
        "description": revision.description,
        "alt_text": revision.alt_text,
        "cta": revision.cta,
        "creative_template": revision.creative_template,
        "creative_template_key": revision.creative_template_key,
        "text_fingerprint": revision.text_fingerprint,
        "creative_fingerprint": revision.creative_fingerprint,
        "facts_used": revision.facts_used,
        "warnings": revision.warnings,
        "missing_facts": revision.missing_facts,
        "unsupported_claims": revision.unsupported_claims,
        "provenance": revision.provenance,
        "provider_mode": revision.provider_mode,
        "generation_mode": revision.generation_mode,
        "reason": revision.reason,
        "generation_type": revision.generation_type,
        "intended_channel": revision.intended_channel,
        "content_payload": revision.content_payload,
        "video_spec": revision.video_spec,
        "background_asset_id": revision.background_asset_id,
        "ai_telemetry_id": revision.ai_telemetry_id,
        "estimated_cost_usd": float(revision.estimated_cost_usd) if revision.estimated_cost_usd is not None else None,
        "actual_cost_usd": float(revision.actual_cost_usd) if revision.actual_cost_usd is not None else None,
        "created_at": revision.created_at,
        "creative": _creative_payload(creative),
    }


def _version_context(
    db: Any,
    product: Product,
    intelligence: ProductIntelligence,
    rationale: dict[str, Any],
    draft: PinDraft,
    original_creative: Any,
) -> tuple[list[dict[str, Any]], ContentRevision | None, str | None]:
    selection = db.scalar(select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft.id))
    active_id = selection.revision_id if selection else None
    original = {
        "id": None,
        "version": 1,
        "kind": "ORIGINAL",
        "status": "REVIEW",
        "parent_revision_id": None,
        "active": active_id is None,
        "headline": rationale.get("headline", ""),
        "title": draft.title,
        "description": draft.description,
        "alt_text": draft.alt_text,
        "cta": rationale.get("cta", ""),
        "creative_template": rationale.get("creative_template", ""),
        "creative_template_key": rationale.get("creative_template_key", ""),
        "text_fingerprint": draft.text_fingerprint,
        "creative_fingerprint": original_creative.creative_fingerprint if original_creative else None,
        "facts_used": rationale.get("facts_used", {}),
        "warnings": rationale.get("warnings", []),
        "missing_facts": rationale.get("missing_facts", []),
        "unsupported_claims": rationale.get("unsupported_claims", []),
        "provenance": (original_creative.render_spec or {}).get("image", {}) if original_creative else rationale.get("authentic_image", {}),
        "provider_mode": "deterministic_original",
        "generation_mode": "original_persisted",
        "reason": "original_persisted",
        "generation_type": "original",
        "intended_channel": "pinterest",
        "content_payload": None,
        "video_spec": None,
        "background_asset_id": None,
        "created_at": draft.created_at,
        "creative": _creative_payload(original_creative),
    }
    revisions = [
        _version_payload(
            revision,
            db.get(PinCreative, revision.creative_id) if revision.creative_id else None,
            active_id,
        )
        for revision in db.scalars(
            select(ContentRevision)
            .where(ContentRevision.draft_id == draft.id)
            .order_by(ContentRevision.version)
        )
    ]
    active = db.get(ContentRevision, active_id) if active_id else None
    return [original, *revisions], active, active_id


def _serialize_proposal(
    product: Product,
    intelligence: ProductIntelligence,
    rationale: dict[str, Any],
    draft: PinDraft,
    creative: Any = None,
    versions: list[dict[str, Any]] | None = None,
    active: ContentRevision | None = None,
    active_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "id": draft.id,
        "concept_id": rationale.get("concept_id"),
        "product_id": product.id,
        "product_title": product.title,
        "vendor": product.vendor,
        "image_url": rationale["authentic_image"]["url"],
        "headline": rationale["headline"],
        "title": draft.title,
        "description": draft.description,
        "alt_text": draft.alt_text,
        "cta": rationale["cta"],
        "canonical_url": draft.destination_url,
        "utm_url": draft.utm_url,
        "keywords": rationale["keywords"],
        "content_angle": rationale["content_angle"],
        "content_angle_key": rationale["content_angle_key"],
        "creative_template": rationale["creative_template"],
        "creative_template_key": rationale["creative_template_key"],
        "intended_board": rationale["board_mapping"],
        "intelligence_facts_used": rationale["facts_used"],
        "warnings": rationale["warnings"],
        "missing_facts": rationale["missing_facts"],
        "unsupported_claims": rationale["unsupported_claims"],
        "duplicate_fingerprint": rationale["duplicate_fingerprint"],
        "text_fingerprint": draft.text_fingerprint,
        "normalization_status": intelligence.normalization_status,
        "approval_status": "REVIEW" if draft.status == DraftStatus.READY_FOR_REVIEW else draft.status.value,
        "variation_reason": rationale.get("variation_reason"),
        "selection_reason": rationale.get("selection_reason"),
        "ranking_score": rationale.get("ranking_score"),
        "ranking_factors": rationale.get("ranking_factors"),
        "created_at": draft.created_at,
        "creative": _creative_payload(creative),
        "active_revision_id": active_id,
        "active_version": active.version if active else 1,
        "versions": versions or [],
    }
    if active:
        payload.update({
            "headline": active.headline,
            "title": active.title,
            "description": active.description,
            "alt_text": active.alt_text,
            "cta": active.cta,
            "canonical_url": active.destination_url,
            "utm_url": active.utm_url,
            "keywords": active.keywords,
            "content_angle": active.content_angle,
            "content_angle_key": active.content_angle_key,
            "creative_template": active.creative_template,
            "creative_template_key": active.creative_template_key,
            "intelligence_facts_used": active.facts_used,
            "warnings": active.warnings,
            "missing_facts": active.missing_facts,
            "unsupported_claims": active.unsupported_claims,
            "text_fingerprint": active.text_fingerprint,
            # Copy revisions intentionally do not own artwork. Keep the original
            # rendered creative visible until a distinct creative revision is selected.
            "creative": next(
                (
                    version["creative"]
                    for version in versions or []
                    if version["id"] == active.id
                ),
                None,
            ) or _creative_payload(creative),
        })
    return payload


class PinProposalService:
    def __init__(self, session_factory: Callable = SessionLocal):
        self.session_factory = session_factory

    def _products(self, db: Any) -> list[ProposalProduct]:
        images_by_product: dict[str, ProductImage] = {}
        for image in db.scalars(
            select(ProductImage)
            .where(
                ProductImage.editorial_eligible.is_(True),
                ProductImage.shopify_media_id.is_not(None),
                ProductImage.source_url.is_not(None),
            )
            .order_by(ProductImage.is_primary.desc(), ProductImage.id)
        ):
            images_by_product.setdefault(image.product_id, image)
        rows = db.execute(
            select(Product, ProductIntelligence)
            .join(ProductIntelligence, ProductIntelligence.product_id == Product.id)
            .where(
                ProductIntelligence.eligibility_status == "ELIGIBLE",
                Product.excluded_from_editorial.is_(False),
            )
            .order_by(Product.title, Product.id)
        ).all()
        products: list[ProposalProduct] = []
        for product, intelligence in rows:
            image = images_by_product.get(product.id)
            if not image or not image.source_url:
                continue
            products.append(ProposalProduct(
                product=product,
                intelligence=intelligence,
                image=image,
                normalized=intelligence.normalized_data or {},
            ))
        return products

    def _filter(
        self,
        products: list[ProposalProduct],
        *,
        category: str | None = None,
        vendor: str | None = None,
        audience: str | None = None,
        designer: str | None = None,
        niche: str | None = None,
        arabian: str | None = None,
        price_band: str | None = None,
        concentration: str | None = None,
        fragrance_family: str | None = None,
        inventory_min: int | None = None,
        normalization_status: str | None = None,
    ) -> list[ProposalProduct]:
        def matches(item: ProposalProduct) -> bool:
            intelligence = item.intelligence
            if category and item.category != category:
                return False
            if vendor and vendor.lower() not in (item.product.vendor or "").lower():
                return False
            if audience and intelligence.audience != audience:
                return False
            if designer and intelligence.designer != designer:
                return False
            if niche and intelligence.niche != niche:
                return False
            if arabian and intelligence.arabian_classification != arabian:
                return False
            if price_band and intelligence.price_band != price_band:
                return False
            if concentration and intelligence.concentration != concentration:
                return False
            if fragrance_family and intelligence.fragrance_family != fragrance_family:
                return False
            if inventory_min is not None and item.product.inventory_total < inventory_min:
                return False
            if normalization_status and item.status != normalization_status:
                return False
            return True

        return [item for item in products if matches(item)]

    def _dry_run_report(
        self,
        db: Any,
        products: list[ProposalProduct],
        product_limit: int,
        max_proposals_per_product: int,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        products_by_id = {item.product.id: item for item in products}
        historical_ids: list[str] = []
        seen: set[str] = set()
        for product_id in db.scalars(
            select(PinConcept.product_id)
            .join(PinDraft, PinDraft.concept_id == PinConcept.id)
            .where(PinDraft.status == DraftStatus.READY_FOR_REVIEW)
            .order_by(PinConcept.created_at, PinConcept.id)
        ):
            if product_id in products_by_id and product_id not in seen:
                historical_ids.append(product_id)
                seen.add(product_id)
        if historical_ids and not filters:
            selected = [products_by_id[product_id] for product_id in historical_ids[:product_limit]]
            sample_source = "historical_review_batch"
        else:
            selected = _select_products(products, product_limit)
            sample_source = "representative_eligible_catalog"
        ranked_by_product, diagnostics = _rank_batch(
            selected,
            max_proposals_per_product,
        )
        previews = [
            _preview_proposal(item, ranked)
            for item in selected
            for ranked in ranked_by_product[item.product.id]
        ]
        return self._report(
            previews,
            products_selected=len(selected),
            duplicate_attempts=0,
            skipped_products=[],
            eligible_products_considered=len(products),
            not_selected_due_to_batch_limit=max(len(products) - len(selected), 0),
            diversity_diagnostics=diagnostics,
            sample_source=sample_source,
            dry_run=True,
        )

    def generate_controlled_batch(
        self,
        *,
        product_limit: int = MAX_CONTROLLED_PRODUCTS,
        max_proposals_per_product: int = MAX_PROPOSALS_PER_PRODUCT,
        filters: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        product_limit = min(max(product_limit, 1), MAX_CONTROLLED_PRODUCTS)
        max_proposals_per_product = min(max(max_proposals_per_product, 1), MAX_PROPOSALS_PER_PRODUCT)
        db = self.session_factory()
        duplicate_attempts = 0
        skipped_products: list[dict[str, str]] = []
        created: list[dict[str, Any]] = []
        try:
            all_products = self._filter(self._products(db), **(filters or {}))
            existing_product_ids = set(db.scalars(select(PinConcept.product_id)))
            unused = [item for item in all_products if item.product.id not in existing_product_ids]
            used = [item for item in all_products if item.product.id in existing_product_ids]
            selected = _select_products(unused, product_limit)
            if len(selected) < product_limit:
                selected.extend(_select_products(used, product_limit - len(selected)))
            candidates = [*unused, *used]
            selected_ids = {item.product.id for item in selected}
            not_selected_due_to_batch_limit = max(len(candidates) - len(selected_ids), 0)
            if dry_run:
                return self._dry_run_report(
                    db,
                    all_products,
                    product_limit,
                    max_proposals_per_product,
                    filters or {},
                )
            ranked_by_product, ranking_diagnostics = _rank_batch(
                selected,
                max_proposals_per_product,
            )
            for item in selected:
                ranked_angles = ranked_by_product[item.product.id]
                if not ranked_angles:
                    skipped_products.append({
                        "product_id": item.product.id,
                        "title": item.product.title,
                        "reason": "No evidence-gated content angle was available.",
                    })
                    continue
                for ranked in ranked_angles:
                    angle = ranked.angle
                    angle_row = _ensure_angle(db, angle)
                    template_row = _ensure_template(db, angle.template_key)
                    board_key = _board_key(item, angle.key)
                    board_row = _ensure_board(db, item.product.store_id, board_key)
                    keywords = tuple(dict.fromkeys(_keywords(item, angle.key)))
                    keyword_row = _ensure_keyword_cluster(db, keywords, angle)
                    campaign_row = _ensure_campaign(db, item.product.store_id)
                    keyword_cluster = "|".join(keywords)
                    fingerprint = concept_fingerprint(
                        product_ids=[item.product.id],
                        content_angle=angle.key,
                        keyword_cluster=keyword_cluster,
                        board_id=board_key,
                    )
                    if db.scalar(select(PinConcept).where(PinConcept.fingerprint == fingerprint)):
                        duplicate_attempts += 1
                        continue
                    copy = _copy(item, angle)
                    unsupported_claims = _unsupported_claims(copy, item)
                    warnings = []
                    if item.status != "COMPLETE":
                        warnings.append(
                            f"Normalization is {item.status}; unavailable fields were omitted from copy."
                        )
                    if _missing_facts(item):
                        warnings.append(
                            "Unavailable catalog facts were not used: "
                            + ", ".join(_missing_facts(item))
                            + "."
                        )
                    content_slug = f"{_slugify(item.product.handle or item.product.title)}-{angle.key}"
                    utm_url = build_pinterest_utm_url(
                        item.product.product_url,
                        campaign=CAMPAIGN_KEY,
                        content=content_slug,
                    )
                    rationale = {
                        "concept_id": None,
                        "headline": copy["headline"],
                        "cta": copy["cta"],
                        "content_angle": angle.label,
                        "content_angle_key": angle.key,
                        "creative_template": CREATIVE_TEMPLATES[angle.template_key],
                        "creative_template_key": angle.template_key,
                        "board_mapping": {
                            "key": board_key,
                            "name": board_row.name,
                            "pinterest_board_id": None,
                        },
                        "keywords": list(keywords),
                        "keyword_cluster": keyword_cluster,
                        "canonical_url": item.product.product_url,
                        "utm_url": utm_url,
                        "authentic_image": {
                            "id": item.image.id,
                            "url": item.image.source_url,
                            "source_sha256": item.image.source_sha256,
                        },
                        "facts_used": _facts_used(item, angle),
                        "warnings": warnings,
                        "missing_facts": _missing_facts(item),
                        "unsupported_claims": unsupported_claims,
                        "duplicate_fingerprint": fingerprint,
                        "variation_reason": None,
                        "campaign": campaign_row.slug,
                        "template_version": template_row.version,
                        "generated_status": "GENERATED",
                        "selection_reason": ranked.selection_reason,
                        "ranking_score": ranked.score,
                        "ranking_factors": ranked.factors,
                    }
                    concept = PinConcept(
                        store_id=item.product.store_id,
                        product_id=item.product.id,
                        content_angle_id=angle_row.id,
                        keyword_cluster_id=keyword_row.id,
                        board_id=board_row.id,
                        campaign_id=campaign_row.id,
                        fingerprint=fingerprint,
                        rationale=rationale,
                    )
                    db.add(concept)
                    db.flush()
                    rationale = {**rationale, "concept_id": concept.id}
                    concept.rationale = rationale
                    draft = PinDraft(
                        concept_id=concept.id,
                        version=1,
                        title=copy["title"],
                        description=copy["description"],
                        alt_text=copy["alt_text"],
                        destination_url=item.product.product_url,
                        utm_url=utm_url,
                        text_fingerprint=text_fingerprint(
                            title=copy["title"],
                            description=copy["description"],
                            alt_text=copy["alt_text"],
                        ),
                        status=DraftStatus.GENERATED,
                    )
                    db.add(draft)
                    db.flush()
                    # The generated proposal is immediately available for explicit human review.
                    draft.status = DraftStatus.READY_FOR_REVIEW
                    db.flush()
                    created.append(_serialize_proposal(item.product, item.intelligence, rationale, draft))
            db.commit()
            return self._report(
                created,
                products_selected=len(selected),
                duplicate_attempts=duplicate_attempts,
                skipped_products=skipped_products,
                eligible_products_considered=len(candidates),
                not_selected_due_to_batch_limit=not_selected_due_to_batch_limit,
                diversity_diagnostics=ranking_diagnostics,
                sample_source="new_ranked_generation",
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_proposals(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.execute(
                select(PinDraft, PinConcept, Product, ProductIntelligence)
                .join(PinConcept, PinConcept.id == PinDraft.concept_id)
                .join(Product, Product.id == PinConcept.product_id)
                .join(ProductIntelligence, ProductIntelligence.product_id == Product.id)
                .order_by(PinDraft.created_at.desc(), PinDraft.id)
                .limit(min(max(limit, 1), 100))
            ).all()
            results = []
            for draft, concept, product, intelligence in rows:
                api_status = "REVIEW" if draft.status == DraftStatus.READY_FOR_REVIEW else draft.status.value
                if status and api_status != status:
                    continue
                if search and search.lower() not in " ".join([
                    product.title,
                    product.vendor or "",
                    concept.rationale.get("content_angle", ""),
                ]).lower():
                    continue
                original_creative = db.scalar(
                    select(PinCreative)
                    .where(PinCreative.draft_id == draft.id)
                    .order_by(PinCreative.created_at.asc(), PinCreative.id)
                )
                versions, active, active_id = _version_context(
                    db, product, intelligence, concept.rationale or {}, draft, original_creative
                )
                results.append(_serialize_proposal(
                    product,
                    intelligence,
                    concept.rationale or {},
                    draft,
                    original_creative,
                    versions,
                    active,
                    active_id,
                ))
            return results
        finally:
            db.close()

    def decide(self, draft_id: str, decision: str, note: str | None = None) -> dict[str, Any]:
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("Decision must be APPROVED or REJECTED.")
        db = self.session_factory()
        try:
            draft = db.get(PinDraft, draft_id)
            if not draft:
                raise ValueError("Proposal was not found.")
            if draft.status != DraftStatus.READY_FOR_REVIEW:
                raise ValueError("Only proposals in REVIEW can be approved or rejected.")
            draft.status = DraftStatus.APPROVED if decision == "APPROVED" else DraftStatus.REJECTED
            db.add(PinApproval(
                draft_id=draft.id,
                decision=decision,
                decided_by="manual_dashboard_action",
                note=note,
            ))
            db.commit()
            return {"id": draft.id, "approval_status": decision, "publishing_enabled": False}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def qa_report(self) -> dict[str, Any]:
        proposals = self.list_proposals(limit=100)
        return self._report(
            proposals,
            products_selected=len({proposal["product_id"] for proposal in proposals}),
            duplicate_attempts=0,
            skipped_products=[],
            eligible_products_considered=len({proposal["product_id"] for proposal in proposals}),
            not_selected_due_to_batch_limit=0,
        )

    def summary(self) -> dict[str, Any]:
        db = self.session_factory()
        try:
            counts = Counter(db.scalars(select(PinDraft.status)))
            return {
                "total": sum(counts.values()),
                "review": counts[DraftStatus.READY_FOR_REVIEW],
                "approved": counts[DraftStatus.APPROVED],
                "rejected": counts[DraftStatus.REJECTED],
                "generated": counts[DraftStatus.GENERATED],
                "scheduled": 0,
                "publishing_enabled": False,
            }
        finally:
            db.close()

    @staticmethod
    def _report(
        proposals: list[dict[str, Any]],
        *,
        products_selected: int,
        duplicate_attempts: int,
        skipped_products: list[dict[str, str]],
        eligible_products_considered: int,
        not_selected_due_to_batch_limit: int,
        diversity_diagnostics: list[dict[str, Any]] | None = None,
        sample_source: str = "persisted_historical_batch",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        angle_distribution = Counter(proposal["content_angle"] for proposal in proposals)
        template_distribution = Counter(proposal["creative_template"] for proposal in proposals)
        board_distribution = Counter(proposal["intended_board"]["name"] for proposal in proposals)
        normalization_distribution = Counter(
            proposal["normalization_status"] for proposal in proposals
        )
        partial = normalization_distribution.get("PARTIAL", 0)
        unsupported = sorted({
            claim
            for proposal in proposals
            for claim in proposal.get("unsupported_claims", [])
        })
        diagnostics = diversity_diagnostics or []
        selected_angle_details = [
            {
                "product_id": product["product_id"],
                "product_title": product["product_title"],
                **candidate,
            }
            for product in diagnostics
            for candidate in product["candidate_angles"]
            if candidate["selected"]
        ]
        rejected_angle_details = [
            {
                "product_id": product["product_id"],
                "product_title": product["product_title"],
                **candidate,
            }
            for product in diagnostics
            for candidate in product["candidate_angles"]
            if not candidate["selected"]
        ]
        classification_coverage = {}
        for key, label in (
            ("designer-fragrance", "Designer Fragrance"),
            ("arabian-fragrance", "Arabian Fragrance"),
            ("niche-fragrance", "Niche Fragrance"),
        ):
            available = [item for item in selected_angle_details + rejected_angle_details if item["angle_key"] == key]
            selected_items = [item for item in available if item["selected"]]
            classification_coverage[label] = {
                "available_candidates": len(available),
                "selected": len(selected_items),
                "coverage": round(len(selected_items) / len(available), 4) if available else None,
            }
        if angle_distribution:
            dominant_angle, dominant_count = sorted(
                angle_distribution.items(),
                key=lambda item: (-item[1], item[0]),
            )[0]
            maximum_angle_share = {
                "angle": dominant_angle,
                "count": dominant_count,
                "share": round(dominant_count / len(proposals), 4),
            }
        else:
            maximum_angle_share = {"angle": None, "count": 0, "share": 0}
        return {
            "products_selected": products_selected,
            "eligible_products_considered": eligible_products_considered,
            "not_selected_due_to_batch_limit": not_selected_due_to_batch_limit,
            "proposals_generated": len(proposals),
            "duplicate_attempts_prevented": duplicate_attempts,
            "content_angle_distribution": dict(angle_distribution),
            "template_distribution": dict(template_distribution),
            "board_mapping_distribution": dict(board_distribution),
            "normalization_distribution": dict(normalization_distribution),
            "proposals_using_partial_records": partial,
            "unsupported_claims_detected": unsupported,
            "skipped_products": skipped_products,
            "representative_proposals": proposals[:15],
            "candidate_angle_diagnostics": diagnostics,
            "selected_angle_details": selected_angle_details,
            "rejected_candidate_angles": rejected_angle_details,
            "maximum_angle_share": maximum_angle_share,
            "classification_angle_coverage": classification_coverage,
            "sample_source": sample_source,
            "dry_run": dry_run,
            "mutations_performed": 0 if dry_run else len(proposals),
            "publishing_enabled": False,
        }
