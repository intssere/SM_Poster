from dataclasses import dataclass


@dataclass(frozen=True)
class ProductSignals:
    inventory_available: bool
    image_quality: float = 0.0       # 0..1
    brand_recognition: float = 0.0   # 0..1
    seasonal_relevance: float = 0.0  # 0..1
    gift_relevance: float = 0.0      # 0..1
    newness: float = 0.0             # 0..1
    manual_priority: float = 0.0     # 0..1
    content_coverage_gap: float = 0.0 # 0..1; 1 = under-covered


WEIGHTS = {
    "image_quality": 0.22,
    "brand_recognition": 0.16,
    "seasonal_relevance": 0.12,
    "gift_relevance": 0.10,
    "newness": 0.10,
    "manual_priority": 0.12,
    "content_coverage_gap": 0.18,
}


def score_product(signals: ProductSignals) -> float:
    if not signals.inventory_available:
        return 0.0
    score = sum(getattr(signals, key) * weight for key, weight in WEIGHTS.items())
    return round(max(0.0, min(score, 1.0)) * 100, 2)
