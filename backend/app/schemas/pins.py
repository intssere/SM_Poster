from datetime import datetime
from pydantic import BaseModel, Field, HttpUrl


class ConceptFingerprintInput(BaseModel):
    product_ids: list[str]
    content_angle: str
    keyword_cluster: str | None = None
    board_id: str | None = None
    destination_type: str = "product"


class DraftCreate(BaseModel):
    product_id: str
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=800)
    alt_text: str = Field(min_length=1, max_length=500)
    destination_url: HttpUrl
    campaign: str = "pinterest_wave1"
    content_slug: str


class ScheduleRequest(BaseModel):
    publication_id: str
    scheduled_for: datetime


class ProposalGenerateRequest(BaseModel):
    category: str | None = None
    vendor: str | None = None
    audience: str | None = None
    designer: str | None = None
    niche: str | None = None
    arabian: str | None = None
    price_band: str | None = None
    concentration: str | None = None
    fragrance_family: str | None = None
    inventory_min: int | None = Field(default=None, ge=0)
    normalization_status: str | None = None
    product_limit: int = Field(default=20, ge=1, le=20)
    max_proposals_per_product: int = Field(default=2, ge=1, le=2)
    dry_run: bool = False


class ProposalDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class CreativeRenderBatchRequest(BaseModel):
    limit: int = Field(default=12, ge=1, le=12)
