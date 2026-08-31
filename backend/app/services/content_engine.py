from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class ProductFacts:
    product_id: str
    title: str
    vendor: str | None
    product_type: str | None
    price: Decimal | None
    inventory_total: int
    product_url: str
    gender: str | None = None
    fragrance_family: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    is_arabian: bool = False
    is_niche: bool = False
    is_designer: bool = False
    is_gift_set: bool = False
    is_home_fragrance: bool = False
    is_new_arrival: bool = False


@dataclass(frozen=True)
class ContentProposal:
    angle_key: str
    angle_label: str
    board_key: str
    keywords: tuple[str, ...]
    reason: str


NOTE_KEYWORDS = {
    "vanilla": ("vanilla perfume", "vanilla fragrance", "sweet perfume"),
    "oud": ("oud perfume", "oud fragrance", "arabian perfume"),
    "lavender": ("lavender perfume", "aromatic fragrance"),
    "rose": ("rose perfume", "floral fragrance"),
    "amber": ("amber perfume", "warm fragrance"),
    "musk": ("musk perfume", "musky fragrance"),
}


def _normalized_notes(notes: Iterable[str]) -> set[str]:
    return {n.strip().lower() for n in notes if n.strip()}


def propose_content(product: ProductFacts, *, limit: int = 5) -> list[ContentProposal]:
    if product.inventory_total <= 0:
        return []

    proposals: list[ContentProposal] = []
    vendor = (product.vendor or "").strip()
    gender = (product.gender or "").strip().lower()
    notes = _normalized_notes(product.notes)

    board = "designer-fragrances" if product.is_designer else "fragrance-guides"
    if product.is_arabian:
        board = "arabian-fragrance"
    elif product.is_niche:
        board = "niche-fragrances"
    elif gender == "women":
        board = "womens-fragrance"
    elif gender == "men":
        board = "mens-fragrance"
    elif product.is_gift_set:
        board = "gift-sets"
    elif product.is_home_fragrance:
        board = "home-fragrance"

    base_keywords = tuple(x for x in [vendor.lower() + " perfume" if vendor else None, "fragrance", "perfume"] if x)
    proposals.append(ContentProposal(
        angle_key="product-spotlight",
        angle_label="Product Spotlight",
        board_key=board,
        keywords=base_keywords,
        reason="Eligible in-stock product with a direct product destination.",
    ))

    if gender in {"women", "men", "unisex"}:
        label = {"women": "Women's Fragrance Pick", "men": "Men's Fragrance Pick", "unisex": "Unisex Fragrance Pick"}[gender]
        proposals.append(ContentProposal(
            angle_key=f"{gender}-fragrance-pick",
            angle_label=label,
            board_key={"women": "womens-fragrance", "men": "mens-fragrance", "unisex": "fragrance-guides"}[gender],
            keywords=(f"{gender} fragrance", f"{gender} perfume", "fragrance picks"),
            reason=f"Catalog explicitly identifies the product as {gender}.",
        ))

    if product.is_arabian:
        proposals.append(ContentProposal(
            angle_key="arabian-fragrance-discovery",
            angle_label="Arabian Fragrance Discovery",
            board_key="arabian-fragrance",
            keywords=("arabian perfume", "middle eastern fragrance", "arabian fragrance"),
            reason="Product taxonomy explicitly classifies the item as Arabian fragrance.",
        ))

    if product.is_new_arrival:
        proposals.append(ContentProposal(
            angle_key="new-arrival",
            angle_label="New Fragrance Arrival",
            board_key="new-arrivals",
            keywords=("new perfume", "new fragrance", "fragrance new arrivals"),
            reason="Product is explicitly marked as a new arrival.",
        ))

    for note, keywords in NOTE_KEYWORDS.items():
        if note in notes:
            proposals.append(ContentProposal(
                angle_key=f"note-{note}",
                angle_label=f"{note.title()} Fragrance",
                board_key="scent-families",
                keywords=keywords,
                reason=f"The product catalog explicitly includes the note '{note}'.",
            ))

    if product.is_gift_set:
        proposals.append(ContentProposal(
            angle_key="gift-idea",
            angle_label="Fragrance Gift Idea",
            board_key="gift-sets",
            keywords=("perfume gift set", "fragrance gift idea", "perfume gifts"),
            reason="Product taxonomy explicitly identifies a gift set.",
        ))

    if product.price is not None:
        for threshold in (50, 100):
            if product.price <= threshold:
                proposals.append(ContentProposal(
                    angle_key=f"under-{threshold}",
                    angle_label=f"Fragrance Under ${threshold}",
                    board_key="fragrance-guides",
                    keywords=(f"perfume under ${threshold}", f"fragrance under ${threshold}", "affordable fragrance"),
                    reason=f"Current catalog price is at or below ${threshold}.",
                ))
                break

    # Stable order + uniqueness by angle key.
    seen: set[str] = set()
    unique: list[ContentProposal] = []
    for p in proposals:
        if p.angle_key not in seen:
            seen.add(p.angle_key)
            unique.append(p)
    return unique[:limit]
