from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    PinterestAutonomousDestinationRun,
    PinterestBoard,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
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
from app.services.pinterest_board_provisioning import (
    BoardProvisioningError,
    execute_board_provisioning_attempt,
    reconcile_provisioned_board,
    start_board_provisioning,
)
from app.services.pinterest_board_strategy import board_strategy
from app.services.pinterest_boards import sync_boards


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reload(db, run_id: str) -> PinterestAutonomousDestinationRun:
    run = db.get(PinterestAutonomousDestinationRun, run_id)
    if run is None:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_RUN_NOT_FOUND")
    return run


def _terminalize(
    db,
    run_id: str,
    *,
    status: str,
    stage: str | None = None,
    code: str,
    now: datetime,
) -> PinterestAutonomousDestinationRun:
    values: dict[str, Any] = {
        "status": status,
        "completed_at": now,
    }
    if stage is not None:
        values["stage"] = stage
    current = db.get(PinterestAutonomousDestinationRun, run_id)
    metadata = dict(current.safe_metadata or {}) if current is not None else {}
    metadata["terminal_code"] = str(code)[:160]
    values["safe_metadata"] = metadata
    result = db.execute(
        update(PinterestAutonomousDestinationRun)
        .execution_options(synchronize_session=False)
        .where(
            PinterestAutonomousDestinationRun.id == run_id,
            PinterestAutonomousDestinationRun.status == "STARTED",
        )
        .values(**values)
    )
    db.commit()
    run = _reload(db, run_id)
    if result.rowcount != 1 and run.status not in {"FAILED", "UNKNOWN", "SUCCEEDED"}:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_TERMINAL_CAS_FAILED")
    return run


def _persist_identity(
    db,
    run: PinterestAutonomousDestinationRun,
    *,
    readiness: dict[str, Any],
    item: PinterestPortfolioPlanItem,
) -> PinterestAutonomousDestinationRun:
    board_plan = readiness["board_strategy"]
    metadata = dict(run.safe_metadata or {})
    expected = {
        "policy_version": DESTINATION_POLICY_VERSION,
        "portfolio_item_fingerprint": item.item_fingerprint,
        "board_canonical_key": board_plan.get("canonical_key"),
        "board_desired_name": board_plan.get("desired_name"),
        "board_desired_description": board_plan.get("desired_description"),
        "board_privacy": board_plan.get("privacy"),
    }
    for key, value in expected.items():
        if key in metadata and metadata[key] != value:
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_IDENTITY_DRIFT")
        metadata[key] = value
    request_fingerprint = board_plan.get("request_fingerprint")
    if request_fingerprint:
        if (
            metadata.get("board_request_fingerprint")
            and metadata["board_request_fingerprint"] != request_fingerprint
        ):
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_REQUEST_DRIFT")
        metadata["board_request_fingerprint"] = request_fingerprint
    selected = board_plan.get("selected_board_id")
    if selected:
        if run.pinterest_board_record_id and run.pinterest_board_record_id != selected:
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_ROUTING_DRIFT")
        if (
            metadata.get("board_record_id")
            and metadata["board_record_id"] != selected
        ):
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_ROUTING_DRIFT")
        metadata["board_record_id"] = selected
    run.safe_metadata = metadata
    db.commit()
    db.refresh(run)
    return run


def _load_bound_attempt(
    db,
    run: PinterestAutonomousDestinationRun,
    readiness: dict[str, Any],
) -> PinterestBoardProvisioningAttempt | None:
    attempt_id = run.board_provisioning_attempt_id
    existing = readiness["board_strategy"].get("existing_attempt")
    if attempt_id is None and isinstance(existing, dict):
        attempt_id = existing.get("id")
    if attempt_id is None:
        return None
    attempt = db.get(PinterestBoardProvisioningAttempt, attempt_id)
    if attempt is None:
        raise AutonomousDestinationError("BOARD_PROVISIONING_ATTEMPT_NOT_FOUND")
    if run.board_provisioning_attempt_id and run.board_provisioning_attempt_id != attempt.id:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_PROVISIONING_DRIFT")
    return attempt


def _bind_attempt(
    db,
    run: PinterestAutonomousDestinationRun,
    attempt: PinterestBoardProvisioningAttempt,
    *,
    stage: str = "BOARD_PROVISIONING",
) -> PinterestAutonomousDestinationRun:
    if run.board_provisioning_attempt_id and run.board_provisioning_attempt_id != attempt.id:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_PROVISIONING_DRIFT")
    request_fp = (run.safe_metadata or {}).get("board_request_fingerprint")
    if request_fp and request_fp != attempt.request_fingerprint:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_PROVISIONING_DRIFT")
    run.board_provisioning_attempt_id = attempt.id
    run.stage = stage
    db.commit()
    db.refresh(run)
    return run


