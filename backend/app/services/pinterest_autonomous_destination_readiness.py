from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.domain import (
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
)
from app.services.pinterest_autonomous_execution import (
    AutonomousExecutionError,
    execution_readiness,
)
from app.services.pinterest_board_strategy import board_strategy

DESTINATION_POLICY_VERSION = "PINTEREST_AUTONOMOUS_DESTINATION_V1"


class AutonomousDestinationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def existing_destination_run(db, item_id: str):
    return db.scalar(
        select(PinterestAutonomousDestinationRun)
        .where(PinterestAutonomousDestinationRun.portfolio_item_id == item_id)
        .limit(1)
    )


def _optimizer_application(db, plan_id: str):
    return db.scalar(
        select(PinterestOptimizerApplication)
        .where(PinterestOptimizerApplication.plan_id == plan_id)
        .limit(1)
    )


def _provisioning_config_blockers(
    db,
    attempt: PinterestBoardProvisioningAttempt,
    settings: Settings,
) -> list[str]:
    blockers: list[str] = []
    if settings.pinterest_board_write_scope_enabled is not True:
        blockers.append("BOARD_WRITE_SCOPE_DISABLED")
    if settings.pinterest_board_provisioning_enabled is not True:
        blockers.append("BOARD_PROVISIONING_DISABLED")
    connection = db.get(PinterestConnection, attempt.connection_id)
    if connection is None or connection.status != "CONNECTED":
        blockers.append("PINTEREST_CONNECTION_REQUIRED")
    elif "boards:write" not in (connection.granted_scopes or []):
        blockers.append("BOARDS_WRITE_SCOPE_REQUIRED")
    return blockers


def _input_fingerprint(item, plan, optimizer, execution, board_plan):
    scheduled_for = execution.get("scheduled_for")
    if scheduled_for is None or not board_plan.get("canonical_key"):
        return None
    return _hash({
        "policy_version": DESTINATION_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "portfolio_item_fingerprint": item.item_fingerprint,
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "optimizer_application_id": optimizer.id,
        "optimizer_fingerprint": optimizer.optimizer_fingerprint,
        "optimizer_input_state_fingerprint": optimizer.input_state_fingerprint,
        "product_id": item.product_id,
        "local_board_id": item.local_board_id,
        "board_key_snapshot": item.board_key_snapshot,
        "content_angle_id": item.content_angle_id,
        "angle_key_snapshot": item.angle_key_snapshot,
        "scheduled_for": _utc(scheduled_for).isoformat(),
        "board_intent": {
            "canonical_key": board_plan.get("canonical_key"),
            "desired_name": board_plan.get("desired_name"),
            "desired_description": board_plan.get("desired_description"),
            "privacy": board_plan.get("privacy"),
        },
    })


