from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.pinterest_performance_analytics import (
    AnalyticsError,
    analytics_readiness,
    analytics_summary,
)

router = APIRouter(prefix="/pinterest/analytics", tags=["pinterest-analytics"])


@router.get("/{publication_id}/readiness")
def publication_analytics_readiness(
    publication_id: str,
    db: Session = Depends(get_db),
):
    try:
        return analytics_readiness(db, publication_id)
    except AnalyticsError as exc:
        if exc.code == "PUBLICATION_NOT_FOUND":
            raise HTTPException(status_code=404, detail=exc.code) from None
        raise HTTPException(status_code=400, detail=exc.code) from None


@router.get("/{publication_id}/summary")
def publication_analytics_summary(
    publication_id: str,
    db: Session = Depends(get_db),
):
    try:
        return analytics_summary(db, publication_id)
    except AnalyticsError as exc:
        if exc.code == "PUBLICATION_NOT_FOUND":
            raise HTTPException(status_code=404, detail=exc.code) from None
        raise HTTPException(status_code=400, detail=exc.code) from None
