from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.domain import PinterestPortfolioPlan, PinterestPortfolioPlanItem
from app.services.pinterest_learning_ranking import LearningError, learning_preview

OPTIMIZER_POLICY_VERSION = "PINTEREST_OPTIMIZER_V1"
SCORE_QUANTUM = Decimal("0.000000000001")
DIMENSION_WEIGHTS = {
    "product": Decimal("0.50"),
    "board": Decimal("0.25"),
    "content_angle": Decimal("0.25"),
}


class OptimizerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _q(value: Decimal) -> Decimal:
    return value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rank_percentile(rank: int, count: int) -> Decimal:
    if count <= 0:
        return Decimal("0")
    if count == 1:
        return Decimal("1")
    return _q(Decimal(count - rank) / Decimal(count - 1))


def _ranking_index(rankings: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension in DIMENSION_WEIGHTS:
        rows = rankings.get(dimension) or []
        count = len(rows)
        index: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("entity_key") or "")
            if not key:
                continue
            rank = int(row.get("rank") or 0)
            index[key] = {
                "rank": rank,
                "score": str(row.get("score") or "0.000000000000"),
                "publication_count": int(row.get("publication_count") or 0),
                "impressions": int(row.get("impressions") or 0),
                "percentile": _rank_percentile(rank, count),
            }
        result[dimension] = index
    return result


