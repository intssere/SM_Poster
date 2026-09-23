from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    Board,
    PinApproval,
    PinPublication,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestBoard,
    PinterestConnection,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.pinterest_autonomous_generation import execute_autonomous_generation
from app.services.pinterest_autonomous_run_lineage import (
    latest_run,
    next_attempt_context,
    retry_reconciliation,
)
from app.services.pinterest_board_strategy import board_strategy
from app.services.pinterest_optimizer_apply import OPTIMIZER_METADATA_KEY
from app.services.pinterest_seo_intelligence import persist_seo_brief
from app.services.publication_identity import PublicationIdentityService
from app.services.routine_autonomous_authorization import (
    AUTONOMOUS_ACTOR,
    auto_permit_publication,
    authorize_draft_autonomously,
)

EXECUTION_POLICY_VERSION = "PINTEREST_AUTONOMOUS_EXECUTION_V1"
EXECUTION_ACTOR = "autonomous-execution-v1"

STAGES = (
    "STARTED",
    "SEO_READY",
    "GENERATED",
    "AUTHORIZED",
    "PUBLICATION_CREATED",
    "PERMITTED",
)
_STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}


class AutonomousExecutionError(RuntimeError):
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
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _publish_unknown_count(db) -> int:
    return len(list(db.scalars(
        select(PinPublication.id)
        .where(PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN)
    ).all()))


def _optimizer_application(db, plan_id: str) -> PinterestOptimizerApplication | None:
    return db.scalar(
        select(PinterestOptimizerApplication)
        .where(PinterestOptimizerApplication.plan_id == plan_id)
        .limit(1)
    )


def _existing_run(db, item_id: str) -> PinterestAutonomousExecutionRun | None:
    return latest_run(db, PinterestAutonomousExecutionRun, item_id)


def _optimizer_metadata(item: PinterestPortfolioPlanItem) -> dict[str, Any] | None:
    metadata = item.selection_metadata or {}
    value = metadata.get(OPTIMIZER_METADATA_KEY)
    return value if isinstance(value, dict) else None


def _recommended_position(item: PinterestPortfolioPlanItem) -> int:
    metadata = _optimizer_metadata(item)
    value = metadata.get("recommended_position") if metadata else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AutonomousExecutionError("OPTIMIZER_RECOMMENDED_POSITION_REQUIRED")
    return value


def deterministic_scheduled_for(
    db,
    item: PinterestPortfolioPlanItem,
    *,
    settings: Settings | None = None,
) -> datetime:
    settings = settings or get_settings()
    if item.planned_date is None:
        raise AutonomousExecutionError("PLANNED_DATE_REQUIRED")
    if item.is_reserve:
        raise AutonomousExecutionError("RESERVE_ITEM_NOT_EXECUTABLE")

    same_day = list(db.scalars(
        select(PinterestPortfolioPlanItem)
        .where(
            PinterestPortfolioPlanItem.plan_id == item.plan_id,
            PinterestPortfolioPlanItem.is_reserve.is_(False),
            PinterestPortfolioPlanItem.planned_date == item.planned_date,
        )
        .order_by(PinterestPortfolioPlanItem.slot_index, PinterestPortfolioPlanItem.id)
    ).all())
    if not same_day:
        raise AutonomousExecutionError("SCHEDULE_DAY_EMPTY")

    ordered = sorted(
        same_day,
        key=lambda row: (
            _recommended_position(row),
            int(row.slot_index),
            row.id,
        ),
    )
    try:
        index = next(idx for idx, row in enumerate(ordered) if row.id == item.id)
    except StopIteration as exc:
        raise AutonomousExecutionError("PORTFOLIO_ITEM_NOT_IN_SCHEDULE_DAY") from exc

    start_minute = int(settings.pinterest_autonomous_schedule_start_minute_utc)
    end_minute = int(settings.pinterest_autonomous_schedule_end_minute_utc)
    if not (0 <= start_minute < end_minute <= 1439):
        raise AutonomousExecutionError("INVALID_AUTONOMOUS_SCHEDULE_WINDOW")

    count = len(ordered)
    span_us = (end_minute - start_minute) * 60 * 1_000_000
    midpoint_offset_us = (span_us * (2 * index + 1)) // (2 * count)
    midnight = datetime(
        item.planned_date.year,
        item.planned_date.month,
        item.planned_date.day,
        tzinfo=timezone.utc,
    )
    return midnight + timedelta(minutes=start_minute, microseconds=midpoint_offset_us)