def _bind_board(
    db,
    run: PinterestAutonomousDestinationRun,
    board: PinterestBoard,
) -> PinterestAutonomousDestinationRun:
    if run.pinterest_board_record_id and run.pinterest_board_record_id != board.id:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_ROUTING_DRIFT")
    run.pinterest_board_record_id = board.id
    run.stage = "BOARD_READY"
    metadata = dict(run.safe_metadata or {})
    if metadata.get("board_record_id") and metadata["board_record_id"] != board.id:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_ROUTING_DRIFT")
    metadata["board_record_id"] = board.id
    run.safe_metadata = metadata
    db.commit()
    db.refresh(run)
    return run


def _existing_routed_board(
    db,
    run: PinterestAutonomousDestinationRun,
    readiness: dict[str, Any],
) -> PinterestBoard | None:
    board_plan = readiness["board_strategy"]
    selected = board_plan.get("selected_board_id")
    if board_plan.get("status") != "ROUTE_EXISTING" or not selected:
        return None
    board = db.get(PinterestBoard, selected)
    if board is None:
        raise AutonomousDestinationError("PINTEREST_BOARD_NOT_FOUND")
    if run.pinterest_board_record_id and run.pinterest_board_record_id != board.id:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_BOARD_ROUTING_DRIFT")
    return board


def _start_or_recover_attempt(
    db,
    run: PinterestAutonomousDestinationRun,
    readiness: dict[str, Any],
    *,
    settings: Settings,
    now: datetime,
) -> PinterestBoardProvisioningAttempt:
    bound = _load_bound_attempt(db, run, readiness)
    if bound is not None:
        return bound
    board_plan = readiness["board_strategy"]
    if board_plan.get("status") != "PROVISION_REQUIRED":
        raise AutonomousDestinationError(
            (board_plan.get("blockers") or ["BOARD_PROVISIONING_NOT_READY"])[0]
        )
    try:
        attempt = start_board_provisioning(
            db,
            canonical_key=board_plan.get("canonical_key"),
            settings=settings,
            now=now,
        )
    except BoardProvisioningError as exc:
        refreshed_plan = board_strategy(
            db,
            canonical_key=board_plan.get("canonical_key"),
            settings=settings,
        )
        existing = refreshed_plan.get("existing_attempt")
        if not isinstance(existing, dict) or not existing.get("id"):
            raise AutonomousDestinationError(str(exc)) from None
        attempt = db.get(PinterestBoardProvisioningAttempt, existing["id"])
        if attempt is None:
            raise AutonomousDestinationError(str(exc)) from None
    return attempt


