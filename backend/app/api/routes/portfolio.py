from fastapi import APIRouter, Depends, HTTPException, Query
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

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


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
