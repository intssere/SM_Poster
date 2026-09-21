from __future__ import annotations

from app.models.domain import (
    PinterestAutonomousDestinationRun,
    PinterestBoard,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
)
from app.services.pinterest_autonomous_destination_existing import (
    _commit,
    _terminal,
)
from app.services.pinterest_autonomous_destination_readiness import (
    AutonomousDestinationError,
)
from app.services.pinterest_board_provisioning import (
    BoardProvisioningError,
    execute_board_provisioning_attempt,
    reconcile_provisioned_board,
    start_board_provisioning,
)
from app.services.pinterest_board_strategy import board_strategy
from app.services.pinterest_boards import sync_boards

_SAFE_SYNC_RETRY_CODES = {
    "PROVISIONED_BOARD_NOT_SYNCED",
    "BOARD_SYNC_REQUIRED",
    "BOARD_SYNC_STALE",
}


def _sync_pending(db, run, code: str):
    run = db.get(PinterestAutonomousDestinationRun, run.id)
    run.stage = "BOARD_SYNC_PENDING"
    run.safe_metadata = {**(run.safe_metadata or {}), "last_sync_code": code}
    return _commit(db, run)


def _bind_board(db, run, board):
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
    return _commit(db, run)


async def ensure_destination_board(
    db,
    run,
    item,
    *,
    settings,
    board_create_client=None,
    board_sync_client=None,
    now,
):
    board_plan = board_strategy(
        db,
        canonical_key=item.board_key_snapshot,
        settings=settings,
    )
    if board_plan.get("status") == "ROUTE_EXISTING":
        board = db.get(PinterestBoard, board_plan.get("selected_board_id"))
        return _bind_board(db, run, board), board

    attempt = None
    if run.board_provisioning_attempt_id:
        attempt = db.get(
            PinterestBoardProvisioningAttempt,
            run.board_provisioning_attempt_id,
        )
        if attempt is None:
            raise AutonomousDestinationError(
                "BOARD_PROVISIONING_ATTEMPT_NOT_FOUND"
            )
    else:
        existing_attempt = board_plan.get("existing_attempt")
        if isinstance(existing_attempt, dict) and existing_attempt.get("id"):
            attempt = db.get(
                PinterestBoardProvisioningAttempt,
                existing_attempt["id"],
            )
        if attempt is None:
            try:
                attempt = start_board_provisioning(
                    db,
                    canonical_key=item.board_key_snapshot,
                    settings=settings,
                    now=now,
                )
            except BoardProvisioningError as exc:
                raise AutonomousDestinationError(str(exc)) from None
        run = db.get(PinterestAutonomousDestinationRun, run.id)
        run.board_provisioning_attempt_id = attempt.id
        run.stage = "BOARD_PROVISIONING"
        run.safe_metadata = {
            **(run.safe_metadata or {}),
            "board_provisioning_attempt_id": attempt.id,
            "board_request_fingerprint": attempt.request_fingerprint,
        }
        run = _commit(db, run)

    if attempt.status == "UNKNOWN":
        return _terminal(
            db,
            run,
            "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
            now,
            status="UNKNOWN",
        ), None
    if attempt.status == "FAILED":
        return _terminal(
            db,
            run,
            "PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
            now,
        ), None

    if attempt.status == "STARTED":
        if attempt.provider_mutation_started_at is not None:
            return _terminal(
                db,
                run,
                "PROVISIONING_MUTATION_ALREADY_STARTED",
                now,
                status="UNKNOWN",
            ), None
        try:
            attempt = await execute_board_provisioning_attempt(
                db,
                attempt.id,
                settings=settings,
                client=board_create_client,
                now=now,
            )
        except BoardProvisioningError as exc:
            raise AutonomousDestinationError(str(exc)) from None

    run = db.get(PinterestAutonomousDestinationRun, run.id)
    run.safe_metadata = {
        **(run.safe_metadata or {}),
        "provider_called": bool(
            (attempt.safe_metadata or {}).get("provider_called")
        ),
        "board_provisioning_status": attempt.status,
        "provider_board_id": attempt.provider_board_id,
    }

    if attempt.status == "UNKNOWN":
        return _terminal(
            db,
            run,
            attempt.error_code
            or "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
            now,
            status="UNKNOWN",
        ), None
    if attempt.status == "FAILED":
        return _terminal(
            db,
            run,
            attempt.error_code
            or "PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
            now,
        ), None
    if attempt.status != "SUCCEEDED" or not attempt.provider_board_id:
        return _terminal(
            db,
            run,
            "PROVISIONING_RESULT_INVALID",
            now,
            status="UNKNOWN",
        ), None

    run.stage = "BOARD_CREATED"
    run = _commit(db, run)
    connection = db.get(PinterestConnection, attempt.connection_id)
    if connection is None or connection.status != "CONNECTED":
        return _terminal(
            db, run, "PINTEREST_CONNECTION_REQUIRED", now
        ), None

    try:
        await sync_boards(db, connection, board_sync_client)
    except Exception:
        return _sync_pending(
            db, run, "BOARD_SYNC_RETRY_REQUIRED"
        ), None

    try:
        board = reconcile_provisioned_board(db, attempt.id, now=now)
    except BoardProvisioningError as exc:
        code = str(exc)
        if code in _SAFE_SYNC_RETRY_CODES:
            return _sync_pending(db, run, code), None
        _terminal(db, run, code, now)
        raise AutonomousDestinationError(code) from None

    return _bind_board(db, run, board), board
