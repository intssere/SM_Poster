from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.models.domain import (
    PinApproval,
    PinPublication,
    PinterestAutonomousDestinationRun,
    PinterestAutonomousExecutionRun,
    PinterestAutonomousGenerationRun,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.pinterest_board_strategy import board_strategy
from app.services.pinterest_optimizer_apply import (
    OptimizerApplyError,
    apply_optimizer,
    optimizer_apply_readiness,
)
from app.services.pinterest_portfolio_planner import (
    PortfolioPlanningError,
    create_draft_portfolio_plan,
    portfolio_preview,
)


class PhaseB1OperatorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_PROVIDER_GATE_FIELDS = (
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
)

_AUTONOMOUS_DOWNSTREAM_FIELDS = (
    "routine_autonomous_authorization_enabled",
    "pinterest_seo_brief_persistence_enabled",
    "pinterest_autonomous_generation_enabled",
    "pinterest_autonomous_execution_enabled",
    "pinterest_autonomous_board_ensure_enabled",
    "pinterest_analytics_ingestion_enabled",
    "pinterest_learning_snapshot_persistence_enabled",
)

_DOWNSTREAM_MODELS = {
    "publications": PinPublication,
    "destination_runs": PinterestAutonomousDestinationRun,
    "execution_runs": PinterestAutonomousExecutionRun,
    "generation_runs": PinterestAutonomousGenerationRun,
    "seo_briefs": PinterestSeoBrief,
    "approvals": PinApproval,
    "routine_permits": RoutineDispatchPermit,
}


def _month_start(month_key: str) -> date:
    try:
        year_text, month_text = month_key.split("-", 1)
        if len(month_key) != 7:
            raise ValueError
        year = int(year_text)
        month = int(month_text)
        if not 1 <= month <= 12:
            raise ValueError
        return date(year, month, 1)
    except Exception as exc:
        raise PhaseB1OperatorError("INVALID_MONTH_KEY") from exc


def _publish_unknown_count(db) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(PinPublication)
            .where(PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN)
        )
        or 0
    )


def _month_plans(db, *, store_id: str, month_start: date) -> list[PinterestPortfolioPlan]:
    return list(
        db.scalars(
            select(PinterestPortfolioPlan)
            .where(
                PinterestPortfolioPlan.store_id == store_id,
                PinterestPortfolioPlan.month_start == month_start,
                PinterestPortfolioPlan.status.in_(("DRAFT", "ACTIVE")),
            )
            .order_by(PinterestPortfolioPlan.created_at, PinterestPortfolioPlan.id)
        ).all()
    )


def _plan_counts(db, plan_id: str) -> dict[str, int]:
    items = list(
        db.scalars(
            select(PinterestPortfolioPlanItem)
            .where(PinterestPortfolioPlanItem.plan_id == plan_id)
            .order_by(PinterestPortfolioPlanItem.slot_index, PinterestPortfolioPlanItem.id)
        ).all()
    )
    return {
        "total": len(items),
        "active": sum(
            1
            for item in items
            if not item.is_reserve and item.planned_date is not None
        ),
        "reserve": sum(
            1
            for item in items
            if item.is_reserve and item.planned_date is None
        ),
    }


def _optimizer_applications(db, plan_id: str) -> list[PinterestOptimizerApplication]:
    return list(
        db.scalars(
            select(PinterestOptimizerApplication)
            .where(PinterestOptimizerApplication.plan_id == plan_id)
            .order_by(PinterestOptimizerApplication.created_at, PinterestOptimizerApplication.id)
        ).all()
    )


def _downstream_counts(db) -> dict[str, int]:
    return {
        key: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for key, model in _DOWNSTREAM_MODELS.items()
    }


def _gate_state(settings: Settings) -> dict[str, bool]:
    names = (
        *_PROVIDER_GATE_FIELDS,
        *_AUTONOMOUS_DOWNSTREAM_FIELDS,
        "routine_pinterest_dry_run",
    )
    return {name: bool(getattr(settings, name)) for name in names}


