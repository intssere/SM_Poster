from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services.pinterest_adaptive_optimizer import OptimizerError, optimizer_preview

router = APIRouter(prefix="/pinterest/optimizer", tags=["pinterest-optimizer"])


@router.get("/preview")
def pinterest_optimizer_preview(
    plan_id: str,
    as_of_at: datetime | None = None,
    db: Session = Depends(get_db),
):
    try:
        return optimizer_preview(
            db,
            plan_id,
            settings=get_settings(),
            as_of_at=as_of_at,
        )
    except OptimizerError as exc:
        if exc.code == "PORTFOLIO_PLAN_NOT_FOUND":
            raise HTTPException(status_code=404, detail=exc.code) from None
        raise HTTPException(status_code=400, detail=exc.code) from None
