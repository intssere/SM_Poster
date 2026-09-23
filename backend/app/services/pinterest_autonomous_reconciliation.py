from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    PinPublication,
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestAutonomousRunReconciliation,
    PinterestPortfolioPlanItem,
    PublicationStatus,
)
from app.services.pinterest_autonomous_destination import destination_readiness
from app.services.pinterest_autonomous_execution import execution_readiness
from app.services.pinterest_autonomous_generation import autonomous_generation_readiness
from app.services.pinterest_autonomous_run_lineage import latest_run


RECONCILIATION_POLICY_VERSION = "PINTEREST_PHASE_B2_RECONCILIATION_V1"
RECONCILIATION_ACTOR = "phase-b2-reconciliation-v1"


_MUST_BE_FALSE_FIELDS = (
    "publishing_enabled",
    "buffer_publishing_enabled",
    "buffer_single_pin_pilot_enabled",
    "routine_pinterest_scheduler_enabled",
    "routine_pinterest_worker_enabled",
    "routine_buffer_dispatch_enabled",
    "pinterest_write_scope_enabled",
    "pinterest_board_write_scope_enabled",
    "pinterest_board_provisioning_enabled",
    "pinterest_single_pin_pilot_enabled",
    "pinterest_seo_brief_persistence_enabled",
    "pinterest_autonomous_generation_enabled",
    "routine_autonomous_authorization_enabled",
    "pinterest_autonomous_execution_enabled",
    "pinterest_autonomous_board_ensure_enabled",
    "pinterest_analytics_ingestion_enabled",
    "pinterest_learning_snapshot_persistence_enabled",
)


def _safety_blockers(settings: Settings) -> list[str]:
    blockers = [
        f"{name.upper()}_MUST_BE_FALSE"
        for name in _MUST_BE_FALSE_FIELDS
        if bool(getattr(settings, name))
    ]
    if settings.routine_pinterest_dry_run is not True:
        blockers.append("ROUTINE_PINTEREST_DRY_RUN_REQUIRED")
    return blockers


class AutonomousRunReconciliationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provider_free(metadata: dict | None) -> bool:
    value = metadata or {}
    return (
        value.get("provider_called") is False
        and value.get("ai_called") is False
    )


def _existing_reconciliations(db, *, destination_id: str, execution_id: str, generation_id: str):
    return list(db.scalars(
        select(PinterestAutonomousRunReconciliation)
        .where(
            (PinterestAutonomousRunReconciliation.failed_destination_run_id == destination_id)
            | (PinterestAutonomousRunReconciliation.failed_execution_run_id == execution_id)
            | (PinterestAutonomousRunReconciliation.failed_generation_run_id == generation_id)
        )
        .order_by(PinterestAutonomousRunReconciliation.created_at, PinterestAutonomousRunReconciliation.id)
    ).all())