def _safety_blockers(settings: Settings) -> list[str]:
    blockers = [
        f"{name.upper()}_MUST_BE_FALSE"
        for name in _PROVIDER_GATE_FIELDS
        if bool(getattr(settings, name))
    ]
    blockers.extend(
        f"{name.upper()}_MUST_BE_FALSE"
        for name in _AUTONOMOUS_DOWNSTREAM_FIELDS
        if bool(getattr(settings, name))
    )
    if settings.routine_pinterest_dry_run is not True:
        blockers.append("ROUTINE_PINTEREST_DRY_RUN_REQUIRED")
    return blockers


def _operator_settings(settings: Settings) -> Settings:
    updates = {
        "pinterest_portfolio_planner_enabled": True,
        "pinterest_optimizer_enabled": True,
        "pinterest_portfolio_activation_enabled": True,
        "pinterest_optimizer_apply_enabled": True,
        "routine_pinterest_dry_run": True,
    }
    updates.update({name: False for name in _PROVIDER_GATE_FIELDS})
    updates.update({name: False for name in _AUTONOMOUS_DOWNSTREAM_FIELDS})
    return settings.model_copy(deep=True, update=updates)


def _board_routes(
    db,
    preview: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, dict[str, Any]]:
    keys = sorted(
        {
            str(item.get("board_key_snapshot") or "").strip()
            for item in preview.get("items") or []
            if not bool(item.get("is_reserve"))
            and str(item.get("board_key_snapshot") or "").strip()
        }
    )
    return {
        key: board_strategy(db, canonical_key=key, settings=settings)
        for key in keys
    }


