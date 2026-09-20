from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PinterestAnalyticsSnapshot,
    PinterestLearningSnapshot,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    Product,
    PublicationStatus,
    Store,
)
from app.services.pinterest_performance_analytics import METRIC_POLICY_VERSION

LEARNING_POLICY_VERSION = "PINTEREST_LEARNING_V1"
WINDOW_PRECEDENCE = {"D1": 1, "D7": 7, "D30": 30, "D90": 90}
RATE_QUANTUM = Decimal("0.000000000001")
WEIGHTS = {
    "outbound_click_rate": Decimal("0.40"),
    "save_rate": Decimal("0.25"),
    "pin_click_rate": Decimal("0.20"),
    "engagement_rate": Decimal("0.15"),
}
DIMENSIONS = (
    "product",
    "board",
    "content_angle",
    "template",
    "creative",
    "primary_keyword",
    "intent",
)


class LearningError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _q(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return _q(Decimal("0"))
    return _q(Decimal(numerator) / Decimal(denominator))


def _normalized_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).casefold()
    return normalized or None


def _json_decimal(value: Decimal) -> str:
    return f"{_q(value):.12f}"


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _latest_snapshots(
    db,
    *,
    as_of_at: datetime,
) -> dict[str, PinterestAnalyticsSnapshot]:
    rows = list(db.scalars(
        select(PinterestAnalyticsSnapshot)
        .where(
            PinterestAnalyticsSnapshot.metric_policy_version == METRIC_POLICY_VERSION,
            PinterestAnalyticsSnapshot.finalized_at <= as_of_at,
        )
        .order_by(
            PinterestAnalyticsSnapshot.publication_id,
            PinterestAnalyticsSnapshot.finalized_at,
            PinterestAnalyticsSnapshot.id,
        )
    ).all())
    selected: dict[str, PinterestAnalyticsSnapshot] = {}
    for row in rows:
        if row.observation_window not in WINDOW_PRECEDENCE:
            continue
        current = selected.get(row.publication_id)
        if current is None:
            selected[row.publication_id] = row
            continue
        current_rank = WINDOW_PRECEDENCE[current.observation_window]
        row_rank = WINDOW_PRECEDENCE[row.observation_window]
        if row_rank > current_rank:
            selected[row.publication_id] = row
        elif row_rank == current_rank and (
            _utc(row.finalized_at), row.id
        ) > (
            _utc(current.finalized_at), current.id
        ):
            selected[row.publication_id] = row
    return selected


def _single_portfolio_item(db, publication_id: str) -> PinterestPortfolioPlanItem | None:
    rows = list(db.scalars(
        select(PinterestPortfolioPlanItem)
        .where(PinterestPortfolioPlanItem.publication_id == publication_id)
        .order_by(PinterestPortfolioPlanItem.id)
    ).all())
    return rows[0] if len(rows) == 1 else None


def _lineage(db, publication: PinPublication) -> dict[str, Any]:
    item = _single_portfolio_item(db, publication.id)
    seo = None
    if item is not None:
        seo = db.scalar(
            select(PinterestSeoBrief)
            .where(PinterestSeoBrief.portfolio_item_id == item.id)
            .order_by(PinterestSeoBrief.created_at.desc(), PinterestSeoBrief.id)
            .limit(1)
        )

    draft = db.get(PinDraft, publication.draft_id) if publication.draft_id else None
    concept = db.get(PinConcept, draft.concept_id) if draft else None
    creative = db.get(PinCreative, publication.creative_id) if publication.creative_id else None

    product_id = item.product_id if item else (concept.product_id if concept else None)
    product = db.get(Product, product_id) if product_id else None

    candidate_store_ids = {
        value
        for value in (
            product.store_id if product else None,
            concept.store_id if concept else None,
        )
        if value
    }
    if len(candidate_store_ids) > 1:
        return {"error": "STORE_LINEAGE_MISMATCH"}
    store_id = next(iter(candidate_store_ids), None)
    if store_id is None:
        return {"error": "STORE_UNRESOLVED"}

    board_id = item.local_board_id if item else (
        publication.board_id or (concept.board_id if concept else None)
    )
    angle_id = item.content_angle_id if item else (
        concept.content_angle_id if concept else None
    )
    template_id = publication.template_id or (creative.template_id if creative else None)
    creative_id = publication.creative_id or (creative.id if creative else None)

    primary_keyword = seo.primary_keyword if seo else None
    intent = seo.intent if seo else None

    return {
        "store_id": store_id,
        "product": {
            "key": product_id,
            "label": product.title if product else None,
        } if product_id else None,
        "board": {
            "key": board_id,
            "label": item.board_key_snapshot if item else None,
        } if board_id else None,
        "content_angle": {
            "key": angle_id,
            "label": item.angle_key_snapshot if item else None,
        } if angle_id else None,
        "template": {
            "key": template_id,
            "label": publication.template_key,
        } if template_id else None,
        "creative": {
            "key": creative_id,
            "label": creative.creative_fingerprint if creative else publication.creative_fingerprint,
        } if creative_id else None,
        "primary_keyword": {
            "key": _normalized_text(primary_keyword),
            "label": primary_keyword,
        } if _normalized_text(primary_keyword) else None,
        "intent": {
            "key": _normalized_text(intent),
            "label": intent,
        } if _normalized_text(intent) else None,
    }


