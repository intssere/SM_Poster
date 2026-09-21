from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    PinterestAutonomousDestinationRun,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
)
from app.services.pinterest_autonomous_destination_existing import (
    DESTINATION_ACTOR,
    _validate_existing,
    execute_existing_board_destination,
)
from app.services.pinterest_autonomous_destination_provisioning import (
    ensure_destination_board,
)
from app.services.pinterest_autonomous_destination_readiness import (
    DESTINATION_POLICY_VERSION,
    AutonomousDestinationError,
    destination_readiness,
    existing_destination_run,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _start_run(db, item, plan, input_fingerprint: str, now: datetime):
    current = existing_destination_run(db, item.id)
    if current is not None:
        return _validate_existing(db, current, input_fingerprint)

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
        return _validate_existing(db, current, input_fingerprint)
    db.refresh(run)
    return run


async def execute_autonomous_destination(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    board_create_client=None,
    board_sync_client=None,
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

    if readiness.get("board_state") == "BOARD_READY":
        return execute_existing_board_destination(
            db,
            portfolio_item_id,
            settings=settings,
            renderer=renderer,
            now=now,
        )

    if settings.pinterest_autonomous_board_ensure_enabled is not True:
        raise AutonomousDestinationError("AUTONOMOUS_BOARD_ENSURE_DISABLED")
    if readiness.get("ready") is not True:
        raise AutonomousDestinationError(
            (readiness.get("blockers")
             or ["AUTONOMOUS_DESTINATION_BLOCKED"])[0]
        )

    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    plan = db.get(PinterestPortfolioPlan, item.plan_id) if item else None
    if item is None or plan is None:
        raise AutonomousDestinationError(
            "AUTONOMOUS_DESTINATION_IDENTITY_INCOMPLETE"
        )

    run = _start_run(db, item, plan, input_fingerprint, now)
    if run.status == "SUCCEEDED":
        return run

    run, board = await ensure_destination_board(
        db,
        run,
        item,
        settings=settings,
        board_create_client=board_create_client,
        board_sync_client=board_sync_client,
        now=now,
    )
    if board is None or run.status in {"FAILED", "UNKNOWN"}:
        return run

    return execute_existing_board_destination(
        db,
        portfolio_item_id,
        settings=settings,
        renderer=renderer,
        now=now,
    )
