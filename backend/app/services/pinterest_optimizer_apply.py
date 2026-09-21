from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    PinPublication,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PublicationStatus,
)
from app.services.pinterest_adaptive_optimizer import (
    OPTIMIZER_POLICY_VERSION,
    OptimizerError,
    optimizer_preview,
)
from app.services.pinterest_portfolio_cap_contract import (
    PlannerCapContractError,
    cap_blockers,
    validate_planner_cap_contract,
)

OPTIMIZER_APPLY_ACTOR = "adaptive-optimizer-v1"
OPTIMIZER_METADATA_KEY = "adaptive_optimizer_v1"


class OptimizerApplyError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _items(db, plan_id: str) -> list[PinterestPortfolioPlanItem]:
    return list(db.scalars(
        select(PinterestPortfolioPlanItem)
        .where(PinterestPortfolioPlanItem.plan_id == plan_id)
        .order_by(PinterestPortfolioPlanItem.slot_index, PinterestPortfolioPlanItem.id)
    ).all())


def _item_state(item: PinterestPortfolioPlanItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "slot_index": item.slot_index,
        "is_reserve": bool(item.is_reserve),
        "planned_date": item.planned_date.isoformat() if item.planned_date else None,
        "status": item.status,
        "publication_id": item.publication_id,
        "product_id": item.product_id,
        "local_board_id": item.local_board_id,
        "board_key_snapshot": item.board_key_snapshot,
        "content_angle_id": item.content_angle_id,
        "angle_key_snapshot": item.angle_key_snapshot,
        "seed_keywords": list(item.seed_keywords or []),
        "selection_score": str(item.selection_score),
        "selection_metadata": deepcopy(item.selection_metadata or {}),
        "item_fingerprint": item.item_fingerprint,
    }


def _input_state_fingerprint(
    plan: PinterestPortfolioPlan,
    items: list[PinterestPortfolioPlanItem],
    *,
    planner_cap_contract_fingerprint: str,
) -> str:
    return _hash({
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "plan_status": plan.status,
        "planner_cap_contract_fingerprint": planner_cap_contract_fingerprint,
        "items": [_item_state(item) for item in items],
    })


def _publish_unknown_count(db) -> int:
    return len(list(db.scalars(
        select(PinPublication.id)
        .where(PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN)
    ).all()))


def _existing_application(db, plan_id: str) -> PinterestOptimizerApplication | None:
    return db.scalar(
        select(PinterestOptimizerApplication)
        .where(PinterestOptimizerApplication.plan_id == plan_id)
        .limit(1)
    )


def _frozen_snapshot(items: list[PinterestPortfolioPlanItem]) -> dict[str, dict[str, Any]]:
    return {
        item.id: _item_state(item)
        for item in items
        if not (item.status == "PLANNED" and item.publication_id is None)
    }


