from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    PinterestAutonomousDestinationRun,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
)
from app.services.pinterest_autonomous_execution import (
    AutonomousExecutionError,
    execute_autonomous_item,
    execution_readiness,
)
from app.services.pinterest_board_provisioning import (
    BoardProvisioningError,
    execute_board_provisioning_attempt,
    reconcile_provisioned_board,
    start_board_provisioning,
)
from app.services.pinterest_board_strategy import board_strategy
from app.services.pinterest_boards import sync_boards
from app.services.pinterest_optimizer_apply import OPTIMIZER_METADATA_KEY
from app.services.pinterest_autonomous_run_lineage import (
    latest_run,
    next_attempt_context,
    retry_reconciliation,
)


DESTINATION_POLICY_VERSION = "PINTEREST_AUTONOMOUS_DESTINATION_V1"
DESTINATION_ACTOR = "autonomous-destination-v1"
_ATTEMPT_BLOCKERS = {
    "PROVISIONING_IN_PROGRESS",
    "PROVISIONING_MUTATION_ALREADY_STARTED",
    "PROVISIONING_SUCCEEDED_SYNC_REQUIRED",
    "PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
    "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
}
_SYNC_PENDING_CODES = {
    "BOARD_SYNC_REQUIRED",
    "PROVISIONED_BOARD_NOT_SYNCED",
    "BOARD_SYNC_STALE",
}
_SQLITE_LOCKS: dict[str, threading.Lock] = {}
_SQLITE_LOCKS_GUARD = threading.Lock()


class AutonomousDestinationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash(payload: Any) -> str:
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def _existing_run(db, item_id: str) -> PinterestAutonomousDestinationRun | None:
    return latest_run(db, PinterestAutonomousDestinationRun, item_id)


def _optimizer(db, plan_id: str) -> PinterestOptimizerApplication | None:
    return db.scalar(
        select(PinterestOptimizerApplication)
        .where(PinterestOptimizerApplication.plan_id == plan_id)
        .limit(1)
    )


def _board_identity(board_plan: dict[str, Any]) -> dict[str, Any] | None:
    if board_plan.get("status") == "ROUTE_EXISTING":
        return {
            "kind": "EXISTING",
            "strategy_version": board_plan.get("strategy_version"),
            "canonical_key": board_plan.get("canonical_key"),
            "pinterest_board_record_id": board_plan.get("selected_board_id"),
            "provider_board_id": board_plan.get("selected_external_board_id"),
        }
    if board_plan.get("request_fingerprint"):
        return {
            "kind": "PROVISION",
            "strategy_version": board_plan.get("strategy_version"),
            "canonical_key": board_plan.get("canonical_key"),
            "request_fingerprint": board_plan.get("request_fingerprint"),
            "desired_name": board_plan.get("desired_name"),
            "desired_description": board_plan.get("desired_description"),
            "privacy": board_plan.get("privacy"),
        }
    return None


def _destination_fingerprint(
    *,
    item: PinterestPortfolioPlanItem,
    plan: PinterestPortfolioPlan,
    optimizer: PinterestOptimizerApplication,
    optimizer_metadata: dict[str, Any],
    scheduled_for: datetime,
    board_identity: dict[str, Any],
) -> str:
    return _hash({
        "policy_version": DESTINATION_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "portfolio_item_fingerprint": item.item_fingerprint,
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "optimizer_application_id": optimizer.id,
        "optimizer_policy_version": optimizer.optimizer_policy_version,
        "optimizer_fingerprint": optimizer.optimizer_fingerprint,
        "optimizer_input_state_fingerprint": optimizer.input_state_fingerprint,
        "item_optimizer_metadata": {
            "optimizer_policy_version": optimizer_metadata.get(
                "optimizer_policy_version"
            ),
            "optimizer_fingerprint": optimizer_metadata.get("optimizer_fingerprint"),
            "input_state_fingerprint": optimizer_metadata.get(
                "input_state_fingerprint"
            ),
            "recommended_position": optimizer_metadata.get("recommended_position"),
            "target_slot_index": optimizer_metadata.get("target_slot_index"),
            "target_planned_date": optimizer_metadata.get("target_planned_date"),
            "selection_reason": optimizer_metadata.get("selection_reason"),
        },
        "scheduled_for": _utc(scheduled_for).isoformat(),
        "product_id": item.product_id,
        "local_board_id": item.local_board_id,
        "board_key_snapshot": item.board_key_snapshot,
        "content_angle_id": item.content_angle_id,
        "angle_key_snapshot": item.angle_key_snapshot,
        "board_identity": board_identity,
    })