def _resolve_store_id(db, records: list[dict[str, Any]], requested: str | None) -> str:
    if requested is not None:
        if db.get(Store, requested) is None:
            raise LearningError("STORE_NOT_FOUND")
        return requested

    stores = sorted({record["store_id"] for record in records if record.get("store_id")})
    if len(stores) == 1:
        return stores[0]
    if len(stores) > 1:
        raise LearningError("STORE_ID_REQUIRED_FOR_MULTI_STORE")

    all_stores = list(db.scalars(select(Store).order_by(Store.id)).all())
    if len(all_stores) == 1:
        return all_stores[0].id
    if not all_stores:
        raise LearningError("STORE_NOT_FOUND")
    raise LearningError("STORE_ID_REQUIRED_FOR_MULTI_STORE")


def _selected_records(
    db,
    *,
    as_of_at: datetime,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    snapshots = _latest_snapshots(db, as_of_at=as_of_at)
    records: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()

    for publication_id in sorted(snapshots):
        snapshot = snapshots[publication_id]
        publication = db.get(PinPublication, publication_id)
        if publication is None:
            excluded["PUBLICATION_NOT_FOUND"] += 1
            continue
        if publication.status != PublicationStatus.PUBLISHED:
            excluded["PUBLICATION_NOT_PUBLISHED"] += 1
            continue

        lineage = _lineage(db, publication)
        if lineage.get("error"):
            excluded[str(lineage["error"])] += 1
            continue

        records.append({
            "publication_id": publication.id,
            "snapshot_id": snapshot.id,
            "provider_payload_fingerprint": snapshot.provider_payload_fingerprint,
            "observation_window": snapshot.observation_window,
            "store_id": lineage["store_id"],
            "impressions": int(snapshot.impressions),
            "saves": int(snapshot.saves),
            "pin_clicks": int(snapshot.pin_clicks),
            "outbound_clicks": int(snapshot.outbound_clicks),
            "engagements": int(snapshot.engagements),
            "dimensions": {
                dimension: lineage.get(dimension)
                for dimension in DIMENSIONS
            },
        })
    return records, excluded


def _pooled_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "impressions": sum(record["impressions"] for record in records),
        "saves": sum(record["saves"] for record in records),
        "pin_clicks": sum(record["pin_clicks"] for record in records),
        "outbound_clicks": sum(record["outbound_clicks"] for record in records),
        "engagements": sum(record["engagements"] for record in records),
    }


def _prior_rates(counts: dict[str, int]) -> dict[str, Decimal]:
    impressions = counts["impressions"]
    return {
        "save_rate": _rate(counts["saves"], impressions),
        "pin_click_rate": _rate(counts["pin_clicks"], impressions),
        "outbound_click_rate": _rate(counts["outbound_clicks"], impressions),
        "engagement_rate": _rate(counts["engagements"], impressions),
    }


def _smoothed_rate(
    *,
    numerator: int,
    impressions: int,
    prior_rate: Decimal,
    prior_impressions: int,
) -> Decimal:
    denominator = Decimal(impressions + prior_impressions)
    if denominator == 0:
        return _q(Decimal("0"))
    numerator_value = Decimal(numerator) + prior_rate * Decimal(prior_impressions)
    return _q(numerator_value / denominator)


