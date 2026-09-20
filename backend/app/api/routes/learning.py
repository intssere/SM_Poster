from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services.pinterest_learning_ranking import (
    LearningError,
    learning_preview,
)

router = APIRouter(prefix="/pinterest/learning", tags=["pinterest-learning"])


@router.get("/preview")
def pinterest_learning_preview(
    store_id: str | None = None,
    as_of_at: datetime | None = None,
    db: Session = Depends(get_db),
):
    try:
        return learning_preview(
            db,
            store_id=store_id,
            as_of_at=as_of_at,
            settings=get_settings(),
        )
    except LearningError as exc:
        if exc.code == "STORE_NOT_FOUND":
            raise HTTPException(status_code=404, detail=exc.code) from None
        raise HTTPException(status_code=400, detail=exc.code) from None