def _validate_strategy_binding(
    db,
    run: PinterestAutonomousDestinationRun,
    board_plan: dict[str, Any],
) -> None:
    identity = (run.safe_metadata or {}).get("board_identity")
    if not isinstance(identity, dict):
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE")
    if (
        identity.get("canonical_key") != board_plan.get("canonical_key")
        or identity.get("strategy_version") != board_plan.get("strategy_version")
    ):
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_DRIFT")

    if identity.get("kind") == "EXISTING":
        if (
            board_plan.get("status") != "ROUTE_EXISTING"
            or identity.get("pinterest_board_record_id")
            != board_plan.get("selected_board_id")
            or identity.get("provider_board_id")
            != board_plan.get("selected_external_board_id")
        ):
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_DRIFT")
        return

    if identity.get("kind") != "PROVISION":
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE")
    if board_plan.get("status") != "ROUTE_EXISTING":
        if identity.get("request_fingerprint") != board_plan.get("request_fingerprint"):
            raise AutonomousDestinationError(
                "AUTONOMOUS_DESTINATION_PROVISIONING_DRIFT"
            )
        return

    if not run.board_provisioning_attempt_id:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_DRIFT")
    attempt = db.get(
        PinterestBoardProvisioningAttempt,
        run.board_provisioning_attempt_id,
    )
    if (
        attempt is None
        or attempt.request_fingerprint != identity.get("request_fingerprint")
        or attempt.status != "SUCCEEDED"
        or not attempt.provider_board_id
        or attempt.provider_board_id != board_plan.get("selected_external_board_id")
    ):
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_DRIFT")