def _dimension_rows(
    records: list[dict[str, Any]],
    dimension: str,
    *,
    priors: dict[str, Decimal],
    prior_impressions: int,
    min_dimension_samples: int,
    optimizer_ready: bool,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        identity = record["dimensions"].get(dimension)
        if not identity or not identity.get("key"):
            continue
        key = str(identity["key"])
        group = groups.setdefault(key, {
            "entity_key": key,
            "label": identity.get("label"),
            "publication_ids": set(),
            "impressions": 0,
            "saves": 0,
            "pin_clicks": 0,
            "outbound_clicks": 0,
            "engagements": 0,
            "windows": Counter(),
        })
        group["publication_ids"].add(record["publication_id"])
        group["impressions"] += record["impressions"]
        group["saves"] += record["saves"]
        group["pin_clicks"] += record["pin_clicks"]
        group["outbound_clicks"] += record["outbound_clicks"]
        group["engagements"] += record["engagements"]
        group["windows"][record["observation_window"]] += 1

    rows: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        impressions = group["impressions"]
        raw_rates = {
            "save_rate": _rate(group["saves"], impressions),
            "pin_click_rate": _rate(group["pin_clicks"], impressions),
            "outbound_click_rate": _rate(group["outbound_clicks"], impressions),
            "engagement_rate": _rate(group["engagements"], impressions),
        }
        smoothed_rates = {
            "save_rate": _smoothed_rate(
                numerator=group["saves"],
                impressions=impressions,
                prior_rate=priors["save_rate"],
                prior_impressions=prior_impressions,
            ),
            "pin_click_rate": _smoothed_rate(
                numerator=group["pin_clicks"],
                impressions=impressions,
                prior_rate=priors["pin_click_rate"],
                prior_impressions=prior_impressions,
            ),
            "outbound_click_rate": _smoothed_rate(
                numerator=group["outbound_clicks"],
                impressions=impressions,
                prior_rate=priors["outbound_click_rate"],
                prior_impressions=prior_impressions,
            ),
            "engagement_rate": _smoothed_rate(
                numerator=group["engagements"],
                impressions=impressions,
                prior_rate=priors["engagement_rate"],
                prior_impressions=prior_impressions,
            ),
        }
        score = _q(sum(
            smoothed_rates[metric] * weight
            for metric, weight in WEIGHTS.items()
        ))
        confidence = _q(
            Decimal(impressions) / Decimal(impressions + prior_impressions)
            if impressions + prior_impressions > 0
            else Decimal("0")
        )
        publication_count = len(group["publication_ids"])
        rows.append({
            "entity_key": key,
            "label": group["label"],
            "publication_count": publication_count,
            "impressions": impressions,
            "saves": group["saves"],
            "pin_clicks": group["pin_clicks"],
            "outbound_clicks": group["outbound_clicks"],
            "engagements": group["engagements"],
            "raw_rates": {
                metric: _json_decimal(value)
                for metric, value in raw_rates.items()
            },
            "smoothed_rates": {
                metric: _json_decimal(value)
                for metric, value in smoothed_rates.items()
            },
            "score": _json_decimal(score),
            "confidence": _json_decimal(confidence),
            "window_distribution": {
                window: int(group["windows"].get(window, 0))
                for window in WINDOW_PRECEDENCE
            },
            "exploitation_eligible": bool(
                optimizer_ready and publication_count >= min_dimension_samples
            ),
        })

    rows.sort(
        key=lambda row: (
            -Decimal(row["score"]),
            -row["publication_count"],
            -row["impressions"],
            row["entity_key"],
        )
    )
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def learning_preview(
    db,
    *,
    store_id: str | None = None,
    as_of_at: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    as_of_at = _utc(as_of_at)
    all_records, excluded = _selected_records(db, as_of_at=as_of_at)
    resolved_store_id = _resolve_store_id(db, all_records, store_id)
    records = [
        record for record in all_records
        if record["store_id"] == resolved_store_id
    ]

    counts = _pooled_counts(records)
    priors = _prior_rates(counts)
    publication_count = len({record["publication_id"] for record in records})
    snapshot_count = len(records)
    optimizer_ready = publication_count >= settings.pinterest_learning_min_total_publications

    rankings = {
        dimension: _dimension_rows(
            records,
            dimension,
            priors=priors,
            prior_impressions=settings.pinterest_learning_prior_impressions,
            min_dimension_samples=settings.pinterest_learning_min_dimension_samples,
            optimizer_ready=optimizer_ready,
        )
        for dimension in DIMENSIONS
    }

    evidence = [
        {
            "publication_id": record["publication_id"],
            "snapshot_id": record["snapshot_id"],
            "provider_payload_fingerprint": record["provider_payload_fingerprint"],
            "observation_window": record["observation_window"],
            "store_id": record["store_id"],
            "dimensions": {
                dimension: (
                    record["dimensions"][dimension]["key"]
                    if record["dimensions"].get(dimension)
                    else None
                )
                for dimension in DIMENSIONS
            },
        }
        for record in sorted(records, key=lambda item: item["publication_id"])
    ]
    input_payload = {
        "policy_version": LEARNING_POLICY_VERSION,
        "metric_policy_version": METRIC_POLICY_VERSION,
        "store_id": resolved_store_id,
        "prior_impressions": settings.pinterest_learning_prior_impressions,
        "min_total_publications": settings.pinterest_learning_min_total_publications,
        "min_dimension_samples": settings.pinterest_learning_min_dimension_samples,
        "weights": {key: str(value) for key, value in sorted(WEIGHTS.items())},
        "evidence": evidence,
    }
    input_fingerprint = _hash(input_payload)

    global_priors = {
        "counts": counts,
        "rates": {
            metric: _json_decimal(value)
            for metric, value in priors.items()
        },
        "prior_impressions": settings.pinterest_learning_prior_impressions,
    }
    result_for_hash = {
        "policy_version": LEARNING_POLICY_VERSION,
        "input_fingerprint": input_fingerprint,
        "global_priors": global_priors,
        "rankings": rankings,
        "optimizer_ready": optimizer_ready,
    }
    learning_fingerprint = _hash(result_for_hash)

    return {
        "policy_version": LEARNING_POLICY_VERSION,
        "metric_policy_version": METRIC_POLICY_VERSION,
        "store_id": resolved_store_id,
        "as_of_at": as_of_at.isoformat(),
        "publication_count": publication_count,
        "snapshot_count": snapshot_count,
        "selected_window_distribution": {
            window: sum(
                1 for record in records
                if record["observation_window"] == window
            )
            for window in WINDOW_PRECEDENCE
        },
        "excluded": {
            key: excluded[key]
            for key in sorted(excluded)
        },
        "global_priors": global_priors,
        "rankings": rankings,
        "optimizer_ready": optimizer_ready,
        "thresholds": {
            "min_total_publications": settings.pinterest_learning_min_total_publications,
            "min_dimension_samples": settings.pinterest_learning_min_dimension_samples,
        },
        "input_fingerprint": input_fingerprint,
        "learning_fingerprint": learning_fingerprint,
        "provider_called": False,
        "state_mutated": False,
    }


def persist_learning_snapshot(
    db,
    *,
    store_id: str | None = None,
    as_of_at: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if settings.pinterest_learning_snapshot_persistence_enabled is not True:
        raise LearningError("LEARNING_SNAPSHOT_PERSISTENCE_DISABLED")

    preview = learning_preview(
        db,
        store_id=store_id,
        as_of_at=as_of_at,
        settings=settings,
    )
    existing = db.scalar(
        select(PinterestLearningSnapshot)
        .where(
            PinterestLearningSnapshot.store_id == preview["store_id"],
            PinterestLearningSnapshot.policy_version == LEARNING_POLICY_VERSION,
            PinterestLearningSnapshot.input_fingerprint == preview["input_fingerprint"],
        )
        .limit(1)
    )
    if existing is not None:
        return {
            "status": "IDEMPOTENT",
            "snapshot_id": existing.id,
            "learning_fingerprint": existing.learning_fingerprint,
        }

    row = PinterestLearningSnapshot(
        store_id=preview["store_id"],
        policy_version=LEARNING_POLICY_VERSION,
        as_of_at=_utc(as_of_at),
        input_fingerprint=preview["input_fingerprint"],
        learning_fingerprint=preview["learning_fingerprint"],
        publication_count=preview["publication_count"],
        snapshot_count=preview["snapshot_count"],
        global_priors=preview["global_priors"],
        rankings=preview["rankings"],
        optimizer_ready=preview["optimizer_ready"],
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(PinterestLearningSnapshot)
            .where(
                PinterestLearningSnapshot.store_id == preview["store_id"],
                PinterestLearningSnapshot.policy_version == LEARNING_POLICY_VERSION,
                PinterestLearningSnapshot.input_fingerprint == preview["input_fingerprint"],
            )
            .limit(1)
        )
        if existing is None:
            raise LearningError("LEARNING_SNAPSHOT_PERSISTENCE_CONFLICT") from None
        return {
            "status": "IDEMPOTENT",
            "snapshot_id": existing.id,
            "learning_fingerprint": existing.learning_fingerprint,
        }

    db.commit()
    db.refresh(row)
    return {
        "status": "CREATED",
        "snapshot_id": row.id,
        "learning_fingerprint": row.learning_fingerprint,
    }
