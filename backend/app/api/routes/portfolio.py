from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services.pinterest_portfolio_planner import (
    PortfolioPlanningError,
    portfolio_preview,
)
from app.services.pinterest_seo_intelligence import (
    PinterestSeoError,
    seo_brief_preview,
)
from app.services.pinterest_autonomous_generation import (
    AutonomousGenerationError,
    autonomous_generation_readiness,
)
from app.services.pinterest_autonomous_execution import (
    AutonomousExecutionError,
    execution_readiness,
)
from app.services.pinterest_autonomous_destination import (
    AutonomousDestinationError,
    destination_readiness,
)
from app.services.pinterest_phase_b1_operator import (
    PhaseB1OperatorError,
    execute_phase_b1,
    phase_b1_readiness,
)
from app.services.pinterest_phase_b2_operator import (
    PhaseB2OperatorError,
    execute_phase_b2,
    phase_b2_readiness,
)
from app.services.product_source_hydration import (
    ProductSourceHydrationError,
    hydrate_product_source,
    product_source_readiness,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


class PhaseB1Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=36)
    month_key: str = Field(pattern=r"^\d{4}-\d{2}$")
    target_pins: int = Field(ge=1, le=10000)
    expected_preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_existing_commitments: int = Field(ge=0)
    expected_active_slots: int = Field(ge=0)
    expected_reserve_slots: int = Field(ge=0)


class PhaseB2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_item_id: str = Field(min_length=1, max_length=36)
    expected_plan_id: str = Field(min_length=1, max_length=36)
    expected_optimizer_application_id: str = Field(min_length=1, max_length=36)
    expected_item_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_destination_input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_execution_input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_seo_input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_seo_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_scheduled_for: datetime
    expected_pinterest_board_record_id: str = Field(min_length=1, max_length=36)
    expected_external_board_id: str = Field(min_length=1, max_length=255)


class ProductSourceHydrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_image_id: str = Field(min_length=1, max_length=36)
    expected_product_id: str = Field(min_length=1, max_length=36)
    expected_source_url: str = Field(min_length=1, max_length=4096)
    expected_shopify_media_id: str = Field(min_length=1, max_length=64)
    expected_current_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


@router.get("/preview")
def preview_monthly_portfolio(
    month_key: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    store_id: str | None = None,
    target_pins: int | None = Query(default=None, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    try:
        return portfolio_preview(
            db,
            month_key=month_key,
            store_id=store_id,
            target_pins=target_pins,
            settings=get_settings(),
        )
    except PortfolioPlanningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/canary/phase-b1-readiness")
def preview_phase_b1_canary(
    store_id: str = Query(..., min_length=1, max_length=36),
    month_key: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    target_pins: int = Query(..., ge=1, le=10000),
    db: Session = Depends(get_db),
):
    try:
        return phase_b1_readiness(
            db,
            store_id=store_id,
            month_key=month_key,
            target_pins=target_pins,
            settings=get_settings(),
        )
    except PhaseB1OperatorError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None


@router.post("/canary/phase-b1")
def run_phase_b1_canary(
    payload: PhaseB1Request,
    db: Session = Depends(get_db),
):
    try:
        return execute_phase_b1(
            db,
            store_id=payload.store_id,
            month_key=payload.month_key,
            target_pins=payload.target_pins,
            expected_preview_fingerprint=payload.expected_preview_fingerprint,
            expected_input_fingerprint=payload.expected_input_fingerprint,
            expected_existing_commitments=payload.expected_existing_commitments,
            expected_active_slots=payload.expected_active_slots,
            expected_reserve_slots=payload.expected_reserve_slots,
            settings=get_settings(),
        )
    except PhaseB1OperatorError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None


@router.get("/canary/product-source-readiness")
def preview_product_source_canary(
    product_image_id: str = Query(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
):
    try:
        return product_source_readiness(
            db,
            product_image_id=product_image_id,
            settings=get_settings(),
        )
    except ProductSourceHydrationError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None


@router.post("/canary/product-source-hydrate")
def hydrate_product_source_canary(
    payload: ProductSourceHydrationRequest,
    db: Session = Depends(get_db),
):
    try:
        return hydrate_product_source(
            db,
            product_image_id=payload.product_image_id,
            expected_product_id=payload.expected_product_id,
            expected_source_url=payload.expected_source_url,
            expected_shopify_media_id=payload.expected_shopify_media_id,
            expected_current_source_sha256=payload.expected_current_source_sha256,
            settings=get_settings(),
        )
    except ProductSourceHydrationError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None


@router.get("/canary/phase-b2-readiness")
def preview_phase_b2_canary(
    portfolio_item_id: str = Query(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
):
    try:
        return phase_b2_readiness(
            db,
            portfolio_item_id=portfolio_item_id,
            settings=get_settings(),
        )
    except PhaseB2OperatorError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None


@router.post("/canary/phase-b2")
async def run_phase_b2_canary(
    payload: PhaseB2Request,
    db: Session = Depends(get_db),
):
    try:
        return await execute_phase_b2(
            db,
            portfolio_item_id=payload.portfolio_item_id,
            expected_plan_id=payload.expected_plan_id,
            expected_optimizer_application_id=payload.expected_optimizer_application_id,
            expected_item_fingerprint=payload.expected_item_fingerprint,
            expected_destination_input_fingerprint=payload.expected_destination_input_fingerprint,
            expected_execution_input_fingerprint=payload.expected_execution_input_fingerprint,
            expected_seo_input_fingerprint=payload.expected_seo_input_fingerprint,
            expected_seo_fingerprint=payload.expected_seo_fingerprint,
            expected_scheduled_for=payload.expected_scheduled_for,
            expected_pinterest_board_record_id=payload.expected_pinterest_board_record_id,
            expected_external_board_id=payload.expected_external_board_id,
            settings=get_settings(),
        )
    except PhaseB2OperatorError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None


@router.get("/items/{portfolio_item_id}/seo-preview")
def preview_portfolio_item_seo(
    portfolio_item_id: str,
    db: Session = Depends(get_db),
):
    try:
        return seo_brief_preview(
            db,
            portfolio_item_id,
            settings=get_settings(),
        )
    except PinterestSeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/items/{portfolio_item_id}/generation-readiness")
def preview_autonomous_generation(
    portfolio_item_id: str,
    db: Session = Depends(get_db),
):
    try:
        return autonomous_generation_readiness(
            db,
            portfolio_item_id,
            settings=get_settings(),
        )
    except AutonomousGenerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/items/{portfolio_item_id}/execution-readiness")
def preview_autonomous_execution(
    portfolio_item_id: str,
    db: Session = Depends(get_db),
):
    try:
        return execution_readiness(
            db,
            portfolio_item_id,
            settings=get_settings(),
        )
    except AutonomousExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/items/{portfolio_item_id}/destination-readiness")
def preview_autonomous_destination(
    portfolio_item_id: str,
    db: Session = Depends(get_db),
):
    try:
        return destination_readiness(
            db,
            portfolio_item_id,
            settings=get_settings(),
        )
    except AutonomousDestinationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
