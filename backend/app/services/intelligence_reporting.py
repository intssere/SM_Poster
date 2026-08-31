from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from app.models.domain import Product, ProductIntelligence
from app.services.product_intelligence import (
    CATEGORY_BATH_BODY,
    CATEGORY_BEAUTY,
    CATEGORY_FRAGRANCE,
    CATEGORY_GIFT_SET,
    CATEGORY_HOME_FRAGRANCE,
    CATEGORY_OTHER,
    NORMALIZATION_FIELDS,
)


CATEGORY_ORDER = (
    CATEGORY_FRAGRANCE,
    CATEGORY_GIFT_SET,
    CATEGORY_BATH_BODY,
    CATEGORY_BEAUTY,
    CATEGORY_HOME_FRAGRANCE,
    CATEGORY_OTHER,
)


def _present(value: Any) -> bool:
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return value is not None and str(value).strip() != ""


def build_intelligence_report(
    db: Any,
    sample_limit: int = 0,
) -> dict[str, Any]:
    rows = list(db.execute(
        select(Product, ProductIntelligence)
        .join(ProductIntelligence, ProductIntelligence.product_id == Product.id)
        .order_by(Product.title, Product.id)
    ))
    status_counts = {"COMPLETE": 0, "PARTIAL": 0, "UNKNOWN": 0}
    field_populated = {field: 0 for field in NORMALIZATION_FIELDS}
    category_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "COMPLETE": 0, "PARTIAL": 0, "UNKNOWN": 0}
    )
    warning_products = 0
    samples_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for product, intelligence in rows:
        normalized = intelligence.normalized_data or {}
        category = normalized.get("normalization_category", CATEGORY_OTHER)
        status = intelligence.normalization_status or "UNKNOWN"
        if status not in status_counts:
            status = "UNKNOWN"
        status_counts[status] += 1
        category_counts[category]["total"] += 1
        category_counts[category][status] += 1
        for field in NORMALIZATION_FIELDS:
            if _present(getattr(intelligence, field)):
                field_populated[field] += 1
        warnings = normalized.get("qa_warnings") or []
        if warnings:
            warning_products += 1
        if sample_limit:
            samples_by_category[category].append({
                "title": product.title,
                "vendor": product.vendor,
                "product_type": product.product_type,
                "price": float(product.price_min) if product.price_min is not None else None,
                "inventory": product.inventory_total,
                "normalization_category": category,
                "normalization_status": status,
                "missing_required_fields": normalized.get("missing_required_fields", []),
                "audience": intelligence.audience,
                "size": intelligence.size,
                "concentration": intelligence.concentration,
                "fragrance_family": intelligence.fragrance_family,
                "designer": intelligence.designer,
                "niche": intelligence.niche,
                "arabian_classification": intelligence.arabian_classification,
                "gift_suitability": intelligence.gift_suitability,
                "eligibility_status": intelligence.eligibility_status,
                "eligibility_blocking_reasons": normalized.get(
                    "eligibility_blocking_reasons", []
                ),
                "qa_warnings": warnings,
            })

    samples = []
    if sample_limit:
        indexes = defaultdict(int)
        while len(samples) < sample_limit:
            added = False
            for category in CATEGORY_ORDER:
                index = indexes[category]
                category_samples = samples_by_category.get(category, [])
                if index < len(category_samples) and len(samples) < sample_limit:
                    samples.append(category_samples[index])
                    indexes[category] += 1
                    added = True
            if not added:
                break

    return {
        "total": len(rows),
        "normalization_status": status_counts,
        "field_populated": field_populated,
        "categories": {
            category: category_counts[category]
            for category in CATEGORY_ORDER
            if category_counts[category]["total"]
        },
        "qa_warning_products": warning_products,
        "representative_products": samples,
    }