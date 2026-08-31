from dataclasses import dataclass
from app.services.content_engine import ContentProposal, ProductFacts


@dataclass(frozen=True)
class PinCopy:
    title: str
    description: str
    alt_text: str


def _clean(value: str, max_len: int) -> str:
    value = " ".join(value.split()).strip()
    return value[:max_len].rstrip()


def generate_fact_safe_copy(product: ProductFacts, proposal: ContentProposal) -> PinCopy:
    """Deterministic baseline. It only phrases facts supplied by the catalog layer."""
    vendor = product.vendor.strip() if product.vendor else ""
    product_name = product.title.strip()

    if proposal.angle_key.startswith("note-"):
        note = proposal.angle_key.removeprefix("note-").title()
        title = f"{product_name} | {note} Fragrance"
        description = f"Explore {product_name} at Diamond Shelf. A {note.lower()}-focused fragrance pick based on the product's listed scent notes. Shop the authentic product and see current availability."
    elif proposal.angle_key == "arabian-fragrance-discovery":
        title = f"{product_name} | Arabian Fragrance Discovery"
        description = f"Discover {product_name} from {vendor or 'Diamond Shelf'}. Explore this Arabian fragrance, view current pricing and availability, and shop the authentic product at Diamond Shelf."
    elif proposal.angle_key == "gift-idea":
        title = f"{product_name} | Fragrance Gift Idea"
        description = f"Considering a fragrance gift? Explore {product_name} at Diamond Shelf, with current product details, pricing and availability on the product page."
    elif proposal.angle_key.startswith("under-"):
        threshold = proposal.angle_key.split("-")[-1]
        title = f"{product_name} | Fragrance Under ${threshold}"
        description = f"Explore {product_name} at Diamond Shelf. Its current catalog price qualifies for this under-${threshold} fragrance edit. Check the product page for current price and availability."
    elif proposal.angle_key == "new-arrival":
        title = f"New Arrival: {product_name}"
        description = f"See {product_name}, a new fragrance arrival at Diamond Shelf. View the authentic product, current pricing and availability on the product page."
    else:
        title = f"{product_name} | {proposal.angle_label}"
        description = f"Explore {product_name}{f' by {vendor}' if vendor else ''} at Diamond Shelf. View the authentic product, current pricing and availability, and discover more fragrance picks for your collection."

    alt_parts = [product_name]
    if vendor and vendor.lower() not in product_name.lower():
        alt_parts.append(f"by {vendor}")
    alt_parts.append("product image for Diamond Shelf Pinterest editorial pin")

    return PinCopy(
        title=_clean(title, 100),
        description=_clean(description, 500),
        alt_text=_clean(" ".join(alt_parts), 500),
    )
