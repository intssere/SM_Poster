from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services.pinterest_portfolio_planner import (
    PortfolioPlanningError,
    portfolio_preview,
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