def phase_b1_readiness(
    db,
    *,
    store_id: str,
    month_key: str,
    target_pins: int,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        preview = portfolio_preview(
            db,
            month_key=month_key,
            store_id=store_id,
            target_pins=target_pins,
            settings=settings,
        )
    except PortfolioPlanningError as exc:
        raise PhaseB1OperatorError(str(exc)) from None

    blockers = list(preview.get("blockers") or [])
    publish_unknown_count = _publish_unknown_count(db)
    if publish_unknown_count and "PUBLISH_UNKNOWN_PRESENT" not in blockers:
        blockers.append("PUBLISH_UNKNOWN_PRESENT")

    plans = _month_plans(
        db,
        store_id=store_id,
        month_start=_month_start(month_key),
    )
    existing_plan = plans[0] if len(plans) == 1 else None
    if len(plans) > 1:
        blockers.append("PORTFOLIO_PLAN_MONTH_CONFLICT")
    elif existing_plan is not None:
        if existing_plan.plan_fingerprint != preview.get("preview_fingerprint"):
            blockers.append("PORTFOLIO_PLAN_MONTH_CONFLICT")

    routes = _board_routes(db, preview, settings=settings)
    for key, route in routes.items():
        if route.get("status") != "ROUTE_EXISTING":
            blockers.append(
                f"BOARD_ROUTE_NOT_EXISTING:{key}:{route.get('status') or 'UNKNOWN'}"
            )

    blockers.extend(_safety_blockers(settings))
    blockers = list(dict.fromkeys(blockers))

    return {
        "ready": preview.get("ready") is True and not blockers,
        "blockers": blockers,
        "store_id": store_id,
        "month_key": month_key,
        "target_pins": int(target_pins),
        "publish_unknown_count": publish_unknown_count,
        "existing_commitments": preview.get("existing_commitments"),
        "resolved_existing_commitments": preview.get(
            "resolved_existing_commitments"
        ),
        "planned_active_slots": preview.get("planned_active_slots"),
        "reserve_slots": preview.get("reserve_slots"),
        "candidate_count": preview.get("candidate_count"),
        "cap_policy": preview.get("cap_policy"),
        "cap_relaxation": preview.get("cap_relaxation"),
        "input_fingerprint": preview.get("input_fingerprint"),
        "preview_fingerprint": preview.get("preview_fingerprint"),
        "existing_plan": {
            "id": existing_plan.id if existing_plan else None,
            "status": existing_plan.status if existing_plan else None,
            "plan_fingerprint": (
                existing_plan.plan_fingerprint if existing_plan else None
            ),
        },
        "board_routes": {
            key: {
                "status": value.get("status"),
                "blockers": list(value.get("blockers") or []),
                "selected_board_id": value.get("selected_board_id"),
                "selected_external_board_id": value.get(
                    "selected_external_board_id"
                ),
                "match_basis": value.get("match_basis"),
            }
            for key, value in routes.items()
        },
        "gate_state": _gate_state(settings),
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def _require_exact(expected: Any, actual: Any, code: str) -> None:
    if expected != actual:
        raise PhaseB1OperatorError(code)


def execute_phase_b1(
    db,
    *,
    store_id: str,
    month_key: str,
    target_pins: int,
    expected_preview_fingerprint: str,
    expected_input_fingerprint: str,
    expected_existing_commitments: int,
    expected_active_slots: int,
    expected_reserve_slots: int,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    readiness = phase_b1_readiness(
        db,
        store_id=store_id,
        month_key=month_key,
        target_pins=target_pins,
        settings=settings,
    )
    if readiness.get("ready") is not True:
        raise PhaseB1OperatorError(
            (readiness.get("blockers") or ["PHASE_B1_NOT_READY"])[0]
        )

    _require_exact(
        expected_preview_fingerprint,
        readiness.get("preview_fingerprint"),
        "PREVIEW_FINGERPRINT_MISMATCH",
    )
    _require_exact(
        expected_input_fingerprint,
        readiness.get("input_fingerprint"),
        "INPUT_FINGERPRINT_MISMATCH",
    )
    _require_exact(
        int(expected_existing_commitments),
        readiness.get("existing_commitments"),
        "EXISTING_COMMITMENT_COUNT_MISMATCH",
    )
    _require_exact(
        int(expected_active_slots),
        readiness.get("planned_active_slots"),
        "ACTIVE_SLOT_COUNT_MISMATCH",
    )
    _require_exact(
        int(expected_reserve_slots),
        readiness.get("reserve_slots"),
        "RESERVE_SLOT_COUNT_MISMATCH",
    )

    before_downstream = _downstream_counts(db)
    operator_settings = _operator_settings(settings)

    try:
        plan = create_draft_portfolio_plan(
            db,
            month_key=month_key,
            store_id=store_id,
            target_pins=target_pins,
            settings=operator_settings,
        )
    except PortfolioPlanningError as exc:
        raise PhaseB1OperatorError(str(exc)) from None

    if plan.plan_fingerprint != expected_preview_fingerprint:
        raise PhaseB1OperatorError("PLAN_FINGERPRINT_DRIFT")
    if plan.input_fingerprint != expected_input_fingerprint:
        raise PhaseB1OperatorError("PLAN_INPUT_FINGERPRINT_DRIFT")
    if int(plan.existing_commitments) != int(expected_existing_commitments):
        raise PhaseB1OperatorError("PLAN_EXISTING_COMMITMENT_DRIFT")
    if int(plan.planned_active_slots) != int(expected_active_slots):
        raise PhaseB1OperatorError("PLAN_ACTIVE_SLOT_DRIFT")
    if int(plan.reserve_slots) != int(expected_reserve_slots):
        raise PhaseB1OperatorError("PLAN_RESERVE_SLOT_DRIFT")

    try:
        optimizer_ready = optimizer_apply_readiness(
            db,
            plan.id,
            settings=operator_settings,
        )
    except OptimizerApplyError as exc:
        raise PhaseB1OperatorError(exc.code) from None

    if optimizer_ready.get("structurally_ready") is not True:
        raise PhaseB1OperatorError(
            (optimizer_ready.get("blockers") or ["OPTIMIZER_APPLY_BLOCKED"])[0]
        )
    if optimizer_ready.get("planner_cap_contract_fingerprint") is None:
        raise PhaseB1OperatorError("PLANNER_CAP_CONTRACT_REQUIRED")
    expected_optimizable = int(expected_active_slots) + int(expected_reserve_slots)
    if int(optimizer_ready.get("optimizable_item_count") or 0) != expected_optimizable:
        raise PhaseB1OperatorError("OPTIMIZABLE_ITEM_COUNT_MISMATCH")

    optimizer_fingerprint = optimizer_ready.get("optimizer_fingerprint")
    input_state_fingerprint = optimizer_ready.get("input_state_fingerprint")
    if not optimizer_fingerprint or not input_state_fingerprint:
        raise PhaseB1OperatorError("OPTIMIZER_IDENTITY_INCOMPLETE")

    try:
        application = apply_optimizer(
            db,
            plan.id,
            expected_optimizer_fingerprint=optimizer_fingerprint,
            expected_input_state_fingerprint=input_state_fingerprint,
            settings=operator_settings,
        )
    except OptimizerApplyError as exc:
        raise PhaseB1OperatorError(exc.code) from None

    plan = db.get(PinterestPortfolioPlan, plan.id)
    if plan is None or plan.status != "ACTIVE":
        raise PhaseB1OperatorError("ACTIVE_PLAN_REQUIRED")
    if plan.plan_fingerprint != expected_preview_fingerprint:
        raise PhaseB1OperatorError("ACTIVE_PLAN_FINGERPRINT_DRIFT")

    applications = _optimizer_applications(db, plan.id)
    if len(applications) != 1:
        raise PhaseB1OperatorError("EXACTLY_ONE_OPTIMIZER_APPLICATION_REQUIRED")
    stored_application = applications[0]
    if stored_application.id != application.id or stored_application.status != "APPLIED":
        raise PhaseB1OperatorError("OPTIMIZER_APPLICATION_DRIFT")
    if (
        stored_application.optimizer_fingerprint != optimizer_fingerprint
        or stored_application.input_state_fingerprint != input_state_fingerprint
    ):
        raise PhaseB1OperatorError("OPTIMIZER_APPLICATION_IDENTITY_DRIFT")

    counts = _plan_counts(db, plan.id)
    if counts["active"] != int(expected_active_slots):
        raise PhaseB1OperatorError("POST_APPLY_ACTIVE_SLOT_COUNT_MISMATCH")
    if counts["reserve"] != int(expected_reserve_slots):
        raise PhaseB1OperatorError("POST_APPLY_RESERVE_SLOT_COUNT_MISMATCH")
    if counts["total"] != expected_optimizable:
        raise PhaseB1OperatorError("POST_APPLY_TOTAL_ITEM_COUNT_MISMATCH")

    after_downstream = _downstream_counts(db)
    if after_downstream != before_downstream:
        raise PhaseB1OperatorError("DOWNSTREAM_STATE_CHANGED")

    return {
        "status": "SUCCEEDED",
        "store_id": store_id,
        "month_key": month_key,
        "target_pins": int(target_pins),
        "preview_fingerprint": expected_preview_fingerprint,
        "input_fingerprint": expected_input_fingerprint,
        "plan_id": plan.id,
        "plan_status": plan.status,
        "plan_fingerprint": plan.plan_fingerprint,
        "optimizer_application_id": stored_application.id,
        "optimizer_status": stored_application.status,
        "optimizer_fingerprint": stored_application.optimizer_fingerprint,
        "input_state_fingerprint": stored_application.input_state_fingerprint,
        "planner_cap_contract_fingerprint": optimizer_ready.get(
            "planner_cap_contract_fingerprint"
        ),
        "existing_commitments": int(plan.existing_commitments),
        "active_slots": counts["active"],
        "reserve_slots": counts["reserve"],
        "downstream_counts_before": before_downstream,
        "downstream_counts_after": after_downstream,
        "state_mutated": True,
        "provider_called": False,
        "ai_called": False,
    }
