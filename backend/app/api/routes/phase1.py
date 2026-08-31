import re
from fastapi import APIRouter

from app.schemas.phase1 import ProductFactsRequest
from app.services.board_mapping import display_board_name
from app.services.content_engine import ProductFacts, propose_content
from app.services.copy_engine import generate_fact_safe_copy
from app.services.fingerprints import concept_fingerprint, text_fingerprint
from app.services.utm import build_pinterest_utm_url

router = APIRouter(prefix="/phase1", tags=["phase1"])


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80]


@router.post("/propose")
def propose(body: ProductFactsRequest):
    facts_data = body.model_dump(exclude={"campaign"})
    facts_data["product_url"] = str(facts_data["product_url"])
    facts_data["notes"] = tuple(facts_data["notes"])
    product = ProductFacts(**facts_data)
    proposals = propose_content(product)

    results = []
    for proposal in proposals:
        copy = generate_fact_safe_copy(product, proposal)
        content_slug = f"{slugify(product.title)}-{proposal.angle_key}"
        destination = str(body.product_url)
        results.append({
            "angle_key": proposal.angle_key,
            "angle": proposal.angle_label,
            "board_key": proposal.board_key,
            "board": display_board_name(proposal.board_key),
            "keywords": proposal.keywords,
            "reason": proposal.reason,
            "title": copy.title,
            "description": copy.description,
            "alt_text": copy.alt_text,
            "destination_url": destination,
            "utm_url": build_pinterest_utm_url(destination, campaign=body.campaign, content=content_slug),
            "concept_fingerprint": concept_fingerprint(
                product_ids=[product.product_id], content_angle=proposal.angle_key,
                keyword_cluster="|".join(proposal.keywords), board_id=proposal.board_key,
            ),
            "text_fingerprint": text_fingerprint(title=copy.title, description=copy.description, alt_text=copy.alt_text),
        })
    return {"product_id": product.product_id, "count": len(results), "proposals": results}