def destination_readiness(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = _utc(now or _now())
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise AutonomousDestinationError("PORTFOLIO_ITEM_NOT_FOUND")

    plan = db.get(PinterestPortfolioPlan, item.plan_id)
    optimizer = _optimizer(db, item.plan_id)
    optimizer_metadata = (item.selection_metadata or {}).get(OPTIMIZER_METADATA_KEY)
    if not isinstance(optimizer_metadata, dict):
        optimizer_metadata = None

    execution = execution_readiness(
        db,
        portfolio_item_id,
        settings=settings,
        now=now,
    )
    try:
        board_plan = board_strategy(
            db,
            canonical_key=item.board_key_snapshot,
            settings=settings,
        )
    except Exception:
        board_plan = {
            "status": "BLOCKED",
            "blockers": ["BOARD_STRATEGY_ERROR"],
            "existing_attempt": None,
            "request_fingerprint": None,
            "selected_board_id": None,
            "selected_external_board_id": None,
        }

    existing = _existing_run(db, item.id)
    stored_identity = (
        (existing.safe_metadata or {}).get("board_identity")
        if existing is not None
        else None
    )
    identity = (
        stored_identity
        if isinstance(stored_identity, dict)
        else _board_identity(board_plan)
    )

    blockers = [
        code
        for code in execution["blockers"]
        if code != "ROUTABLE_PINTEREST_BOARD_REQUIRED"
    ]
    if board_plan.get("status") == "ROUTE_EXISTING":
        pass
    elif board_plan.get("request_fingerprint"):
        if settings.pinterest_autonomous_board_ensure_enabled is not True:
            blockers.append("AUTONOMOUS_BOARD_ENSURE_DISABLED")
        blockers.extend(board_plan.get("blockers") or [])
    else:
        blockers.extend(
            board_plan.get("blockers") or ["ROUTABLE_PINTEREST_BOARD_REQUIRED"]
        )

    input_fingerprint = None
    if (
        plan is not None
        and optimizer is not None
        and optimizer_metadata is not None
        and execution.get("scheduled_for") is not None
        and identity is not None
    ):
        input_fingerprint = _destination_fingerprint(
            item=item,
            plan=plan,
            optimizer=optimizer,
            optimizer_metadata=optimizer_metadata,
            scheduled_for=execution["scheduled_for"],
            board_identity=identity,
        )

    already_completed = False
    retry_reconciliation_record = None
    if existing is not None:
        if existing.status == "FAILED":
            retry_reconciliation_record = retry_reconciliation(
                db,
                kind="destination",
                failed_run=existing,
                retry_input_fingerprint=input_fingerprint,
            )
            if retry_reconciliation_record is None:
                blockers.append(
                    "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED"
                )
        elif not input_fingerprint or existing.input_fingerprint != input_fingerprint:
            blockers.append("AUTONOMOUS_DESTINATION_INPUT_DRIFT")
        elif existing.status == "UNKNOWN":
            blockers.append(
                "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
            )
        elif existing.status == "SUCCEEDED":
            already_completed = True
            if existing.stage != "EXECUTION_READY":
                blockers.append("AUTONOMOUS_DESTINATION_SUCCEEDED_STAGE_DRIFT")
        elif existing.status != "STARTED":
            blockers.append("AUTONOMOUS_DESTINATION_STATUS_INVALID")

    blockers = list(dict.fromkeys(blockers))
    attempt = board_plan.get("existing_attempt")
    return {
        "policy_version": DESTINATION_POLICY_VERSION,
        "enabled": bool(settings.pinterest_autonomous_board_ensure_enabled),
        "portfolio_item_id": item.id,
        "plan_id": item.plan_id,
        "ready": not blockers,
        "already_completed": already_completed,
        "blockers": blockers,
        "board_strategy": {
            "status": board_plan.get("status"),
            "strategy_version": board_plan.get("strategy_version"),
            "canonical_key": board_plan.get("canonical_key"),
            "selected_board_id": board_plan.get("selected_board_id"),
            "selected_external_board_id": board_plan.get(
                "selected_external_board_id"
            ),
            "request_fingerprint": board_plan.get("request_fingerprint"),
            "desired_name": board_plan.get("desired_name"),
            "desired_description": board_plan.get("desired_description"),
            "privacy": board_plan.get("privacy"),
            "provisioning_ready": board_plan.get("provisioning_ready", False),
            "blockers": board_plan.get("blockers") or [],
            "existing_attempt": attempt,
        },
        "execution_readiness": (
            execution if board_plan.get("status") == "ROUTE_EXISTING" else None
        ),
        "scheduled_for": execution.get("scheduled_for"),
        "input_fingerprint": input_fingerprint,
        "board_identity": identity,
        "existing_run": {
            "id": existing.id if existing else None,
            "status": existing.status if existing else None,
            "stage": existing.stage if existing else None,
            "attempt_number": existing.attempt_number if existing else None,
            "board_provisioning_attempt_id": (
                existing.board_provisioning_attempt_id if existing else None
            ),
            "pinterest_board_record_id": (
                existing.pinterest_board_record_id if existing else None
            ),
            "autonomous_execution_run_id": (
                existing.autonomous_execution_run_id if existing else None
            ),
            "retry_reconciliation_id": (
                retry_reconciliation_record.id if retry_reconciliation_record else None
            ),
        },
        "next_attempt_number": (
            existing.attempt_number + 1
            if existing is not None and retry_reconciliation_record is not None
            else 1 if existing is None else existing.attempt_number
        ),
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def _set_terminal(
    db,
    run: PinterestAutonomousDestinationRun,
    *,
    status: str,
    code: str,
    now: datetime,
) -> PinterestAutonomousDestinationRun:
    db.rollback()
    current = db.get(PinterestAutonomousDestinationRun, run.id)
    if current is None:
        raise AutonomousDestinationError(code)
    metadata = {**(current.safe_metadata or {}), "error_code": code}
    changed = db.execute(
        update(PinterestAutonomousDestinationRun)
        .execution_options(synchronize_session=False)
        .where(
            PinterestAutonomousDestinationRun.id == run.id,
            PinterestAutonomousDestinationRun.status == "STARTED",
        )
        .values(
            status=status,
            completed_at=now,
            safe_metadata=metadata,
        )
    )
    if changed.rowcount == 1:
        db.add(AuditLog(
            actor=DESTINATION_ACTOR,
            action=f"AUTONOMOUS_DESTINATION_{status}",
            entity_type="PinterestAutonomousDestinationRun",
            entity_id=current.id,
            metadata_json={"stage": current.stage, "error_code": code},
        ))
        db.commit()
    else:
        db.rollback()
    db.expire_all()
    current = db.get(PinterestAutonomousDestinationRun, run.id)
    if current is None:
        raise AutonomousDestinationError(code)
    return current


def _terminal_run_error(run: PinterestAutonomousDestinationRun) -> str:
    if run.status == "UNKNOWN":
        return "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
    if run.status == "FAILED":
        return "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED"
    if run.status == "SUCCEEDED":
        return "AUTONOMOUS_DESTINATION_ALREADY_SUCCEEDED"
    return "AUTONOMOUS_DESTINATION_STATUS_INVALID"


def _advance_started(
    db,
    run_id: str,
    **values,
) -> PinterestAutonomousDestinationRun:
    changed = db.execute(
        update(PinterestAutonomousDestinationRun)
        .execution_options(synchronize_session=False)
        .where(
            PinterestAutonomousDestinationRun.id == run_id,
            PinterestAutonomousDestinationRun.status == "STARTED",
        )
        .values(**values)
    )
    if changed.rowcount != 1:
        db.rollback()
        db.expire_all()
        current = db.get(PinterestAutonomousDestinationRun, run_id)
        if current is None:
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_RUN_NOT_FOUND")
        raise AutonomousDestinationError(_terminal_run_error(current))
    db.commit()
    db.expire_all()
    current = db.get(PinterestAutonomousDestinationRun, run_id)
    if current is None:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_RUN_NOT_FOUND")
    return current


def _execution_blockers_for_coordinator(readiness: dict[str, Any]) -> list[str]:
    return [
        code
        for code in readiness.get("blockers", [])
        if code not in _ATTEMPT_BLOCKERS
    ]


def _acquire_coordinator_lock(db, portfolio_item_id: str):
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        connection = bind.connect()
        acquired = bool(connection.scalar(
            select(
                func.pg_try_advisory_lock(
                    func.hashtext(
                        f"autonomous-destination:{portfolio_item_id}"
                    )
                )
            )
        ))
        if not acquired:
            connection.close()
            return None
        return ("postgresql", connection, portfolio_item_id)

    with _SQLITE_LOCKS_GUARD:
        lock = _SQLITE_LOCKS.setdefault(portfolio_item_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return None
    return ("local", lock, portfolio_item_id)


def _release_coordinator_lock(lock_handle) -> None:
    kind, resource, portfolio_item_id = lock_handle
    if kind == "postgresql":
        try:
            resource.execute(
                select(
                    func.pg_advisory_unlock(
                        func.hashtext(
                            f"autonomous-destination:{portfolio_item_id}"
                        )
                    )
                )
            )
        finally:
            resource.close()
        return
    resource.release()


async def ensure_autonomous_destination(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    provisioning_client=None,
    sync_client=None,
    renderer=None,
    now: datetime | None = None,
) -> PinterestAutonomousDestinationRun:
    lock_handle = _acquire_coordinator_lock(db, portfolio_item_id)
    if lock_handle is None:
        current = _existing_run(db, portfolio_item_id)
        if current is not None:
            return current
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_IN_PROGRESS")
    try:
        return await _ensure_autonomous_destination_locked(
            db,
            portfolio_item_id,
            settings=settings,
            provisioning_client=provisioning_client,
            sync_client=sync_client,
            renderer=renderer,
            now=now,
        )
    finally:
        _release_coordinator_lock(lock_handle)


async def _ensure_autonomous_destination_locked(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    provisioning_client=None,
    sync_client=None,
    renderer=None,
    now: datetime | None = None,
) -> PinterestAutonomousDestinationRun:
    settings = settings or get_settings()
    now = _utc(now or _now())
    readiness = destination_readiness(
        db,
        portfolio_item_id,
        settings=settings,
        now=now,
    )
    existing = _existing_run(db, portfolio_item_id)
    if existing is not None and existing.status == "SUCCEEDED":
        if (
            existing.input_fingerprint == readiness.get("input_fingerprint")
            and existing.stage == "EXECUTION_READY"
        ):
            return existing
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_INPUT_DRIFT")
    if existing is not None and existing.status == "UNKNOWN":
        raise AutonomousDestinationError(
            "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
        )
    if not readiness.get("input_fingerprint"):
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE")
    blockers = _execution_blockers_for_coordinator(readiness)
    if blockers:
        raise AutonomousDestinationError(blockers[0])
    attempt_number, supersedes_run_id, reconciliation_id = next_attempt_context(
        db,
        kind="destination",
        latest=existing,
        retry_input_fingerprint=readiness["input_fingerprint"],
    )
    if existing is not None and existing.status == "FAILED" and reconciliation_id is None:
        raise AutonomousDestinationError(
            "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED"
        )

    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    plan = db.get(PinterestPortfolioPlan, item.plan_id) if item else None
    if item is None or plan is None:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE")

    run = None if existing is not None and existing.status == "FAILED" else existing
    if run is None:
        identity = readiness.get("board_identity")
        if not isinstance(identity, dict):
            raise AutonomousDestinationError(
                "AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE"
            )
        run = PinterestAutonomousDestinationRun(
            portfolio_item_id=item.id,
            plan_id=plan.id,
            input_fingerprint=readiness["input_fingerprint"],
            attempt_number=attempt_number,
            supersedes_run_id=supersedes_run_id,
            status="STARTED",
            stage="STARTED",
            safe_metadata={
                "policy_version": DESTINATION_POLICY_VERSION,
                "portfolio_item_fingerprint": item.item_fingerprint,
                "plan_fingerprint": plan.plan_fingerprint,
                "board_identity": identity,
                "reconciliation_id": reconciliation_id,
                "supersedes_run_id": supersedes_run_id,
                "attempt_number": attempt_number,
                "provider_called": False,
                "ai_called": False,
            },
            started_at=now,
        )
        db.add(run)
        db.flush()
        db.add(AuditLog(
            actor=DESTINATION_ACTOR,
            action="AUTONOMOUS_DESTINATION_STARTED",
            entity_type="PinterestAutonomousDestinationRun",
            entity_id=run.id,
            metadata_json={
                "portfolio_item_id": item.id,
                "input_fingerprint": run.input_fingerprint,
                "attempt_number": run.attempt_number,
                "supersedes_run_id": run.supersedes_run_id,
                "reconciliation_id": reconciliation_id,
            },
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            current = _existing_run(db, item.id)
            if (
                current is None
                or current.status != "STARTED"
                or current.input_fingerprint != readiness["input_fingerprint"]
                or current.attempt_number != attempt_number
            ):
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_ALREADY_EXISTS"
                ) from None
            run = current
        db.refresh(run)
    elif run.input_fingerprint != readiness["input_fingerprint"]:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_INPUT_DRIFT")

    board_plan = board_strategy(
        db,
        canonical_key=item.board_key_snapshot,
        settings=settings,
    )
    try:
        _validate_strategy_binding(db, run, board_plan)
        if board_plan.get("status") == "ROUTE_EXISTING":
            selected = board_plan.get("selected_board_id")
            if run.pinterest_board_record_id and run.pinterest_board_record_id != selected:
                raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_DRIFT")
            run = _advance_started(
                db,
                run.id,
                pinterest_board_record_id=selected,
                stage="BOARD_READY",
            )
        else:
            if settings.pinterest_autonomous_board_ensure_enabled is not True:
                raise AutonomousDestinationError("AUTONOMOUS_BOARD_ENSURE_DISABLED")
            if settings.pinterest_board_write_scope_enabled is not True:
                raise AutonomousDestinationError("BOARD_WRITE_SCOPE_DISABLED")
            if settings.pinterest_board_provisioning_enabled is not True:
                raise AutonomousDestinationError("BOARD_PROVISIONING_DISABLED")

            attempt = (
                db.get(
                    PinterestBoardProvisioningAttempt,
                    run.board_provisioning_attempt_id,
                )
                if run.board_provisioning_attempt_id
                else None
            )
            if attempt is None:
                existing_attempt = board_plan.get("existing_attempt") or {}
                if existing_attempt.get("id"):
                    attempt = db.get(
                        PinterestBoardProvisioningAttempt,
                        existing_attempt["id"],
                    )
                else:
                    attempt = start_board_provisioning(
                        db,
                        canonical_key=item.board_key_snapshot,
                        settings=settings,
                        now=now,
                    )
                if attempt is None:
                    raise AutonomousDestinationError(
                        "BOARD_PROVISIONING_ATTEMPT_NOT_FOUND"
                    )
                run = _advance_started(
                    db,
                    run.id,
                    board_provisioning_attempt_id=attempt.id,
                    stage="BOARD_PROVISIONING",
                )

            if attempt.request_fingerprint != (
                (run.safe_metadata or {}).get("board_identity", {}).get(
                    "request_fingerprint"
                )
            ):
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_PROVISIONING_DRIFT"
                )
            if attempt.status == "STARTED":
                if attempt.provider_mutation_started_at is not None:
                    _set_terminal(
                        db,
                        run,
                        status="UNKNOWN",
                        code="PROVISIONING_MUTATION_ALREADY_STARTED",
                        now=now,
                    )
                    raise AutonomousDestinationError(
                        "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
                    )
                try:
                    attempt = await execute_board_provisioning_attempt(
                        db,
                        attempt.id,
                        settings=settings,
                        client=provisioning_client,
                        now=now,
                    )
                except BoardProvisioningError as exc:
                    if str(exc) != "BOARD_PROVISIONING_MUTATION_ALREADY_STARTED":
                        raise
                    _set_terminal(
                        db,
                        run,
                        status="UNKNOWN",
                        code="PROVISIONING_MUTATION_ALREADY_STARTED",
                        now=now,
                    )
                    raise AutonomousDestinationError(
                        "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
                    ) from exc
            if attempt.status == "UNKNOWN":
                _set_terminal(
                    db,
                    run,
                    status="UNKNOWN",
                    code="PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
                    now=now,
                )
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
                )
            if attempt.status == "FAILED":
                _set_terminal(
                    db,
                    run,
                    status="FAILED",
                    code="PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
                    now=now,
                )
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED"
                )
            if attempt.status != "SUCCEEDED" or not attempt.provider_board_id:
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_PROVISIONING_STATE_INVALID"
                )

            metadata = {
                **(run.safe_metadata or {}),
                "provider_called": True,
                "provider_board_id": attempt.provider_board_id,
            }
            run = _advance_started(
                db,
                run.id,
                stage="BOARD_CREATED",
                safe_metadata=metadata,
            )
            connection = db.get(PinterestConnection, attempt.connection_id)
            if connection is None:
                raise AutonomousDestinationError("PINTEREST_CONNECTION_REQUIRED")
            run = _advance_started(db, run.id, stage="BOARD_SYNC_PENDING")
            try:
                await sync_boards(db, connection, client=sync_client)
            except Exception as exc:
                db.rollback()
                metadata = {
                    **(run.safe_metadata or {}),
                    "sync_error_code": exc.__class__.__name__,
                }
                run = _advance_started(db, run.id, safe_metadata=metadata)
                return run
            try:
                try:
                    board = reconcile_provisioned_board(db, attempt.id, now=now)
                except BoardProvisioningError as exc:
                    if str(exc) not in _SYNC_PENDING_CODES:
                        raise
                    run = _advance_started(
                        db,
                        run.id,
                        stage="BOARD_SYNC_PENDING",
                    )
                    return run
            except BoardProvisioningError:
                raise
            except Exception as exc:
                db.rollback()
                metadata = {
                    **(run.safe_metadata or {}),
                    "reconcile_error_code": exc.__class__.__name__,
                }
                run = _advance_started(db, run.id, safe_metadata=metadata)
                return run
            run = _advance_started(
                db,
                run.id,
                pinterest_board_record_id=board.id,
                stage="BOARD_READY",
            )

        final_board_plan = board_strategy(
            db,
            canonical_key=item.board_key_snapshot,
            settings=settings,
        )
        _validate_strategy_binding(db, run, final_board_plan)
        if final_board_plan.get("status") != "ROUTE_EXISTING":
            raise AutonomousDestinationError("ROUTABLE_PINTEREST_BOARD_REQUIRED")
        if run.pinterest_board_record_id != final_board_plan.get("selected_board_id"):
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_DRIFT")

        execution_run = execute_autonomous_item(
            db,
            item.id,
            settings=settings,
            renderer=renderer,
            now=now,
        )
        if execution_run.status != "SUCCEEDED" or execution_run.stage != "PERMITTED":
            raise AutonomousDestinationError("AUTONOMOUS_EXECUTION_NOT_PERMITTED")
        metadata = {
            **(run.safe_metadata or {}),
            "autonomous_execution_run_id": execution_run.id,
        }
        run = _advance_started(
            db,
            run.id,
            autonomous_execution_run_id=execution_run.id,
            status="SUCCEEDED",
            stage="EXECUTION_READY",
            completed_at=now,
            safe_metadata=metadata,
        )
        return run
    except AutonomousDestinationError:
        raise
    except AutonomousExecutionError as exc:
        _set_terminal(db, run, status="FAILED", code=exc.code, now=now)
        raise AutonomousDestinationError(exc.code) from exc
    except BoardProvisioningError as exc:
        _set_terminal(db, run, status="FAILED", code=str(exc), now=now)
        raise AutonomousDestinationError(str(exc)) from exc
    except Exception as exc:
        _set_terminal(
            db,
            run,
            status="FAILED",
            code=exc.__class__.__name__,
            now=now,
        )
        raise AutonomousDestinationError(exc.__class__.__name__) from exc
