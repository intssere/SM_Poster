"""Real-admin operator endpoint for preflight-bound five-Pin preparation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.readiness_execution_auth import NO_STORE, require_real_admin
from app.db.session import get_db
from app.services.bounded_pilot_preparation_operator import (
    PREPARATION_CONTRACT,
    BoundedPreparationOperatorError,
    prepare_certified_batch,
)


router = APIRouter(
    prefix="/internal/operations/bounded-pilot-preparation",
    tags=["management"],
    redirect_slashes=False,
)


class CandidateReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=36)
    item_fingerprint: str = Field(min_length=64, max_length=64)
    candidate_fingerprint: str = Field(min_length=64, max_length=64)
    product_id: str = Field(min_length=1, max_length=36)
    local_board_id: str = Field(min_length=1, max_length=36)
    pinterest_board_record_id: str = Field(min_length=1, max_length=36)
    external_board_id: str = Field(min_length=1, max_length=255)
    content_angle_id: str = Field(min_length=1, max_length=36)
    planned_date: str = Field(min_length=10, max_length=10)
    slot_index: int = Field(ge=0)
    candidate_identity_fingerprint: str = Field(min_length=64, max_length=64)


class PreflightReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: str
    database_revision: str
    month_start: str = Field(min_length=10, max_length=10)
    current_date: str = Field(min_length=10, max_length=10)
    plan_id: str = Field(min_length=1, max_length=36)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    candidates: list[CandidateReceipt] = Field(min_length=5, max_length=5)
    preflight_fingerprint: str = Field(min_length=64, max_length=64)


class PreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = Field(strict=True)
    confirmation_text_version: str
    preflight: PreflightReceipt


@router.post("")
def prepare_bounded_pilot(
    payload: PreparationRequest,
    request: Request,
    username: str = Depends(require_real_admin),
    db: Session = Depends(get_db),
):
    if request.query_params:
        return JSONResponse(
            {
                "success": False,
                "contract": PREPARATION_CONTRACT,
                "code": "REQUEST_QUERY_INPUTS_PROHIBITED",
                "publishing_admission": "NOT_GRANTED",
            },
            status_code=400,
            headers=NO_STORE,
        )
    if (
        payload.confirmed is not True
        or payload.confirmation_text_version != PREPARATION_CONTRACT
    ):
        return JSONResponse(
            {
                "success": False,
                "contract": PREPARATION_CONTRACT,
                "code": "EXPLICIT_PREPARATION_CONFIRMATION_REQUIRED",
                "publishing_admission": "NOT_GRANTED",
            },
            status_code=422,
            headers=NO_STORE,
        )
    try:
        result = prepare_certified_batch(
            db,
            settings=get_settings(),
            actor=username,
            receipt=payload.preflight.model_dump(),
        )
    except BoundedPreparationOperatorError as exc:
        db.rollback()
        return JSONResponse(
            {
                "success": False,
                "contract": PREPARATION_CONTRACT,
                "code": exc.code,
                "publishing_admission": "NOT_GRANTED",
            },
            status_code=409,
            headers=NO_STORE,
        )
    except Exception:
        db.rollback()
        return JSONResponse(
            {
                "success": False,
                "contract": PREPARATION_CONTRACT,
                "code": "BOUNDED_PREPARATION_UNEXPECTED_ERROR",
                "publishing_admission": "NOT_GRANTED",
            },
            status_code=500,
            headers=NO_STORE,
        )
    return JSONResponse(result, status_code=200, headers=NO_STORE)
