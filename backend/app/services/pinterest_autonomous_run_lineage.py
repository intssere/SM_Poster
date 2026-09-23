from __future__ import annotations

from typing import Any, Type

from sqlalchemy import select

from app.models.domain import (
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestAutonomousRunReconciliation,
)


_KIND_FIELDS = {
    "destination": (
        PinterestAutonomousDestinationRun,
        "failed_destination_run_id",
        "retry_destination_input_fingerprint",
    ),
    "execution": (
        PinterestAutonomousExecutionRun,
        "failed_execution_run_id",
        "retry_execution_input_fingerprint",
    ),
    "generation": (
        PinterestAutonomousGenerationRun,
        "failed_generation_run_id",
        "retry_generation_input_fingerprint",
    ),
}


def latest_run(db, model: Type[Any], portfolio_item_id: str):
    return db.scalar(
        select(model)
        .where(model.portfolio_item_id == portfolio_item_id)
        .order_by(model.attempt_number.desc(), model.created_at.desc(), model.id.desc())
        .limit(1)
    )


def reconciliation_for_failed_run(
    db,
    *,
    kind: str,
    run_id: str,
) -> PinterestAutonomousRunReconciliation | None:
    _, failed_field, _ = _KIND_FIELDS[kind]
    column = getattr(PinterestAutonomousRunReconciliation, failed_field)
    return db.scalar(
        select(PinterestAutonomousRunReconciliation)
        .where(
            column == run_id,
            PinterestAutonomousRunReconciliation.status == "RECONCILED",
        )
        .limit(1)
    )


def retry_reconciliation(
    db,
    *,
    kind: str,
    failed_run,
    retry_input_fingerprint: str | None,
) -> PinterestAutonomousRunReconciliation | None:
    if failed_run is None or failed_run.status != "FAILED" or not retry_input_fingerprint:
        return None
    reconciliation = reconciliation_for_failed_run(
        db,
        kind=kind,
        run_id=failed_run.id,
    )
    if reconciliation is None:
        return None
    _, _, retry_field = _KIND_FIELDS[kind]
    if getattr(reconciliation, retry_field) != retry_input_fingerprint:
        return None
    return reconciliation


def next_attempt_context(
    db,
    *,
    kind: str,
    latest,
    retry_input_fingerprint: str,
) -> tuple[int, str | None, str | None]:
    if latest is None:
        return 1, None, None
    if latest.status != "FAILED":
        return latest.attempt_number, None, None
    reconciliation = retry_reconciliation(
        db,
        kind=kind,
        failed_run=latest,
        retry_input_fingerprint=retry_input_fingerprint,
    )
    if reconciliation is None:
        return latest.attempt_number, None, None
    return latest.attempt_number + 1, latest.id, reconciliation.id
