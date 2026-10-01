"""No startup/health hook: an explicit, default-disabled management operation."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.readiness_execution_auth import (
    CONFIRMATION, CONFIRM_HEADER, GRANT_HEADER, NO_STORE, binding_from_settings,
    require_real_admin, verify_authorization,
)
from app.services.readiness_execution import (
    execute_readiness, lookup_readiness, require_runtime_binding, require_static_safety,
)
from app.services.readiness_execution_contract import ReadinessError

router = APIRouter(prefix="/internal/operations/object-storage-readiness",
                   tags=["management"], redirect_slashes=False)


async def _empty_request(request: Request) -> None:
    if request.query_params:
        raise ReadinessError("REQUEST_INPUTS_PROHIBITED", 400)
    async for chunk in request.stream():
        if chunk:
            raise ReadinessError("REQUEST_INPUTS_PROHIBITED", 400)


def _refusal(error: ReadinessError) -> JSONResponse:
    return JSONResponse({"code": error.code}, status_code=error.status_code, headers=NO_STORE)


@router.post("")
async def execute(request: Request, username: str = Depends(require_real_admin)):
    try:
        await _empty_request(request)
        settings = get_settings()
        require_static_safety(settings)
        binding = binding_from_settings(settings)
        require_runtime_binding(binding)
        confirmations = request.headers.getlist(CONFIRM_HEADER)
        tokens = request.headers.getlist(GRANT_HEADER)
        if confirmations != [CONFIRMATION] or len(tokens) != 1:
            raise ReadinessError("EXPLICIT_EXECUTION_AUTHORIZATION_REQUIRED", 403)
        claims = verify_authorization(tokens[0], settings, binding, username)
        result = await run_in_threadpool(execute_readiness, settings, binding, claims)
        status = {"PASS": 200, "FAILED": 502, "UNKNOWN": 503}[result["outcome"]]
        return JSONResponse(result, status_code=status, headers=NO_STORE)
    except ReadinessError as error:
        return _refusal(error)


@router.get("")
async def status(request: Request, username: str = Depends(require_real_admin)):
    """Read consumed evidence after a lost response; never admits or relaunches."""
    try:
        await _empty_request(request)
        settings = get_settings()
        require_static_safety(settings, executing=False)
        binding = binding_from_settings(settings)
        require_runtime_binding(binding)
        result = await run_in_threadpool(lookup_readiness, binding)
        return JSONResponse(result, headers=NO_STORE)
    except ReadinessError as error:
        return _refusal(error)