def reconciliation_readiness(
    db,
    *,
    portfolio_item_id: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = now or _now()
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise AutonomousRunReconciliationError("PORTFOLIO_ITEM_NOT_FOUND")

    destination = latest_run(db, PinterestAutonomousDestinationRun, item.id)
    execution = latest_run(db, PinterestAutonomousExecutionRun, item.id)
    generation = latest_run(db, PinterestAutonomousGenerationRun, item.id)

    blockers: list[str] = []
    if destination is None or execution is None or generation is None:
        blockers.append("FAILED_RUN_CHAIN_REQUIRED")
    else:
        if destination.status != "FAILED":
            blockers.append("DESTINATION_RUN_NOT_FAILED")
        if execution.status != "FAILED":
            blockers.append("EXECUTION_RUN_NOT_FAILED")
        if generation.status != "FAILED":
            blockers.append("GENERATION_RUN_NOT_FAILED")
        if len({
            destination.attempt_number,
            execution.attempt_number,
            generation.attempt_number,
        }) != 1:
            blockers.append("FAILED_RUN_ATTEMPT_DRIFT")

        if not _provider_free(destination.safe_metadata):
            blockers.append("DESTINATION_PROVIDER_SIDE_EFFECT_NOT_PROVEN_ABSENT")
        if not _provider_free(execution.safe_metadata):
            blockers.append("EXECUTION_PROVIDER_SIDE_EFFECT_NOT_PROVEN_ABSENT")
        if not _provider_free(generation.safe_metadata):
            blockers.append("GENERATION_PROVIDER_SIDE_EFFECT_NOT_PROVEN_ABSENT")

        if destination.board_provisioning_attempt_id is not None:
            blockers.append("BOARD_PROVISIONING_SIDE_EFFECT_PRESENT")
        if destination.autonomous_execution_run_id is not None:
            blockers.append("DESTINATION_EXECUTION_LINK_PRESENT")
        if any((
            execution.generation_run_id,
            execution.approval_id,
            execution.publication_id,
            execution.routine_permit_id,
        )):
            blockers.append("EXECUTION_DOWNSTREAM_SIDE_EFFECT_PRESENT")
        if any((generation.concept_id, generation.draft_id, generation.creative_id)):
            blockers.append("GENERATION_CONTENT_SIDE_EFFECT_PRESENT")

    if item.is_reserve:
        blockers.append("RESERVE_ITEM_NOT_RECONCILABLE")
    if item.status != "PLANNED":
        blockers.append("PORTFOLIO_ITEM_NOT_PLANNED")
    if item.publication_id is not None:
        blockers.append("PORTFOLIO_ITEM_PUBLICATION_ALREADY_SET")

    publish_unknown_count = int(
        db.scalar(
            select(func.count())
            .select_from(PinPublication)
            .where(PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN)
        )
        or 0
    )
    if publish_unknown_count:
        blockers.append("PUBLISH_UNKNOWN_PRESENT")
    blockers.extend(_safety_blockers(settings))

    destination_state = destination_readiness(
        db,
        item.id,
        settings=settings,
        now=now,
    )
    execution_state = execution_readiness(
        db,
        item.id,
        settings=settings,
        now=now,
    )
    generation_state = autonomous_generation_readiness(
        db,
        item.id,
        settings=settings,
    )

    retry_destination_fp = destination_state.get("input_fingerprint")
    retry_execution_fp = execution_state.get("input_fingerprint")
    retry_generation_fp = generation_state.get("input_fingerprint")
    if not retry_destination_fp:
        blockers.append("RETRY_DESTINATION_FINGERPRINT_REQUIRED")
    if not retry_execution_fp:
        blockers.append("RETRY_EXECUTION_FINGERPRINT_REQUIRED")
    if not retry_generation_fp:
        blockers.append("RETRY_GENERATION_FINGERPRINT_REQUIRED")

    if destination is not None and retry_destination_fp and (
        destination.input_fingerprint != retry_destination_fp
    ):
        blockers.append("DESTINATION_RETRY_INPUT_DRIFT")
    if execution is not None and retry_execution_fp and (
        execution.input_fingerprint != retry_execution_fp
    ):
        blockers.append("EXECUTION_RETRY_INPUT_DRIFT")

    evidence = None
    reconciliation_fingerprint = None
    existing = None
    already_reconciled = False
    if destination is not None and execution is not None and generation is not None:
        evidence = {
            "policy_version": RECONCILIATION_POLICY_VERSION,
            "portfolio_item_id": item.id,
            "portfolio_item_fingerprint": item.item_fingerprint,
            "attempt_number": destination.attempt_number,
            "failed_destination": {
                "id": destination.id,
                "input_fingerprint": destination.input_fingerprint,
                "status": destination.status,
                "stage": destination.stage,
                "provider_called": (destination.safe_metadata or {}).get("provider_called"),
                "ai_called": (destination.safe_metadata or {}).get("ai_called"),
            },
            "failed_execution": {
                "id": execution.id,
                "input_fingerprint": execution.input_fingerprint,
                "status": execution.status,
                "stage": execution.stage,
                "provider_called": (execution.safe_metadata or {}).get("provider_called"),
                "ai_called": (execution.safe_metadata or {}).get("ai_called"),
            },
            "failed_generation": {
                "id": generation.id,
                "input_fingerprint": generation.input_fingerprint,
                "status": generation.status,
                "provider_called": (generation.safe_metadata or {}).get("provider_called"),
                "ai_called": (generation.safe_metadata or {}).get("ai_called"),
                "failure_code": (generation.safe_metadata or {}).get("failure_code"),
            },
            "retry_fingerprints": {
                "destination": retry_destination_fp,
                "execution": retry_execution_fp,
                "generation": retry_generation_fp,
            },
            "side_effect_evidence": {
                "publish_unknown_count": publish_unknown_count,
                "item_publication_id": item.publication_id,
                "board_provisioning_attempt_id": destination.board_provisioning_attempt_id,
                "destination_execution_run_id": destination.autonomous_execution_run_id,
                "execution_generation_run_id": execution.generation_run_id,
                "execution_approval_id": execution.approval_id,
                "execution_publication_id": execution.publication_id,
                "execution_routine_permit_id": execution.routine_permit_id,
                "generation_concept_id": generation.concept_id,
                "generation_draft_id": generation.draft_id,
                "generation_creative_id": generation.creative_id,
            },
        }
        reconciliation_fingerprint = _hash(evidence)
        existing_rows = _existing_reconciliations(
            db,
            destination_id=destination.id,
            execution_id=execution.id,
            generation_id=generation.id,
        )
        if existing_rows:
            ids = {row.id for row in existing_rows}
            if len(ids) != 1:
                blockers.append("RECONCILIATION_RECORD_DRIFT")
            else:
                existing = existing_rows[0]
                expected = (
                    existing.failed_destination_run_id == destination.id
                    and existing.failed_execution_run_id == execution.id
                    and existing.failed_generation_run_id == generation.id
                    and existing.failed_destination_input_fingerprint == destination.input_fingerprint
                    and existing.failed_execution_input_fingerprint == execution.input_fingerprint
                    and existing.failed_generation_input_fingerprint == generation.input_fingerprint
                    and existing.retry_destination_input_fingerprint == retry_destination_fp
                    and existing.retry_execution_input_fingerprint == retry_execution_fp
                    and existing.retry_generation_input_fingerprint == retry_generation_fp
                    and existing.reconciliation_fingerprint == reconciliation_fingerprint
                    and existing.status == "RECONCILED"
                )
                if expected:
                    already_reconciled = True
                else:
                    blockers.append("RECONCILIATION_RECORD_DRIFT")

    blockers = list(dict.fromkeys(blockers))
    return {
        "ready": not blockers,
        "already_reconciled": already_reconciled,
        "blockers": blockers,
        "policy_version": RECONCILIATION_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "item_fingerprint": item.item_fingerprint,
        "item_status": item.status,
        "publication_id": item.publication_id,
        "failed_attempt_number": destination.attempt_number if destination else None,
        "failed_destination_run_id": destination.id if destination else None,
        "failed_destination_input_fingerprint": destination.input_fingerprint if destination else None,
        "failed_destination_stage": destination.stage if destination else None,
        "failed_execution_run_id": execution.id if execution else None,
        "failed_execution_input_fingerprint": execution.input_fingerprint if execution else None,
        "failed_execution_stage": execution.stage if execution else None,
        "failed_generation_run_id": generation.id if generation else None,
        "failed_generation_input_fingerprint": generation.input_fingerprint if generation else None,
        "failed_generation_failure_code": (
            (generation.safe_metadata or {}).get("failure_code") if generation else None
        ),
        "retry_destination_input_fingerprint": retry_destination_fp,
        "retry_execution_input_fingerprint": retry_execution_fp,
        "retry_generation_input_fingerprint": retry_generation_fp,
        "reconciliation_fingerprint": reconciliation_fingerprint,
        "existing_reconciliation_id": existing.id if existing else None,
        "evidence": evidence,
        "publish_unknown_count": publish_unknown_count,
        "gate_state": {
            name: bool(getattr(settings, name))
            for name in (*_MUST_BE_FALSE_FIELDS, "routine_pinterest_dry_run")
        },
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def _require(expected: Any, actual: Any, code: str) -> None:
    if expected != actual:
        raise AutonomousRunReconciliationError(code)


def reconcile_failed_run_chain(
    db,
    *,
    portfolio_item_id: str,
    expected_item_fingerprint: str,
    expected_failed_destination_run_id: str,
    expected_failed_destination_input_fingerprint: str,
    expected_failed_execution_run_id: str,
    expected_failed_execution_input_fingerprint: str,
    expected_failed_generation_run_id: str,
    expected_failed_generation_input_fingerprint: str,
    expected_retry_destination_input_fingerprint: str,
    expected_retry_execution_input_fingerprint: str,
    expected_retry_generation_input_fingerprint: str,
    expected_reconciliation_fingerprint: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    readiness = reconciliation_readiness(
        db,
        portfolio_item_id=portfolio_item_id,
        settings=settings,
        now=now,
    )
    if readiness.get("ready") is not True:
        raise AutonomousRunReconciliationError(
            (readiness.get("blockers") or ["RECONCILIATION_NOT_READY"])[0]
        )

    _require(expected_item_fingerprint, readiness["item_fingerprint"], "ITEM_FINGERPRINT_MISMATCH")
    _require(expected_failed_destination_run_id, readiness["failed_destination_run_id"], "FAILED_DESTINATION_RUN_ID_MISMATCH")
    _require(expected_failed_destination_input_fingerprint, readiness["failed_destination_input_fingerprint"], "FAILED_DESTINATION_FINGERPRINT_MISMATCH")
    _require(expected_failed_execution_run_id, readiness["failed_execution_run_id"], "FAILED_EXECUTION_RUN_ID_MISMATCH")
    _require(expected_failed_execution_input_fingerprint, readiness["failed_execution_input_fingerprint"], "FAILED_EXECUTION_FINGERPRINT_MISMATCH")
    _require(expected_failed_generation_run_id, readiness["failed_generation_run_id"], "FAILED_GENERATION_RUN_ID_MISMATCH")
    _require(expected_failed_generation_input_fingerprint, readiness["failed_generation_input_fingerprint"], "FAILED_GENERATION_FINGERPRINT_MISMATCH")
    _require(expected_retry_destination_input_fingerprint, readiness["retry_destination_input_fingerprint"], "RETRY_DESTINATION_FINGERPRINT_MISMATCH")
    _require(expected_retry_execution_input_fingerprint, readiness["retry_execution_input_fingerprint"], "RETRY_EXECUTION_FINGERPRINT_MISMATCH")
    _require(expected_retry_generation_input_fingerprint, readiness["retry_generation_input_fingerprint"], "RETRY_GENERATION_FINGERPRINT_MISMATCH")
    _require(expected_reconciliation_fingerprint, readiness["reconciliation_fingerprint"], "RECONCILIATION_FINGERPRINT_MISMATCH")

    if readiness.get("already_reconciled"):
        return {
            **readiness,
            "status": "RECONCILED",
            "idempotent": True,
            "reconciliation_id": readiness["existing_reconciliation_id"],
            "state_mutated": False,
        }

    record = PinterestAutonomousRunReconciliation(
        portfolio_item_id=portfolio_item_id,
        failed_destination_run_id=readiness["failed_destination_run_id"],
        failed_execution_run_id=readiness["failed_execution_run_id"],
        failed_generation_run_id=readiness["failed_generation_run_id"],
        failed_destination_input_fingerprint=readiness["failed_destination_input_fingerprint"],
        failed_execution_input_fingerprint=readiness["failed_execution_input_fingerprint"],
        failed_generation_input_fingerprint=readiness["failed_generation_input_fingerprint"],
        retry_destination_input_fingerprint=readiness["retry_destination_input_fingerprint"],
        retry_execution_input_fingerprint=readiness["retry_execution_input_fingerprint"],
        retry_generation_input_fingerprint=readiness["retry_generation_input_fingerprint"],
        reconciliation_fingerprint=readiness["reconciliation_fingerprint"],
        status="RECONCILED",
        actor=RECONCILIATION_ACTOR,
        evidence=readiness["evidence"],
    )
    db.add(record)
    db.flush()
    db.add(AuditLog(
        actor=RECONCILIATION_ACTOR,
        action="AUTONOMOUS_FAILED_RUN_CHAIN_RECONCILED",
        entity_type="PinterestAutonomousRunReconciliation",
        entity_id=record.id,
        metadata_json={
            "portfolio_item_id": portfolio_item_id,
            "failed_attempt_number": readiness["failed_attempt_number"],
            "failed_destination_run_id": readiness["failed_destination_run_id"],
            "failed_execution_run_id": readiness["failed_execution_run_id"],
            "failed_generation_run_id": readiness["failed_generation_run_id"],
            "reconciliation_fingerprint": readiness["reconciliation_fingerprint"],
            "provider_called": False,
            "ai_called": False,
        },
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        current = reconciliation_readiness(
            db,
            portfolio_item_id=portfolio_item_id,
            settings=settings,
            now=now,
        )
        if current.get("already_reconciled") and (
            current.get("reconciliation_fingerprint") == expected_reconciliation_fingerprint
        ):
            return {
                **current,
                "status": "RECONCILED",
                "idempotent": True,
                "reconciliation_id": current["existing_reconciliation_id"],
                "state_mutated": False,
            }
        raise AutonomousRunReconciliationError("RECONCILIATION_ALREADY_EXISTS") from None

    return {
        **readiness,
        "status": "RECONCILED",
        "idempotent": False,
        "reconciliation_id": record.id,
        "state_mutated": True,
        "provider_called": False,
        "ai_called": False,
    }