def destination_readiness(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = _utc(now or datetime.now(timezone.utc))
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise AutonomousDestinationError("PORTFOLIO_ITEM_NOT_FOUND")
    plan = db.get(PinterestPortfolioPlan, item.plan_id)
    optimizer = _optimizer_application(db, item.plan_id)

    try:
        execution = execution_readiness(
            db, item.id, settings=settings, now=now
        )
    except AutonomousExecutionError as exc:
        raise AutonomousDestinationError(exc.code) from None

    board_plan = board_strategy(
        db,
        canonical_key=item.board_key_snapshot,
        settings=settings,
    )
    execution_blockers = list(execution.get("blockers") or [])
    non_board_execution_blockers = [
        code for code in execution_blockers
        if code != "ROUTABLE_PINTEREST_BOARD_REQUIRED"
    ]

    blockers: list[str] = []
    if settings.pinterest_autonomous_board_ensure_enabled is not True:
        blockers.append("AUTONOMOUS_BOARD_ENSURE_DISABLED")
    blockers.extend(non_board_execution_blockers)

    board_state = "BLOCKED"
    board_progress_ready = False
    existing_attempt = board_plan.get("existing_attempt")

    if board_plan.get("status") == "ROUTE_EXISTING":
        board_state = "BOARD_READY"
        board_progress_ready = True
    elif board_plan.get("status") == "PROVISION_REQUIRED":
        board_state = "PROVISION_REQUIRED"
        board_progress_ready = bool(board_plan.get("provisioning_ready"))
        blockers.extend(board_plan.get("blockers") or [])
    elif isinstance(existing_attempt, dict):
        attempt_status = existing_attempt.get("status")
        if attempt_status == "STARTED":
            if existing_attempt.get("provider_mutation_started_at") is not None:
                board_state = "PROVISIONING_OUTCOME_UNCERTAIN"
                blockers.append("PROVISIONING_MUTATION_ALREADY_STARTED")
            else:
                board_state = "PROVISIONING_STARTED"
                attempt = db.get(
                    PinterestBoardProvisioningAttempt,
                    existing_attempt.get("id"),
                )
                if attempt is None:
                    blockers.append("BOARD_PROVISIONING_ATTEMPT_NOT_FOUND")
                else:
                    cfg = _provisioning_config_blockers(db, attempt, settings)
                    blockers.extend(cfg)
                    board_progress_ready = not cfg
        elif attempt_status == "SUCCEEDED":
            board_state = "BOARD_SYNC_PENDING"
            board_progress_ready = True
        elif attempt_status == "FAILED":
            board_state = "PROVISIONING_FAILED"
            blockers.append("PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED")
        elif attempt_status == "UNKNOWN":
            board_state = "PROVISIONING_UNKNOWN"
            blockers.append(
                "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED"
            )
        else:
            blockers.extend(
                board_plan.get("blockers")
                or ["PROVISIONING_ATTEMPT_STATE_INVALID"]
            )
    else:
        blockers.extend(
            board_plan.get("blockers")
            or ["ROUTABLE_PINTEREST_BOARD_REQUIRED"]
        )

    input_fingerprint = None
    if plan is not None and optimizer is not None:
        input_fingerprint = _input_fingerprint(
            item, plan, optimizer, execution, board_plan
        )

    existing = existing_destination_run(db, item.id)
    already_succeeded = False
    if existing is not None:
        if (
            input_fingerprint is None
            or existing.input_fingerprint != input_fingerprint
        ):
            blockers.append("AUTONOMOUS_DESTINATION_INPUT_DRIFT")
        elif existing.status == "FAILED":
            blockers.append(
                "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED"
            )
        elif existing.status == "UNKNOWN":
            blockers.append(
                "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
            )
        elif existing.status == "SUCCEEDED":
            execution_run = (
                db.get(
                    PinterestAutonomousExecutionRun,
                    existing.autonomous_execution_run_id,
                )
                if existing.autonomous_execution_run_id
                else None
            )
            if (
                execution_run is None
                or execution_run.status != "SUCCEEDED"
                or execution_run.stage != "PERMITTED"
            ):
                blockers.append(
                    "AUTONOMOUS_DESTINATION_SUCCEEDED_STAGE_DRIFT"
                )
            else:
                already_succeeded = True
        elif existing.status != "STARTED":
            blockers.append("AUTONOMOUS_DESTINATION_STATUS_INVALID")

    blockers = list(dict.fromkeys(blockers))
    ready = already_succeeded or bool(
        input_fingerprint and board_progress_ready and not blockers
    )

    return {
        "policy_version": DESTINATION_POLICY_VERSION,
        "enabled": bool(settings.pinterest_autonomous_board_ensure_enabled),
        "portfolio_item_id": item.id,
        "plan_id": item.plan_id,
        "ready": ready,
        "already_succeeded": already_succeeded,
        "blockers": blockers,
        "input_fingerprint": input_fingerprint,
        "board_state": board_state,
        "board_strategy": board_plan,
        "execution_readiness": (
            execution
            if board_plan.get("status") == "ROUTE_EXISTING"
            else None
        ),
        "execution_prerequisite_blockers": non_board_execution_blockers,
        "existing_run": {
            "id": existing.id if existing else None,
            "status": existing.status if existing else None,
            "stage": existing.stage if existing else None,
            "board_provisioning_attempt_id": (
                existing.board_provisioning_attempt_id if existing else None
            ),
            "pinterest_board_record_id": (
                existing.pinterest_board_record_id if existing else None
            ),
            "autonomous_execution_run_id": (
                existing.autonomous_execution_run_id if existing else None
            ),
        },
        "state_mutated": False,
        "provider_called": False,
        "pin_write_called": False,
        "ai_called": False,
    }
