from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestBoard,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
)
from app.services.pinterest_autonomous_destination_readiness import (
    DESTINATION_POLICY_VERSION,
    AutonomousDestinationError,
    destination_readiness,
    existing_destination_run,
)
from app.services.pinterest_autonomous_execution import (
    AutonomousExecutionError,
    execute_autonomous_item,
)
from app.services.pinterest_board_strategy import board_strategy

DESTINATION_ACTOR = "autonomous-destination-v1"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _commit(db, run):
    db.commit()
    db.refresh(run)
    return run


def _terminal(
    db,
    run,
    code: str,
    now: datetime,
    *,
    status: str = "FAILED",
):
    if status not in {"FAILED", "UNKNOWN"}:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_TERMINAL_STATUS_INVALID")
    run = db.get(PinterestAutonomousDestinationRun, run.id)
    run.status = status
    run.completed_at = now
    run.safe_metadata = {**(run.safe_metadata or {}), "terminal_code": code}
    db.add(AuditLog(
        actor=DESTINATION_ACTOR,
        action=f"AUTONOMOUS_DESTINATION_{status}",
        entity_type="PinterestAutonomousDestinationRun",
        entity_id=run.id,
        metadata_json={
            "portfolio_item_id": run.portfolio_item_id,
            "stage": run.stage,
            "code": code,
        },
    ))
    return _commit(db, run)


def _validate_existing(db, run, input_fingerprint: str):
    if run.input_fingerprint != input_fingerprint:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_INPUT_DRIFT")
    if run.status == "FAILED":
        raise AutonomousDestinationError(
            "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED"
        )
    if run.status == "UNKNOWN":
        raise AutonomousDestinationError(
            "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
        )
    if run.status == "SUCCEEDED":
        execution = (
            db.get(
                PinterestAutonomousExecutionRun,
                run.autonomous_execution_run_id,
            )
            if run.autonomous_execution_run_id
            else None
        )
        if (
            execution is None
            or execution.status != "SUCCEEDED"
            or execution.stage != "PERMITTED"
        ):
            raise AutonomousDestinationError(
                "AUTONOMOUS_DESTINATION_SUCCEEDED_STAGE_DRIFT"
            )
        return run
    if run.status != "STARTED":
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_STATUS_INVALID")
    return run


def execute_existing_board_destination(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    renderer=None,
    now: datetime | None = None,
) -> PinterestAutonomousDestinationRun:
    settings = settings or get_settings()
    now = _utc(now or datetime.now(timezone.utc))
    readiness = destination_readiness(
        db,
        portfolio_item_id,
        settings=settings,
        now=now,
    )
    input_fingerprint = readiness.get("input_fingerprint")
    if not input_fingerprint:
        raise AutonomousDestinationError(
            (readiness.get("blockers")
             or ["AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE"])[0]
        )

    existing = existing_destination_run(db, portfolio_item_id)
    if existing is not None:
        existing = _validate_existing(db, existing, input_fingerprint)
        if existing.status == "SUCCEEDED":
            return existing

    if settings.pinterest_autonomous_board_ensure_enabled is not True:
        raise AutonomousDestinationError("AUTONOMOUS_BOARD_ENSURE_DISABLED")
    if readiness.get("ready") is not True:
        raise AutonomousDestinationError(
            (readiness.get("blockers")
             or ["AUTONOMOUS_DESTINATION_BLOCKED"])[0]
        )
    if readiness.get("board_state") != "BOARD_READY":
        raise AutonomousDestinationError("EXISTING_BOARD_ROUTE_REQUIRED")

    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    plan = db.get(PinterestPortfolioPlan, item.plan_id) if item else None
    if item is None or plan is None:
        raise AutonomousDestinationError(
            "AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE"
        )

    run = existing
    if run is None:
        run = PinterestAutonomousDestinationRun(
            portfolio_item_id=item.id,
            plan_id=plan.id,
            input_fingerprint=input_fingerprint,
            status="STARTED",
            stage="STARTED",
            safe_metadata={
                "policy_version": DESTINATION_POLICY_VERSION,
                "portfolio_item_fingerprint": item.item_fingerprint,
                "plan_fingerprint": plan.plan_fingerprint,
                "board_key_snapshot": item.board_key_snapshot,
                "provider_called": False,
                "pin_write_called": False,
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
                "plan_id": plan.id,
                "input_fingerprint": input_fingerprint,
            },
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            current = existing_destination_run(db, item.id)
            if (
                current is None
                or current.input_fingerprint != input_fingerprint
            ):
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_ALREADY_EXISTS"
                ) from None
            run = _validate_existing(db, current, input_fingerprint)
            if run.status == "SUCCEEDED":
                return run
        db.refresh(run)

    board_plan = board_strategy(
        db,
        canonical_key=item.board_key_snapshot,
        settings=settings,
    )
    board_id = board_plan.get("selected_board_id")
    if board_plan.get("status") != "ROUTE_EXISTING" or not board_id:
        raise AutonomousDestinationError("ROUTABLE_PINTEREST_BOARD_REQUIRED")
    board = db.get(PinterestBoard, board_id)
    if board is None or not board.is_active or not board.is_eligible:
        raise AutonomousDestinationError("PINTEREST_DESTINATION_DRIFT")

    run = db.get(PinterestAutonomousDestinationRun, run.id)
    if (
        run.pinterest_board_record_id
        and run.pinterest_board_record_id != board.id
    ):
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_DRIFT")
    run.pinterest_board_record_id = board.id
    run.stage = "BOARD_READY"
    run.safe_metadata = {
        **(run.safe_metadata or {}),
        "pinterest_board_record_id": board.id,
        "pinterest_external_board_id": board.external_board_id,
    }
    run = _commit(db, run)

    try:
        execution = execute_autonomous_item(
            db,
            item.id,
            settings=settings,
            renderer=renderer,
            now=now,
        )
    except AutonomousExecutionError as exc:
        _terminal(db, run, exc.code, now)
        raise AutonomousDestinationError(exc.code) from None
    except Exception as exc:
        _terminal(db, run, exc.__class__.__name__, now)
        raise AutonomousDestinationError(exc.__class__.__name__) from exc

    if execution.status != "SUCCEEDED" or execution.stage != "PERMITTED":
        return _terminal(
            db, run, "AUTONOMOUS_EXECUTION_NOT_PERMITTED", now
        )

    run = db.get(PinterestAutonomousDestinationRun, run.id)
    run.autonomous_execution_run_id = execution.id
    run.stage = "EXECUTION_READY"
    run.status = "SUCCEEDED"
    run.completed_at = now
    run.safe_metadata = {
        **(run.safe_metadata or {}),
        "autonomous_execution_run_id": execution.id,
        "provider_called": False,
        "pin_write_called": False,
        "ai_called": False,
    }
    db.add(AuditLog(
        actor=DESTINATION_ACTOR,
        action="AUTONOMOUS_DESTINATION_SUCCEEDED",
        entity_type="PinterestAutonomousDestinationRun",
        entity_id=run.id,
        metadata_json={
            "portfolio_item_id": item.id,
            "pinterest_board_record_id": board.id,
            "autonomous_execution_run_id": execution.id,
        },
    ))
    return _commit(db, run)
