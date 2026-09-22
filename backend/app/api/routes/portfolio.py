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