def optimizer_apply_readiness(
    db,
    plan_id: str,
    *,
    settings: Settings | None = None,
    as_of_at: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    plan = db.get(PinterestPortfolioPlan, plan_id)
    if plan is None:
        raise OptimizerApplyError("PORTFOLIO_PLAN_NOT_FOUND")

    items = _items(db, plan.id)
    try:
        cap_contract = validate_planner_cap_contract(plan, items)
    except PlannerCapContractError as exc:
        raise OptimizerApplyError(exc.code) from None
    input_state_fingerprint = _input_state_fingerprint(
        plan,
        items,
        planner_cap_contract_fingerprint=cap_contract.fingerprint,
    )
    existing = _existing_application(db, plan.id)

    if existing is not None:
        return {
            "policy_version": "PINTEREST_OPTIMIZER_APPLY_V1",
            "enabled": bool(
                settings.pinterest_portfolio_activation_enabled
                and settings.pinterest_optimizer_apply_enabled
            ),
            "plan_id": plan.id,
            "plan_status": plan.status,
            "ready": plan.status == "ACTIVE",
            "structurally_ready": plan.status == "ACTIVE",
            "already_applied": True,
            "blockers": [] if plan.status == "ACTIVE" else ["APPLIED_PLAN_NOT_ACTIVE"],
            "optimizer_fingerprint": existing.optimizer_fingerprint,
            "input_state_fingerprint": existing.input_state_fingerprint,
            "planner_cap_contract_fingerprint": cap_contract.fingerprint,
            "application_id": existing.id,
            "optimizable_item_count": existing.optimizable_item_count,
            "frozen_item_count": existing.frozen_item_count,
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        }

    try:
        preview = optimizer_preview(
            db,
            plan.id,
            settings=settings,
            as_of_at=as_of_at,
        )
    except OptimizerError as exc:
        raise OptimizerApplyError(exc.code) from None

    optimizable = [
        item for item in items
        if item.status == "PLANNED" and item.publication_id is None
    ]
    frozen_count = len(items) - len(optimizable)

    structural_blockers: list[str] = []
    if plan.status != "DRAFT":
        structural_blockers.append("PLAN_NOT_DRAFT")
    if _publish_unknown_count(db):
        structural_blockers.append("PUBLISH_UNKNOWN_PRESENT")
    if preview.get("ready") is not True:
        structural_blockers.extend(
            code for code in (preview.get("blockers") or [])
            if code not in structural_blockers
        )
    if not optimizable:
        structural_blockers.append("NO_OPTIMIZABLE_ITEMS")

    recommendation_ids = [row.get("item_id") for row in preview.get("recommendations") or []]
    expected_ids = sorted(item.id for item in optimizable)
    if sorted(str(x) for x in recommendation_ids if x) != expected_ids:
        structural_blockers.append("OPTIMIZER_RECOMMENDATION_SET_MISMATCH")
    if len(recommendation_ids) != len(set(recommendation_ids)):
        structural_blockers.append("OPTIMIZER_RECOMMENDATION_DUPLICATE")

    flag_blockers: list[str] = []
    if settings.pinterest_portfolio_activation_enabled is not True:
        flag_blockers.append("PORTFOLIO_ACTIVATION_DISABLED")
    if settings.pinterest_optimizer_apply_enabled is not True:
        flag_blockers.append("OPTIMIZER_APPLY_DISABLED")

    blockers = [*structural_blockers, *flag_blockers]
    return {
        "policy_version": "PINTEREST_OPTIMIZER_APPLY_V1",
        "enabled": not flag_blockers,
        "plan_id": plan.id,
        "plan_status": plan.status,
        "ready": not blockers,
        "structurally_ready": not structural_blockers,
        "already_applied": False,
        "blockers": blockers,
        "optimizer_fingerprint": preview.get("optimizer_fingerprint"),
        "input_state_fingerprint": input_state_fingerprint,
        "planner_cap_contract_fingerprint": cap_contract.fingerprint,
        "learning_fingerprint": preview.get("learning_fingerprint"),
        "optimizer_ready": preview.get("optimizer_ready"),
        "optimizable_item_count": len(optimizable),
        "frozen_item_count": frozen_count,
        "exploit_count": int(preview.get("exploit_count") or 0),
        "explore_count": int(preview.get("explore_count") or 0),
        "recommendations": deepcopy(preview.get("recommendations") or []),
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }


def apply_optimizer(
    db,
    plan_id: str,
    *,
    expected_optimizer_fingerprint: str,
    expected_input_state_fingerprint: str,
    settings: Settings | None = None,
    as_of_at: datetime | None = None,
    now: datetime | None = None,
) -> PinterestOptimizerApplication:
    settings = settings or get_settings()
    plan = db.get(PinterestPortfolioPlan, plan_id)
    if plan is None:
        raise OptimizerApplyError("PORTFOLIO_PLAN_NOT_FOUND")

    current_items = _items(db, plan.id)
    try:
        current_cap_contract = validate_planner_cap_contract(
            plan,
            current_items,
        )
    except PlannerCapContractError as exc:
        raise OptimizerApplyError(exc.code) from None
    current_input_state_fingerprint = _input_state_fingerprint(
        plan,
        current_items,
        planner_cap_contract_fingerprint=current_cap_contract.fingerprint,
    )

    existing = _existing_application(db, plan.id)
    if existing is not None:
        if (
            plan.status == "ACTIVE"
            and existing.optimizer_fingerprint == expected_optimizer_fingerprint
            and existing.input_state_fingerprint == expected_input_state_fingerprint
            and current_input_state_fingerprint == expected_input_state_fingerprint
        ):
            return existing
        raise OptimizerApplyError("OPTIMIZER_APPLICATION_CONFLICT")

    if settings.pinterest_portfolio_activation_enabled is not True:
        raise OptimizerApplyError("PORTFOLIO_ACTIVATION_DISABLED")
    if settings.pinterest_optimizer_apply_enabled is not True:
        raise OptimizerApplyError("OPTIMIZER_APPLY_DISABLED")

    readiness = optimizer_apply_readiness(
        db,
        plan.id,
        settings=settings,
        as_of_at=as_of_at,
    )
    if readiness.get("structurally_ready") is not True:
        raise OptimizerApplyError(
            (readiness.get("blockers") or ["OPTIMIZER_APPLY_BLOCKED"])[0]
        )
    if readiness.get("optimizer_fingerprint") != expected_optimizer_fingerprint:
        raise OptimizerApplyError("OPTIMIZER_FINGERPRINT_MISMATCH")
    if readiness.get("input_state_fingerprint") != expected_input_state_fingerprint:
        raise OptimizerApplyError("OPTIMIZER_INPUT_STATE_DRIFT")

    items = _items(db, plan.id)
    optimizable = [
        item for item in items
        if item.status == "PLANNED" and item.publication_id is None
    ]
    frozen_before = _frozen_snapshot(items)
    active_before = sum(1 for item in optimizable if not item.is_reserve and item.planned_date is not None)
    reserve_before = sum(1 for item in optimizable if item.is_reserve and item.planned_date is None)

    target_slots = {
        item.slot_index: {
            "planned_date": item.planned_date,
            "is_reserve": bool(item.is_reserve),
        }
        for item in optimizable
    }
    by_id = {item.id: item for item in optimizable}
    recommendations = readiness["recommendations"]

    if len(recommendations) != len(optimizable):
        raise OptimizerApplyError("OPTIMIZER_RECOMMENDATION_SET_MISMATCH")

    for row in recommendations:
        item = by_id.get(row.get("item_id"))
        target = target_slots.get(row.get("target_slot_index"))
        if item is None or target is None:
            raise OptimizerApplyError("OPTIMIZER_RECOMMENDATION_SET_MISMATCH")
        expected_date = (
            target["planned_date"].isoformat()
            if target["planned_date"] is not None
            else None
        )
        if row.get("recommended_planned_date") != expected_date:
            raise OptimizerApplyError("OPTIMIZER_TARGET_SLOT_DRIFT")

        item.planned_date = target["planned_date"]
        item.is_reserve = bool(target["is_reserve"])
        metadata = deepcopy(item.selection_metadata or {})
        metadata[OPTIMIZER_METADATA_KEY] = {
            "optimizer_policy_version": OPTIMIZER_POLICY_VERSION,
            "optimizer_fingerprint": expected_optimizer_fingerprint,
            "input_state_fingerprint": expected_input_state_fingerprint,
            "recommended_position": int(row["recommended_position"]),
            "target_slot_index": int(row["target_slot_index"]),
            "target_planned_date": row.get("recommended_planned_date"),
            "selection_reason": row.get("selection_reason"),
            "evidence_score": row.get("evidence_score"),
        }
        item.selection_metadata = metadata

    plan.status = "ACTIVE"
    after_items = _items(db, plan.id)
    active_after = sum(
        1 for item in after_items
        if item.status == "PLANNED"
        and item.publication_id is None
        and not item.is_reserve
        and item.planned_date is not None
    )
    reserve_after = sum(
        1 for item in after_items
        if item.status == "PLANNED"
        and item.publication_id is None
        and item.is_reserve
        and item.planned_date is None
    )
    if active_after != active_before or reserve_after != reserve_before:
        db.rollback()
        raise OptimizerApplyError("OPTIMIZER_SLOT_COUNTS_CHANGED")

    frozen_after = _frozen_snapshot(after_items)
    if frozen_after != frozen_before:
        db.rollback()
        raise OptimizerApplyError("FROZEN_ITEM_MUTATION_DETECTED")

    try:
        after_cap_contract = validate_planner_cap_contract(plan, after_items)
    except PlannerCapContractError as exc:
        db.rollback()
        raise OptimizerApplyError(exc.code) from None
    if after_cap_contract.fingerprint != readiness.get(
        "planner_cap_contract_fingerprint"
    ):
        db.rollback()
        raise OptimizerApplyError("PLANNER_CAP_POLICY_MISMATCH")
    distribution_blockers = cap_blockers(after_items, after_cap_contract)
    if distribution_blockers:
        db.rollback()
        raise OptimizerApplyError(distribution_blockers[0])

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    application = PinterestOptimizerApplication(
        plan_id=plan.id,
        plan_fingerprint_snapshot=plan.plan_fingerprint,
        optimizer_policy_version=OPTIMIZER_POLICY_VERSION,
        optimizer_fingerprint=expected_optimizer_fingerprint,
        learning_fingerprint=readiness.get("learning_fingerprint"),
        input_state_fingerprint=expected_input_state_fingerprint,
        frozen_item_count=int(readiness["frozen_item_count"]),
        optimizable_item_count=int(readiness["optimizable_item_count"]),
        exploit_count=int(readiness["exploit_count"]),
        explore_count=int(readiness["explore_count"]),
        recommendation_snapshot={
            "recommendations": deepcopy(recommendations),
            "planner_cap_contract_fingerprint": (
                readiness.get("planner_cap_contract_fingerprint")
            ),
        },
        status="APPLIED",
        applied_by=OPTIMIZER_APPLY_ACTOR,
        applied_at=now,
    )
    db.add(application)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        current = _existing_application(db, plan.id)
        if (
            current is not None
            and current.optimizer_fingerprint == expected_optimizer_fingerprint
            and current.input_state_fingerprint == expected_input_state_fingerprint
        ):
            return current
        raise OptimizerApplyError("OPTIMIZER_APPLICATION_CONFLICT") from None
    db.refresh(application)
    return application
