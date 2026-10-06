"""Authenticated, strictly read-only five-Pin bounded preflight operator route."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core.readiness_execution_auth import NO_STORE, require_real_admin
from app.state_transfer.bounded_pilot_preflight import run as run_bounded_preflight


router = APIRouter(
    prefix="/internal/operations/bounded-pilot-preflight",
    tags=["management"],
    redirect_slashes=False,
)


async def _require_empty_request(request: Request) -> None:
    if request.query_params:
        raise ValueError("REQUEST_INPUTS_PROHIBITED")
    async for chunk in request.stream():
        if chunk:
            raise ValueError("REQUEST_INPUTS_PROHIBITED")


@router.get("")
async def bounded_pilot_preflight(
    request: Request,
    username: str = Depends(require_real_admin),
):
    """Return the exact sanitized five-candidate dossier; never mutate or call providers."""
    del username
    try:
        await _require_empty_request(request)
    except ValueError:
        return JSONResponse(
            {
                "success": False,
                "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
                "terminal_stage": "REQUEST",
                "bounded_preflight_certification": "NOT_GRANTED",
                "publishing_admission": "NOT_GRANTED",
                "code": "REQUEST_INPUTS_PROHIBITED",
            },
            status_code=400,
            headers=NO_STORE,
        )

    try:
        result = await run_in_threadpool(run_bounded_preflight)
    except Exception:
        result = {
            "success": False,
            "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
            "terminal_stage": "UNEXPECTED",
            "bounded_preflight_certification": "NOT_GRANTED",
            "publishing_admission": "NOT_GRANTED",
        }

    status_code = 200 if (
        result.get("success") is True
        and result.get("bounded_preflight_certification") == "PASS"
        and result.get("publishing_admission") == "NOT_GRANTED"
    ) else 409
    return JSONResponse(result, status_code=status_code, headers=NO_STORE)
