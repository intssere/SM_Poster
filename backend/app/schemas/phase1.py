from decimal import Decimal
from pydantic import BaseModel, Field, HttpUrl


class ProductFactsRequest(BaseModel):
    product_id: str
    title: str
    vendor: str | None = None
    product_type: str | None = None
    price: Decimal | None = None
    inventory_total: int = 0
    product_url: HttpUrl
    gender: str | None = None
    fragrance_family: str | None = None
    notes: list[str] = Field(default_factory=list)
    is_arabian: bool = False
    is_niche: bool = False
    is_designer: bool = False
    is_gift_set: bool = False
    is_home_fragrance: bool = False
    is_new_arrival: bool = False
    campaign: str = "pinterest_wave1"