def _execution_input_fingerprint(
    *,
    item: PinterestPortfolioPlanItem,
    plan: PinterestPortfolioPlan,
    optimizer: PinterestOptimizerApplication,
    optimizer_metadata: dict[str, Any],
    board_plan: dict[str, Any],
    scheduled_for: datetime,
) -> str:
    return _hash({
        "policy_version": EXECUTION_POLICY_VERSION,
        "portfolio_item_id": item.id,
        "portfolio_item_fingerprint": item.item_fingerprint,
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "optimizer_application_id": optimizer.id,
        "optimizer_fingerprint": optimizer.optimizer_fingerprint,
        "optimizer_input_state_fingerprint": optimizer.input_state_fingerprint,
        "item_optimizer_metadata": {
            "optimizer_policy_version": optimizer_metadata.get("optimizer_policy_version"),
            "optimizer_fingerprint": optimizer_metadata.get("optimizer_fingerprint"),
            "input_state_fingerprint": optimizer_metadata.get("input_state_fingerprint"),
            "recommended_position": optimizer_metadata.get("recommended_position"),
            "target_slot_index": optimizer_metadata.get("target_slot_index"),
            "target_planned_date": optimizer_metadata.get("target_planned_date"),
            "selection_reason": optimizer_metadata.get("selection_reason"),
        },
        "product_id": item.product_id,
        "local_board_id": item.local_board_id,
        "board_key_snapshot": item.board_key_snapshot,
        "content_angle_id": item.content_angle_id,
        "angle_key_snapshot": item.angle_key_snapshot,
        "scheduled_for": scheduled_for.isoformat(),
        "pinterest_board_record_id": board_plan.get("selected_board_id"),
        "pinterest_external_board_id": board_plan.get("selected_external_board_id"),
    })


def _stage_at_least(run: PinterestAutonomousExecutionRun, stage: str) -> bool:
    return _STAGE_INDEX.get(run.stage, -1) >= _STAGE_INDEX[stage]