def _item_evidence(item: PinterestPortfolioPlanItem, index: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    identities = {
        "product": item.product_id,
        "board": item.local_board_id,
        "content_angle": item.content_angle_id,
    }
    evidence: dict[str, Any] = {}
    weighted = Decimal("0")
    total_exposure = 0
    for dimension, weight in DIMENSION_WEIGHTS.items():
        row = index.get(dimension, {}).get(str(identities[dimension]))
        percentile = row["percentile"] if row else Decimal("0")
        publication_count = int(row["publication_count"]) if row else 0
        weighted += percentile * weight
        total_exposure += publication_count
        evidence[dimension] = {
            "entity_key": str(identities[dimension]),
            "weight": f"{weight:.2f}",
            "rank": row["rank"] if row else None,
            "historical_score": row["score"] if row else None,
            "percentile": f"{_q(percentile):.12f}",
            "publication_count": publication_count,
            "impressions": int(row["impressions"]) if row else 0,
        }
    return {
        "evidence_score": _q(weighted),
        "historical_exposure": total_exposure,
        "dimensions": evidence,
    }


def _cap_blockers(items: list[PinterestPortfolioPlanItem], settings: Settings) -> list[str]:
    if not items:
        return []
    blockers: list[str] = []
    product_counts = Counter(item.product_id for item in items)
    board_counts = Counter(item.local_board_id for item in items)
    if product_counts and max(product_counts.values()) > settings.pinterest_portfolio_max_pins_per_product:
        blockers.append("PRODUCT_CAP_EXCEEDED")
    max_board_share = max(Decimal(count) / Decimal(len(items)) for count in board_counts.values())
    if max_board_share > Decimal(str(settings.pinterest_portfolio_max_board_share)):
        blockers.append("BOARD_SHARE_CAP_EXCEEDED")
    identities = [
        (item.product_id, item.local_board_id, item.content_angle_id)
        for item in items
    ]
    if len(identities) != len(set(identities)):
        blockers.append("DUPLICATE_PLAN_ITEM_IDENTITY")
    return blockers


def optimizer_preview(
    db,
    plan_id: str,
    *,
    settings: Settings | None = None,
    as_of_at: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    plan = db.get(PinterestPortfolioPlan, plan_id)
    if plan is None:
        raise OptimizerError("PORTFOLIO_PLAN_NOT_FOUND")

    as_of_at = as_of_at or datetime.now(timezone.utc)
    if as_of_at.tzinfo is None:
        as_of_at = as_of_at.replace(tzinfo=timezone.utc)
    else:
        as_of_at = as_of_at.astimezone(timezone.utc)

    items = list(db.scalars(
        select(PinterestPortfolioPlanItem)
        .where(PinterestPortfolioPlanItem.plan_id == plan.id)
        .order_by(
            PinterestPortfolioPlanItem.planned_date,
            PinterestPortfolioPlanItem.slot_index,
            PinterestPortfolioPlanItem.id,
        )
    ).all())

    optimizable = [
        item for item in items
        if item.status == "PLANNED" and item.publication_id is None
    ]
    frozen = [item for item in items if item not in optimizable]
    blockers = _cap_blockers(items, settings)

    try:
        learning = learning_preview(
            db,
            store_id=plan.store_id,
            as_of_at=as_of_at,
            settings=settings,
        )
    except LearningError as exc:
        raise OptimizerError(exc.code) from None

    index = _ranking_index(learning.get("rankings") or {})
    enriched = []
    for item in optimizable:
        evidence = _item_evidence(item, index)
        enriched.append({
            "item": item,
            **evidence,
        })

    optimizer_ready = bool(learning.get("optimizer_ready"))
    n = len(enriched)
    exploit_count = (
        min(n, int(math.floor(n * float(settings.pinterest_optimizer_exploit_share))))
        if optimizer_ready else 0
    )
    explore_count = n - exploit_count

    exploit_sorted = sorted(
        enriched,
        key=lambda row: (
            -row["evidence_score"],
            row["historical_exposure"],
            row["item"].slot_index,
            row["item"].id,
        ),
    )
    exploit_ids = {row["item"].id for row in exploit_sorted[:exploit_count]}
    explore_sorted = sorted(
        [row for row in enriched if row["item"].id not in exploit_ids],
        key=lambda row: (
            row["historical_exposure"],
            -row["item"].selection_score,
            row["item"].slot_index,
            row["item"].id,
        ),
    )
    exploit_queue = [row for row in exploit_sorted if row["item"].id in exploit_ids]

    slots = sorted(
        optimizable,
        key=lambda item: (
            item.planned_date is None,
            item.planned_date,
            item.slot_index,
            item.id,
        ),
    )

    recommendations = []
    used_exploit = 0
    used_explore = 0
    for position, slot in enumerate(slots, start=1):
        desired_exploit = (
            int(math.floor(position * float(settings.pinterest_optimizer_exploit_share)))
            if optimizer_ready else 0
        )
        choose_exploit = (
            used_exploit < exploit_count
            and desired_exploit > used_exploit
            and bool(exploit_queue)
        )
        if choose_exploit:
            row = exploit_queue.pop(0)
            reason = "EXPLOIT"
            used_exploit += 1
        else:
            if explore_sorted:
                row = explore_sorted.pop(0)
                reason = "EXPLORE"
                used_explore += 1
            elif exploit_queue:
                row = exploit_queue.pop(0)
                reason = "EXPLOIT"
                used_exploit += 1
            else:
                break

        item = row["item"]
        recommendations.append({
            "recommended_position": position,
            "target_slot_index": slot.slot_index,
            "recommended_planned_date": slot.planned_date.isoformat() if slot.planned_date else None,
            "item_id": item.id,
            "original_slot_index": item.slot_index,
            "original_planned_date": item.planned_date.isoformat() if item.planned_date else None,
            "selection_reason": reason,
            "product_id": item.product_id,
            "local_board_id": item.local_board_id,
            "content_angle_id": item.content_angle_id,
            "evidence_score": f"{row['evidence_score']:.12f}",
            "historical_exposure": row["historical_exposure"],
            "evidence": row["dimensions"],
        })

    fingerprint_payload = {
        "policy_version": OPTIMIZER_POLICY_VERSION,
        "plan_id": plan.id,
        "plan_fingerprint": plan.plan_fingerprint,
        "store_id": plan.store_id,
        "learning_fingerprint": learning.get("learning_fingerprint"),
        "optimizer_ready": optimizer_ready,
        "exploit_share": str(settings.pinterest_optimizer_exploit_share),
        "dimension_weights": {k: str(v) for k, v in sorted(DIMENSION_WEIGHTS.items())},
        "recommendations": recommendations,
        "frozen_item_ids": [item.id for item in sorted(frozen, key=lambda x: x.id)],
        "blockers": blockers,
    }

    return {
        "policy_version": OPTIMIZER_POLICY_VERSION,
        "enabled": settings.pinterest_optimizer_enabled,
        "plan_id": plan.id,
        "store_id": plan.store_id,
        "ready": not blockers,
        "blockers": blockers,
        "optimizer_ready": optimizer_ready,
        "learning_fingerprint": learning.get("learning_fingerprint"),
        "learning_publication_count": learning.get("publication_count", 0),
        "frozen_item_count": len(frozen),
        "optimizable_item_count": n,
        "exploit_count": exploit_count,
        "explore_count": explore_count,
        "exploit_share": settings.pinterest_optimizer_exploit_share,
        "dimension_weights": {k: f"{v:.2f}" for k, v in DIMENSION_WEIGHTS.items()},
        "recommendations": recommendations,
        "frozen_items": [
            {
                "item_id": item.id,
                "status": item.status,
                "publication_id": item.publication_id,
                "planned_date": item.planned_date.isoformat() if item.planned_date else None,
            }
            for item in sorted(frozen, key=lambda x: (x.slot_index, x.id))
        ],
        "optimizer_fingerprint": _hash(fingerprint_payload),
        "state_mutated": False,
        "provider_called": False,
    }
