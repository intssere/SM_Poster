"""Authenticated retrieval of the durable sanitized READY-batch receipt."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.readiness_execution_auth import NO_STORE, require_real_admin
from app.db.session import get_db
from app.services.ready_bounded_batch_receipt import ReadyReceiptError, latest_receipt


router = APIRouter(
    prefix="/internal/operations/bounded-pilot-ready-receipt",
    tags=["management"],
    redirect_slashes=False,
)


async def _require_empty_request(request: Request) -> None:
    if request.query_params:
        raise ReadyReceiptError("REQUEST_INPUTS_PROHIBITED")
    async for chunk in request.stream():
        if chunk:
            raise ReadyReceiptError("REQUEST_INPUTS_PROHIBITED")


@router.get("")
async def get_ready_receipt(
    request: Request,
    username: str = Depends(require_real_admin),
    db: Session = Depends(get_db),
):
    del username
    try:
        await _require_empty_request(request)
        result = latest_receipt(db)
        if result is None:
            return JSONResponse(
                {
                    "success": False,
                    "code": "READY_RECEIPT_NOT_FOUND",
                    "publishing_admission": "NOT_GRANTED",
                },
                status_code=404,
                headers=NO_STORE,
            )
        return JSONResponse(
            {
                "success": True,
                "receipt_id": result["receipt_id"],
                "receipt": result["receipt"],
                "publishing_admission": "NOT_GRANTED",
            },
            status_code=200,
            headers=NO_STORE,
        )
    except ReadyReceiptError as exc:
        return JSONResponse(
            {
                "success": False,
                "code": exc.code,
                "publishing_admission": "NOT_GRANTED",
            },
            status_code=409,
            headers=NO_STORE,
        )
    except Exception:
        return JSONResponse(
            {
                "success": False,
                "code": "READY_RECEIPT_UNEXPECTED_ERROR",
                "publishing_admission": "NOT_GRANTED",
            },
            status_code=500,
            headers=NO_STORE,
        )