def execution_readiness(
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
        raise AutonomousExecutionError("PORTFOLIO_ITEM_NOT_FOUND")

    blockers: list[str] = []
    plan = db.get(PinterestPortfolioPlan, item.plan_id)
    if plan is None:
        blockers.append("PORTFOLIO_PLAN_NOT_FOUND")
    elif plan.status != "ACTIVE":
        blockers.append("PORTFOLIO_PLAN_NOT_ACTIVE")

    optimizer = _optimizer_application(db, item.plan_id)
    if optimizer is None:
        blockers.append("OPTIMIZER_APPLICATION_REQUIRED")
    elif plan is not None and (
        optimizer.status != "APPLIED"
        or optimizer.plan_fingerprint_snapshot != plan.plan_fingerprint
    ):
        blockers.append("OPTIMIZER_APPLICATION_DRIFT")

    metadata = _optimizer_metadata(item)
    if metadata is None:
        blockers.append("OPTIMIZER_ITEM_METADATA_REQUIRED")
    elif optimizer is not None:
        if (
            metadata.get("optimizer_fingerprint") != optimizer.optimizer_fingerprint
            or metadata.get("optimizer_policy_version") != optimizer.optimizer_policy_version
            or metadata.get("input_state_fingerprint") != optimizer.input_state_fingerprint
        ):
            blockers.append("OPTIMIZER_ITEM_BINDING_MISMATCH")

    if item.is_reserve:
        blockers.append("RESERVE_ITEM_NOT_EXECUTABLE")
    if item.planned_date is None:
        blockers.append("PLANNED_DATE_REQUIRED")
    if item.status not in {"PLANNED", "GENERATED", "SCHEDULED"}:
        blockers.append("PORTFOLIO_ITEM_STATUS_NOT_EXECUTABLE")

    if _publish_unknown_count(db):
        blockers.append("PUBLISH_UNKNOWN_PRESENT")

    scheduled_for = None
    if item.planned_date is not None and not item.is_reserve:
        try:
            scheduled_for = deterministic_scheduled_for(db, item, settings=settings)
        except AutonomousExecutionError as exc:
            blockers.append(exc.code)
    if scheduled_for is not None and scheduled_for <= now:
        blockers.append("SCHEDULE_TIME_NOT_FUTURE")

    board_plan: dict[str, Any] | None = None
    if item.board_key_snapshot:
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
                "selected_board_id": None,
                "selected_external_board_id": None,
            }
        if board_plan.get("status") != "ROUTE_EXISTING":
            blockers.append("ROUTABLE_PINTEREST_BOARD_REQUIRED")
    else:
        blockers.append("PORTFOLIO_BOARD_KEY_REQUIRED")

    if settings.pinterest_seo_brief_persistence_enabled is not True:
        blockers.append("SEO_BRIEF_PERSISTENCE_DISABLED")
    if settings.pinterest_autonomous_generation_enabled is not True:
        blockers.append("AUTONOMOUS_GENERATION_DISABLED")
    if settings.routine_autonomous_authorization_enabled is not True:
        blockers.append("AUTONOMOUS_AUTHORIZATION_DISABLED")
    if settings.pinterest_autonomous_execution_enabled is not True:
        blockers.append("AUTONOMOUS_EXECUTION_DISABLED")

    input_fingerprint = None
    if (
        plan is not None
        and optimizer is not None
        and metadata is not None
        and scheduled_for is not None
        and board_plan is not None
        and board_plan.get("status") == "ROUTE_EXISTING"
    ):
        input_fingerprint = _execution_input_fingerprint(
            item=item,
            plan=plan,
            optimizer=optimizer,
            optimizer_metadata=metadata,
            board_plan=board_plan,
            scheduled_for=scheduled_for,
        )

    existing = _existing_run(db, item.id)
    already_executed = False
    retry_reconciliation_record = None
    if existing is not None:
        if existing.status == "FAILED":
            retry_reconciliation_record = retry_reconciliation(
                db,
                kind="execution",
                failed_run=existing,
                retry_input_fingerprint=input_fingerprint,
            )
            if retry_reconciliation_record is None:
                blockers.append("AUTONOMOUS_EXECUTION_FAILED_RECONCILIATION_REQUIRED")
        elif input_fingerprint is None or existing.input_fingerprint != input_fingerprint:
            blockers.append("AUTONOMOUS_EXECUTION_INPUT_DRIFT")
        elif existing.status == "SUCCEEDED":
            already_executed = True
            if existing.stage != "PERMITTED":
                blockers.append("AUTONOMOUS_EXECUTION_SUCCEEDED_STAGE_DRIFT")
        elif existing.status != "STARTED":
            blockers.append("AUTONOMOUS_EXECUTION_STATUS_INVALID")

    # Keep blocker order deterministic without duplicating codes generated by
    # overlapping structural checks.
    blockers = list(dict.fromkeys(blockers))

    return {
        "policy_version": EXECUTION_POLICY_VERSION,
        "enabled": bool(settings.pinterest_autonomous_execution_enabled),
        "portfolio_item_id": item.id,
        "plan_id": item.plan_id,
        "ready": not blockers,
        "already_executed": already_executed,
        "blockers": blockers,
        "optimizer_binding": {
            "application_id": optimizer.id if optimizer else None,
            "optimizer_fingerprint": optimizer.optimizer_fingerprint if optimizer else None,
            "item_optimizer_fingerprint": metadata.get("optimizer_fingerprint") if metadata else None,
            "recommended_position": metadata.get("recommended_position") if metadata else None,
        },
        "board_routing": {
            "status": board_plan.get("status") if board_plan else None,
            "selected_board_id": board_plan.get("selected_board_id") if board_plan else None,
            "selected_external_board_id": board_plan.get("selected_external_board_id") if board_plan else None,
            "blockers": board_plan.get("blockers") if board_plan else [],
        },
        "scheduled_for": scheduled_for,
        "input_fingerprint": input_fingerprint,
        "existing_run": {
            "id": existing.id if existing else None,
            "status": existing.status if existing else None,
            "stage": existing.stage if existing else None,
            "attempt_number": existing.attempt_number if existing else None,
            "seo_brief_id": existing.seo_brief_id if existing else None,
            "generation_run_id": existing.generation_run_id if existing else None,
            "approval_id": existing.approval_id if existing else None,
            "publication_id": existing.publication_id if existing else None,
            "routine_permit_id": existing.routine_permit_id if existing else None,
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


def _validate_seo(brief: PinterestSeoBrief, item_id: str) -> None:
    if brief.portfolio_item_id != item_id or brief.status != "CURRENT":
        raise AutonomousExecutionError("SEO_BRIEF_DRIFT")


def _validate_generation(
    generation: PinterestAutonomousGenerationRun,
    *,
    item: PinterestPortfolioPlanItem,
    seo_brief_id: str,
) -> None:
    if (
        generation.portfolio_item_id != item.id
        or generation.seo_brief_id != seo_brief_id
        or generation.status != "SUCCEEDED"
        or not generation.draft_id
        or not generation.creative_id
    ):
        raise AutonomousExecutionError("AUTONOMOUS_GENERATION_DRIFT")


def _validate_approval(
    approval: PinApproval,
    *,
    generation: PinterestAutonomousGenerationRun,
) -> None:
    if (
        approval.decision != "APPROVED"
        or approval.decided_by != AUTONOMOUS_ACTOR
        or approval.draft_id != generation.draft_id
        or approval.creative_id != generation.creative_id
    ):
        raise AutonomousExecutionError("AUTONOMOUS_APPROVAL_DRIFT")


def _same_schedule(left: datetime | None, right: datetime) -> bool:
    return left is not None and _utc(left) == _utc(right)


def _publication_matches(
    publication: PinPublication,
    *,
    approval: PinApproval,
    generation: PinterestAutonomousGenerationRun,
    board: PinterestBoard,
    connection: PinterestConnection,
    scheduled_for: datetime,
) -> bool:
    return bool(
        publication.approval_id == approval.id
        and publication.draft_id == generation.draft_id
        and publication.creative_id == generation.creative_id
        and publication.pinterest_connection_id == connection.id
        and publication.pinterest_board_record_id == board.id
        and publication.pinterest_board_id_snapshot == board.external_board_id
        and publication.status == PublicationStatus.SCHEDULED
        and _same_schedule(publication.scheduled_for, scheduled_for)
    )


def _recover_publication(
    db,
    *,
    approval: PinApproval,
    generation: PinterestAutonomousGenerationRun,
    board: PinterestBoard,
    connection: PinterestConnection,
    scheduled_for: datetime,
) -> PinPublication | None:
    rows = list(db.scalars(
        select(PinPublication)
        .where(
            PinPublication.approval_id == approval.id,
            PinPublication.draft_id == generation.draft_id,
            PinPublication.creative_id == generation.creative_id,
            PinPublication.pinterest_connection_id == connection.id,
            PinPublication.pinterest_board_record_id == board.id,
        )
        .order_by(PinPublication.created_at, PinPublication.id)
    ).all())
    if len(rows) > 1:
        raise AutonomousExecutionError("PUBLICATION_RECOVERY_AMBIGUOUS")
    if not rows:
        return None
    publication = rows[0]
    if not _publication_matches(
        publication,
        approval=approval,
        generation=generation,
        board=board,
        connection=connection,
        scheduled_for=scheduled_for,
    ):
        raise AutonomousExecutionError("PUBLICATION_RECOVERY_DRIFT")
    return publication


def _publication_service(db) -> PublicationIdentityService:
    Session = sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return PublicationIdentityService(session_factory=Session)


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, AutonomousExecutionError):
        return str(exc.code)[:120]
    detail = str(exc).strip()
    if (
        detail
        and len(detail) <= 120
        and all(ch.isupper() or ch.isdigit() or ch == "_" for ch in detail)
    ):
        return detail
    return exc.__class__.__name__[:120]


def _fail_run(
    db,
    run: PinterestAutonomousExecutionRun,
    exc: Exception,
    *,
    now: datetime,
) -> None:
    try:
        db.rollback()
        current = db.get(PinterestAutonomousExecutionRun, run.id)
        if current is None or current.status != "STARTED":
            return
        code = _failure_code(exc)
        current.status = "FAILED"
        current.completed_at = now
        current.safe_metadata = {
            **(current.safe_metadata or {}),
            "error_code": str(code)[:120],
            "provider_called": False,
            "ai_called": False,
        }
        db.add(AuditLog(
            actor=EXECUTION_ACTOR,
            action="AUTONOMOUS_EXECUTION_FAILED",
            entity_type="PinterestAutonomousExecutionRun",
            entity_id=current.id,
            metadata_json={
                "portfolio_item_id": current.portfolio_item_id,
                "stage": current.stage,
                "error_code": str(code)[:120],
            },
        ))
        db.commit()
    except Exception:
        db.rollback()


def execute_autonomous_item(
    db,
    portfolio_item_id: str,
    *,
    settings: Settings | None = None,
    renderer=None,
    now: datetime | None = None,
) -> PinterestAutonomousExecutionRun:
    settings = settings or get_settings()
    now = _utc(now or _now())
    item = db.get(PinterestPortfolioPlanItem, portfolio_item_id)
    if item is None:
        raise AutonomousExecutionError("PORTFOLIO_ITEM_NOT_FOUND")

    readiness = execution_readiness(
        db,
        portfolio_item_id,
        settings=settings,
        now=now,
    )
    existing = _existing_run(db, item.id)

    if existing is not None and existing.status == "SUCCEEDED":
        if (
            readiness.get("input_fingerprint")
            and existing.input_fingerprint == readiness["input_fingerprint"]
            and existing.stage == "PERMITTED"
        ):
            return existing
        raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_INPUT_DRIFT")
    if settings.pinterest_autonomous_execution_enabled is not True:
        raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_DISABLED")
    if readiness.get("ready") is not True:
        raise AutonomousExecutionError(
            (readiness.get("blockers") or ["AUTONOMOUS_EXECUTION_BLOCKED"])[0]
        )
    if not readiness.get("input_fingerprint") or not readiness.get("scheduled_for"):
        raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_IDENTITY_INCOMPLETE")
    attempt_number, supersedes_run_id, reconciliation_id = next_attempt_context(
        db,
        kind="execution",
        latest=existing,
        retry_input_fingerprint=readiness["input_fingerprint"],
    )
    if existing is not None and existing.status == "FAILED" and reconciliation_id is None:
        raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_FAILED_RECONCILIATION_REQUIRED")

    plan = db.get(PinterestPortfolioPlan, item.plan_id)
    optimizer = _optimizer_application(db, item.plan_id)
    if plan is None or optimizer is None:
        raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_IDENTITY_INCOMPLETE")

    run = None if existing is not None and existing.status == "FAILED" else existing
    if run is None:
        run = PinterestAutonomousExecutionRun(
            portfolio_item_id=item.id,
            plan_id=plan.id,
            optimizer_application_id=optimizer.id,
            input_fingerprint=readiness["input_fingerprint"],
            attempt_number=attempt_number,
            supersedes_run_id=supersedes_run_id,
            status="STARTED",
            stage="STARTED",
            scheduled_for=readiness["scheduled_for"],
            safe_metadata={
                "policy_version": EXECUTION_POLICY_VERSION,
                "portfolio_item_fingerprint": item.item_fingerprint,
                "plan_fingerprint": plan.plan_fingerprint,
                "optimizer_fingerprint": optimizer.optimizer_fingerprint,
                "board_record_id": readiness["board_routing"]["selected_board_id"],
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
            actor=EXECUTION_ACTOR,
            action="AUTONOMOUS_EXECUTION_STARTED",
            entity_type="PinterestAutonomousExecutionRun",
            entity_id=run.id,
            metadata_json={
                "portfolio_item_id": item.id,
                "plan_id": plan.id,
                "optimizer_application_id": optimizer.id,
                "input_fingerprint": run.input_fingerprint,
                "scheduled_for": run.scheduled_for.isoformat(),
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
                current
                and current.status == "STARTED"
                and current.input_fingerprint == readiness["input_fingerprint"]
                and current.attempt_number == attempt_number
            ):
                run = current
            else:
                raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_ALREADY_EXISTS") from None
        db.refresh(run)
    else:
        if run.input_fingerprint != readiness["input_fingerprint"]:
            raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_INPUT_DRIFT")
        if run.status != "STARTED":
            raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_STATUS_INVALID")
        if not _same_schedule(run.scheduled_for, readiness["scheduled_for"]):
            raise AutonomousExecutionError("AUTONOMOUS_EXECUTION_SCHEDULE_DRIFT")

    try:
        # SEO_READY
        if _stage_at_least(run, "SEO_READY"):
            if not run.seo_brief_id:
                raise AutonomousExecutionError("SEO_STAGE_DRIFT")
            brief = db.get(PinterestSeoBrief, run.seo_brief_id)
            if brief is None:
                raise AutonomousExecutionError("SEO_STAGE_DRIFT")
            _validate_seo(brief, item.id)
        else:
            brief = persist_seo_brief(db, item.id, settings=settings)
            _validate_seo(brief, item.id)
            run = db.get(PinterestAutonomousExecutionRun, run.id)
            run.seo_brief_id = brief.id
            run.stage = "SEO_READY"
            db.commit()
            db.refresh(run)

        # GENERATED
        if _stage_at_least(run, "GENERATED"):
            if not run.generation_run_id:
                raise AutonomousExecutionError("GENERATION_STAGE_DRIFT")
            generation = db.get(PinterestAutonomousGenerationRun, run.generation_run_id)
            if generation is None:
                raise AutonomousExecutionError("GENERATION_STAGE_DRIFT")
            _validate_generation(generation, item=item, seo_brief_id=run.seo_brief_id)
        else:
            generation = execute_autonomous_generation(
                db,
                item.id,
                settings=settings,
                renderer=renderer,
                now=now,
            )
            _validate_generation(generation, item=item, seo_brief_id=run.seo_brief_id)
            run = db.get(PinterestAutonomousExecutionRun, run.id)
            run.generation_run_id = generation.id
            run.stage = "GENERATED"
            db.commit()
            db.refresh(run)

        # AUTHORIZED
        if _stage_at_least(run, "AUTHORIZED"):
            if not run.approval_id:
                raise AutonomousExecutionError("AUTHORIZATION_STAGE_DRIFT")
            approval = db.get(PinApproval, run.approval_id)
            if approval is None:
                raise AutonomousExecutionError("AUTHORIZATION_STAGE_DRIFT")
            _validate_approval(approval, generation=generation)
        else:
            approval = authorize_draft_autonomously(
                db,
                generation.draft_id,
                settings=settings,
                now=now,
            )
            _validate_approval(approval, generation=generation)
            run = db.get(PinterestAutonomousExecutionRun, run.id)
            run.approval_id = approval.id
            run.stage = "AUTHORIZED"
            db.commit()
            db.refresh(run)

        # PUBLICATION_CREATED
        local_board = db.get(Board, item.local_board_id)
        if local_board is None or not local_board.active or local_board.slug != item.board_key_snapshot:
            raise AutonomousExecutionError("PORTFOLIO_BOARD_DRIFT")
        board_plan = board_strategy(
            db,
            canonical_key=local_board.slug,
            settings=settings,
        )
        if board_plan.get("status") != "ROUTE_EXISTING":
            raise AutonomousExecutionError("ROUTABLE_PINTEREST_BOARD_REQUIRED")
        provider_board = db.get(PinterestBoard, board_plan.get("selected_board_id"))
        connection = (
            db.get(PinterestConnection, provider_board.connection_id)
            if provider_board is not None
            else None
        )
        if (
            provider_board is None
            or not provider_board.is_active
            or not provider_board.is_eligible
            or connection is None
            or connection.status != "CONNECTED"
        ):
            raise AutonomousExecutionError("PINTEREST_DESTINATION_DRIFT")

        if _stage_at_least(run, "PUBLICATION_CREATED"):
            if not run.publication_id:
                raise AutonomousExecutionError("PUBLICATION_STAGE_DRIFT")
            publication = db.get(PinPublication, run.publication_id)
            if publication is None or not _publication_matches(
                publication,
                approval=approval,
                generation=generation,
                board=provider_board,
                connection=connection,
                scheduled_for=run.scheduled_for,
            ):
                raise AutonomousExecutionError("PUBLICATION_STAGE_DRIFT")
        else:
            publication = _recover_publication(
                db,
                approval=approval,
                generation=generation,
                board=provider_board,
                connection=connection,
                scheduled_for=run.scheduled_for,
            )
            if publication is None:
                try:
                    detached = _publication_service(db).create_snapshot(
                        approval_id=approval.id,
                        board_id=item.local_board_id,
                        pinterest_connection_id=connection.id,
                        pinterest_board_record_id=provider_board.id,
                        scheduled_for=run.scheduled_for,
                    )
                    publication_id = detached.id
                except Exception:
                    db.expire_all()
                    recovered = _recover_publication(
                        db,
                        approval=approval,
                        generation=generation,
                        board=provider_board,
                        connection=connection,
                        scheduled_for=run.scheduled_for,
                    )
                    if recovered is None:
                        raise
                    publication_id = recovered.id
                db.expire_all()
                publication = db.get(PinPublication, publication_id)
            if publication is None or not _publication_matches(
                publication,
                approval=approval,
                generation=generation,
                board=provider_board,
                connection=connection,
                scheduled_for=run.scheduled_for,
            ):
                raise AutonomousExecutionError("PUBLICATION_RECOVERY_DRIFT")

            item = db.get(PinterestPortfolioPlanItem, item.id)
            run = db.get(PinterestAutonomousExecutionRun, run.id)
            item.publication_id = publication.id
            item.status = "SCHEDULED"
            run.publication_id = publication.id
            run.stage = "PUBLICATION_CREATED"
            db.commit()
            db.refresh(run)

        # PERMITTED
        if _stage_at_least(run, "PERMITTED"):
            if not run.routine_permit_id:
                raise AutonomousExecutionError("PERMIT_STAGE_DRIFT")
            permit = db.get(RoutineDispatchPermit, run.routine_permit_id)
            if (
                permit is None
                or permit.publication_id != publication.id
                or permit.status != "ACTIVE"
                or permit.authorized_by != AUTONOMOUS_ACTOR
            ):
                raise AutonomousExecutionError("PERMIT_STAGE_DRIFT")
        else:
            permit = auto_permit_publication(
                db,
                publication.id,
                settings=settings,
                now=now,
            )
            if (
                permit.publication_id != publication.id
                or permit.status != "ACTIVE"
                or permit.authorized_by != AUTONOMOUS_ACTOR
            ):
                raise AutonomousExecutionError("AUTONOMOUS_PERMIT_DRIFT")
            run = db.get(PinterestAutonomousExecutionRun, run.id)
            run.routine_permit_id = permit.id
            run.stage = "PERMITTED"
            run.status = "SUCCEEDED"
            run.completed_at = now
            run.safe_metadata = {
                **(run.safe_metadata or {}),
                "seo_brief_id": run.seo_brief_id,
                "generation_run_id": run.generation_run_id,
                "approval_id": run.approval_id,
                "publication_id": run.publication_id,
                "routine_permit_id": permit.id,
            }
            db.add(AuditLog(
                actor=EXECUTION_ACTOR,
                action="AUTONOMOUS_EXECUTION_SUCCEEDED",
                entity_type="PinterestAutonomousExecutionRun",
                entity_id=run.id,
                metadata_json={
                    "portfolio_item_id": item.id,
                    "publication_id": publication.id,
                    "routine_permit_id": permit.id,
                    "scheduled_for": run.scheduled_for.isoformat(),
                },
            ))
            db.commit()
            db.refresh(run)

        return run
    except Exception as exc:
        code = _failure_code(exc)
        _fail_run(db, run, exc, now=now)
        if isinstance(exc, AutonomousExecutionError):
            raise
        raise AutonomousExecutionError(code) from exc
