from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.services.production_alembic_reconciliation_control import (
    ReconciliationControlRefused,
    execute_temporary_reconciliation,
)

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class ReplitSchemaDiffAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    repl_id: str
    database_scope: str
    database_identity_sha256: str
    checked_at: str
    pending_statements: int
    structural_data_loss: bool
    potential_incompatibility: bool
    warnings: list[str]


class ReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorization_secret: str = Field(min_length=1, max_length=4096)
    confirmation: str
    attestation: ReplitSchemaDiffAttestation
    attestation_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


@router.post(
    "/production-alembic-0017-0018/reconcile",
    include_in_schema=False,
)
def reconcile_production_alembic(payload: ReconciliationRequest) -> dict[str, Any]:
    try:
        return execute_temporary_reconciliation(
            supplied_secret=payload.authorization_secret,
            confirmation=payload.confirmation,
            attestation=payload.attestation.model_dump(mode="json"),
            attestation_sha256=payload.attestation_sha256,
        )
    except ReconciliationControlRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
