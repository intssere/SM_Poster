import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.db.session import get_db
from app.services.buffer_publication_reconciliation import BufferReconciliationError, reconcile_buffer
from app.services.pinterest_publisher import PublicationReconciliationError


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/publications", tags=["publications"])
BUFFER_RECONCILIATION_CONFIRMATION_VERSION = "BUFFER_RECONCILIATION_V1"


class BufferReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool
    confirmation_text_version: str


@router.post("/{publication_id}/reconcile-buffer")
async def reconcile_known_buffer_operation(
    publication_id: str,
    request: Request,
    payload: BufferReconciliationRequest,
    db: Session = Depends(get_db),
):
    """Read exactly one known Buffer operation and atomically reconcile its outcome.

    This endpoint has no dispatch dependency and cannot create, retry, schedule, or
    authorize a provider write. It delegates only to the exact-operation Buffer
    reconciliation service.
    """
    actor = current_user(request)
    if not actor:
        raise HTTPException(401, "Authentication required")
    if not payload.confirmed:
        raise HTTPException(422, "CONFIRMATION_REQUIRED")
    if payload.confirmation_text_version != BUFFER_RECONCILIATION_CONFIRMATION_VERSION:
        raise HTTPException(422, "INVALID_RECONCILIATION_CONFIRMATION_VERSION")
    try:
        row = await reconcile_buffer(db, publication_id, actor=actor)
    except BufferReconciliationError as exc:
        diagnostic = exc.safe_diagnostic()
        logger.warning(
            "buffer_reconciliation_rejected",
            extra={
                "buffer_reconciliation_code": diagnostic.get("code"),
                "buffer_reconciliation_stage": diagnostic.get("stage"),
                "buffer_reconciliation_field": diagnostic.get("field"),
                "buffer_publication_id": publication_id,
            },
        )
        headers = {}
        if diagnostic.get("stage"):
            headers["X-Buffer-Reconciliation-Stage"] = diagnostic["stage"]
        if diagnostic.get("field"):
            headers["X-Buffer-Reconciliation-Field"] = diagnostic["field"]
        raise HTTPException(409, str(exc), headers=headers) from None
    except PublicationReconciliationError as exc:
        raise HTTPException(500, str(exc)) from None
    return {
        "id": row.id,
        "status": row.status.value if hasattr(row.status, "value") else row.status,
        "pinterest_pin_id": row.pinterest_pin_id,
        "published_at": row.published_at,
        "error_code": row.error_code,
    }