async def _sync_and_reconcile(
    db,
    run: PinterestAutonomousDestinationRun,
    attempt: PinterestBoardProvisioningAttempt,
    *,
    sync_client,
    now: datetime,
) -> PinterestBoard | None:
    connection = db.get(PinterestConnection, attempt.connection_id)
    if connection is None or connection.status != "CONNECTED":
        raise AutonomousDestinationError("PINTEREST_CONNECTION_REQUIRED")
    run.stage = "BOARD_SYNC_PENDING"
    db.commit()
    db.refresh(run)
    try:
        await sync_boards(db, connection, client=sync_client)
    except Exception:
        # A confirmed board create is never replayed. Read-only sync is resumable.
        return None
    try:
        return reconcile_provisioned_board(db, attempt.id, now=now)
    except BoardProvisioningError as exc:
        if str(exc) in {"PROVISIONED_BOARD_NOT_SYNCED", "BOARD_SYNC_REQUIRED", "BOARD_SYNC_STALE"}:
            return None
        raise AutonomousDestinationError(str(exc)) from None


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
    settings = settings or get_settings()
    now = _utc(now or _now())
    if settings.pinterest_autonomous_board_ensure_enabled is not True:
        raise AutonomousDestinationError("AUTONOMOUS_BOARD_ENSURE_DISABLED")

    readiness = destination_readiness(
        db,
        portfolio_item_id,
        settings=settings,
        now=now,
    )
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise AutonomousDestinationError("PORTFOLIO_ITEM_NOT_FOUND")
    input_fingerprint = readiness.get("input_fingerprint")
    if not input_fingerprint:
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE")

    run = existing_destination_run(db, item.id)
    if run is not None:
        if run.input_fingerprint != input_fingerprint:
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_INPUT_DRIFT")
        if run.status == "SUCCEEDED":
            return run
        if run.status == "FAILED":
            raise AutonomousDestinationError(
                "AUTONOMOUS_DESTINATION_FAILED_RECONCILIATION_REQUIRED"
            )
        if run.status == "UNKNOWN":
            raise AutonomousDestinationError(
                "AUTONOMOUS_DESTINATION_UNKNOWN_RECONCILIATION_REQUIRED"
            )
        if run.status != "STARTED":
            raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_STATUS_INVALID")
    else:
        run = PinterestAutonomousDestinationRun(
            portfolio_item_id=item.id,
            plan_id=item.plan_id,
            input_fingerprint=input_fingerprint,
            status="STARTED",
            stage="STARTED",
            safe_metadata={
                "policy_version": DESTINATION_POLICY_VERSION,
                "provider_called": False,
                "pin_write_called": False,
                "ai_called": False,
            },
            started_at=now,
        )
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            run = existing_destination_run(db, item.id)
            if run is None:
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_ALREADY_EXISTS"
                ) from None
            if run.input_fingerprint != input_fingerprint:
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_INPUT_DRIFT"
                ) from None
            if run.status != "STARTED":
                raise AutonomousDestinationError(
                    "AUTONOMOUS_DESTINATION_ALREADY_EXISTS"
                ) from None
        db.refresh(run)

    run = _persist_identity(db, run, readiness=readiness, item=item)

    routed = _existing_routed_board(db, run, readiness)
    if routed is not None:
        run = _bind_board(db, run, routed)
    else:
        attempt = _start_or_recover_attempt(
            db,
            run,
            readiness,
            settings=settings,
            now=now,
        )
        run = _bind_attempt(db, run, attempt)

        if attempt.status == "UNKNOWN":
            return _terminalize(
                db,
                run.id,
                status="UNKNOWN",
                code="PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
                now=now,
            )
        if attempt.status == "FAILED":
            return _terminalize(
                db,
                run.id,
                status="FAILED",
                code="PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
                now=now,
            )
        if attempt.status == "STARTED":
            if attempt.provider_mutation_started_at is not None:
                return _terminalize(
                    db,
                    run.id,
                    status="UNKNOWN",
                    code="PROVISIONING_MUTATION_ALREADY_STARTED",
                    now=now,
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
                current = db.get(PinterestBoardProvisioningAttempt, attempt.id)
                if (
                    current is not None
                    and current.status == "STARTED"
                    and current.provider_mutation_started_at is not None
                ):
                    return _terminalize(
                        db,
                        run.id,
                        status="UNKNOWN",
                        code="PROVISIONING_MUTATION_ALREADY_STARTED",
                        now=now,
                    )
                raise AutonomousDestinationError(str(exc)) from None

        run = _bind_attempt(
            db,
            _reload(db, run.id),
            attempt,
            stage="BOARD_CREATED" if attempt.status == "SUCCEEDED" else "BOARD_PROVISIONING",
        )
        if attempt.status == "UNKNOWN":
            return _terminalize(
                db,
                run.id,
                status="UNKNOWN",
                code=attempt.error_code or "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
                now=now,
            )
        if attempt.status == "FAILED":
            return _terminalize(
                db,
                run.id,
                status="FAILED",
                code=attempt.error_code or "PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
                now=now,
            )
        if attempt.status != "SUCCEEDED":
            raise AutonomousDestinationError("PROVISIONING_ATTEMPT_STATE_INVALID")

        board = await _sync_and_reconcile(
            db,
            run,
            attempt,
            sync_client=sync_client,
            now=now,
        )
        if board is None:
            return _reload(db, run.id)
        run = _bind_board(db, _reload(db, run.id), board)

    try:
        execution = execute_autonomous_item(
            db,
            portfolio_item_id,
            settings=settings,
            renderer=renderer,
            now=now,
        )
    except AutonomousExecutionError as exc:
        _terminalize(
            db,
            run.id,
            status="FAILED",
            stage="BOARD_READY",
            code=exc.code,
            now=now,
        )
        raise AutonomousDestinationError(exc.code) from None
    except Exception as exc:
        _terminalize(
            db,
            run.id,
            status="FAILED",
            stage="BOARD_READY",
            code=exc.__class__.__name__,
            now=now,
        )
        raise

    if execution.status != "SUCCEEDED" or execution.stage != "PERMITTED":
        _terminalize(
            db,
            run.id,
            status="FAILED",
            stage="BOARD_READY",
            code="AUTONOMOUS_EXECUTION_NOT_PERMITTED",
            now=now,
        )
        raise AutonomousDestinationError("AUTONOMOUS_EXECUTION_NOT_PERMITTED")

    run = _reload(db, run.id)
    if run.status != "STARTED":
        if run.status == "SUCCEEDED" and run.autonomous_execution_run_id == execution.id:
            return run
        raise AutonomousDestinationError("AUTONOMOUS_DESTINATION_TERMINAL_STATE_DRIFT")
    run.autonomous_execution_run_id = execution.id
    run.stage = "EXECUTION_READY"
    run.status = "SUCCEEDED"
    run.completed_at = now
    metadata = dict(run.safe_metadata or {})
    metadata["autonomous_execution_run_id"] = execution.id
    run.safe_metadata = metadata
    db.commit()
    db.refresh(run)
    return run
