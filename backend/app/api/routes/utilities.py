from fastapi import APIRouter
from pydantic import BaseModel, HttpUrl

from app.schemas.pins import ConceptFingerprintInput
from app.services.fingerprints import concept_fingerprint
from app.services.scoring import ProductSignals, score_product
from app.services.utm import build_pinterest_utm_url

router = APIRouter(prefix="/utilities", tags=["utilities"])


class UtmRequest(BaseModel):
    destination_url: HttpUrl
    campaign: str
    content: str


class ScoreRequest(BaseModel):
    inventory_available: bool
    image_quality: float = 0
    brand_recognition: float = 0
    seasonal_relevance: float = 0
    gift_relevance: float = 0
    newness: float = 0
    manual_priority: float = 0
    content_coverage_gap: float = 0


@router.post("/concept-fingerprint")
def fingerprint(body: ConceptFingerprintInput):
    return {"fingerprint": concept_fingerprint(**body.model_dump())}


@router.post("/utm")
def utm(body: UtmRequest):
    return {"url": build_pinterest_utm_url(str(body.destination_url), campaign=body.campaign, content=body.content)}


@router.post("/product-score")
def product_score(body: ScoreRequest):
    return {"score": score_product(ProductSignals(**body.model_dump()))}
