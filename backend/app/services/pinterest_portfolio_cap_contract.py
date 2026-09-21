from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
from typing import Any, Iterable

from app.models.domain import PinterestPortfolioPlan, PinterestPortfolioPlanItem


class PlannerCapContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlannerCapContract:
    max_pins_per_product: int
    max_vendor_share: Decimal
    max_board_share: Decimal
    vendor_limit: int
    board_limit: int
    used: bool
    vendor_cap_relaxed: bool
    board_cap_relaxed: bool
    fingerprint: str


def _fail(code: str) -> None:
    raise PlannerCapContractError(code)


def _strict_bool(value: Any) -> bool:
    if type(value) is not bool:
        _fail("PLANNER_CAP_METADATA_INVALID")
    return value


def _strict_int(value: Any, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        _fail("PLANNER_CAP_METADATA_INVALID")
    return value


def _strict_share(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        _fail("PLANNER_CAP_METADATA_INVALID")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _fail("PLANNER_CAP_METADATA_INVALID")
    if not parsed.is_finite() or parsed <= 0 or parsed > 1:
        _fail("PLANNER_CAP_METADATA_INVALID")
    return parsed


def _hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_limit(target_pins: int, share: Decimal) -> int:
    return max(
        1,
        int(
            (Decimal(target_pins) * share).to_integral_value(
                rounding=ROUND_CEILING
            )
        ),
    )


def _selection_marker(
    item: PinterestPortfolioPlanItem,
) -> tuple[str, bool, bool]:
    metadata = item.selection_metadata
    if not isinstance(metadata, dict):
        _fail("PLANNER_CAP_METADATA_INVALID")

    stage = metadata.get("selection_stage")
    vendor_relaxed = metadata.get("relaxed_vendor_cap")
    board_relaxed = metadata.get("relaxed_board_cap")
    if not isinstance(stage, str):
        _fail("PLANNER_CAP_METADATA_INVALID")
    vendor_relaxed = _strict_bool(vendor_relaxed)
    board_relaxed = _strict_bool(board_relaxed)

    if item.is_reserve:
        if stage != "RESERVE" or vendor_relaxed or board_relaxed:
            _fail("PLANNER_CAP_RELAXATION_MISMATCH")
    else:
        if stage not in {"STRICT", "RELAXED"}:
            _fail("PLANNER_CAP_METADATA_INVALID")
        if stage == "STRICT" and (vendor_relaxed or board_relaxed):
            _fail("PLANNER_CAP_RELAXATION_MISMATCH")
        if stage == "RELAXED" and not (vendor_relaxed or board_relaxed):
            _fail("PLANNER_CAP_RELAXATION_MISMATCH")
    return stage, vendor_relaxed, board_relaxed


def validate_planner_cap_contract(
    plan: PinterestPortfolioPlan,
    items: Iterable[PinterestPortfolioPlanItem],
) -> PlannerCapContract:
    metadata = plan.metadata_json
    if not isinstance(metadata, dict):
        _fail("PLANNER_CAP_METADATA_MISSING")

    policy = metadata.get("cap_policy")
    relaxation = metadata.get("cap_relaxation")
    if policy is None or relaxation is None:
        _fail("PLANNER_CAP_METADATA_MISSING")
    if not isinstance(policy, dict) or not isinstance(relaxation, dict):
        _fail("PLANNER_CAP_METADATA_INVALID")

    max_pins_per_product = _strict_int(policy.get("max_pins_per_product"))
    max_vendor_share = _strict_share(policy.get("max_vendor_share"))
    max_board_share = _strict_share(policy.get("max_board_share"))
    vendor_limit = _strict_int(policy.get("vendor_limit"))
    board_limit = _strict_int(policy.get("board_limit"))

    used = _strict_bool(relaxation.get("used"))
    vendor_cap_relaxed = _strict_bool(
        relaxation.get("vendor_cap_relaxed")
    )
    board_cap_relaxed = _strict_bool(
        relaxation.get("board_cap_relaxed")
    )
    relaxation_vendor_limit = _strict_int(relaxation.get("vendor_limit"))
    relaxation_board_limit = _strict_int(relaxation.get("board_limit"))

    if plan.target_pins is None or type(plan.target_pins) is not int:
        _fail("PLANNER_CAP_POLICY_MISMATCH")
    if plan.target_pins < 1:
        _fail("PLANNER_CAP_POLICY_MISMATCH")

    if vendor_limit != _expected_limit(plan.target_pins, max_vendor_share):
        _fail("PLANNER_CAP_POLICY_MISMATCH")
    if board_limit != _expected_limit(plan.target_pins, max_board_share):
        _fail("PLANNER_CAP_POLICY_MISMATCH")
    if (
        relaxation_vendor_limit != vendor_limit
        or relaxation_board_limit != board_limit
    ):
        _fail("PLANNER_CAP_POLICY_MISMATCH")
    if used != bool(vendor_cap_relaxed or board_cap_relaxed):
        _fail("PLANNER_CAP_RELAXATION_MISMATCH")

    seen_vendor_relaxed = False
    seen_board_relaxed = False
    normalized_markers: list[dict[str, Any]] = []
    ordered_items = sorted(items, key=lambda row: (row.slot_index, row.id))
    for item in ordered_items:
        stage, item_vendor_relaxed, item_board_relaxed = _selection_marker(item)
        seen_vendor_relaxed = seen_vendor_relaxed or item_vendor_relaxed
        seen_board_relaxed = seen_board_relaxed or item_board_relaxed
        normalized_markers.append({
            "item_id": item.id,
            "slot_index": item.slot_index,
            "is_reserve": bool(item.is_reserve),
            "selection_stage": stage,
            "relaxed_vendor_cap": item_vendor_relaxed,
            "relaxed_board_cap": item_board_relaxed,
        })

    if seen_vendor_relaxed != vendor_cap_relaxed:
        _fail("PLANNER_CAP_RELAXATION_MISMATCH")
    if seen_board_relaxed != board_cap_relaxed:
        _fail("PLANNER_CAP_RELAXATION_MISMATCH")

    normalized = {
        "target_pins": plan.target_pins,
        "cap_policy": {
            "max_pins_per_product": max_pins_per_product,
            "max_vendor_share": str(max_vendor_share),
            "max_board_share": str(max_board_share),
            "vendor_limit": vendor_limit,
            "board_limit": board_limit,
        },
        "cap_relaxation": {
            "used": used,
            "vendor_cap_relaxed": vendor_cap_relaxed,
            "board_cap_relaxed": board_cap_relaxed,
            "vendor_limit": vendor_limit,
            "board_limit": board_limit,
        },
        "selection_markers": normalized_markers,
    }
    return PlannerCapContract(
        max_pins_per_product=max_pins_per_product,
        max_vendor_share=max_vendor_share,
        max_board_share=max_board_share,
        vendor_limit=vendor_limit,
        board_limit=board_limit,
        used=used,
        vendor_cap_relaxed=vendor_cap_relaxed,
        board_cap_relaxed=board_cap_relaxed,
        fingerprint=_hash(normalized),
    )


def cap_blockers(
    items: Iterable[PinterestPortfolioPlanItem],
    contract: PlannerCapContract,
) -> list[str]:
    rows = list(items)
    if not rows:
        return []

    blockers: list[str] = []
    product_counts = Counter(row.product_id for row in rows)
    if (
        product_counts
        and max(product_counts.values()) > contract.max_pins_per_product
    ):
        blockers.append("PRODUCT_CAP_EXCEEDED")

    identities = [
        (row.product_id, row.local_board_id, row.content_angle_id)
        for row in rows
    ]
    if len(identities) != len(set(identities)):
        blockers.append("DUPLICATE_PLAN_ITEM_IDENTITY")

    if not contract.board_cap_relaxed:
        active_rows = [row for row in rows if not row.is_reserve]
        board_counts = Counter(row.local_board_id for row in active_rows)
        if board_counts and max(board_counts.values()) > contract.board_limit:
            blockers.append("BOARD_SHARE_CAP_EXCEEDED")

    return blockers
