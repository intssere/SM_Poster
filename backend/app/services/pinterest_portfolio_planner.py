from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    Board,
    ContentAngle,
    KeywordCluster,
    PinConcept,
    PinDraft,
    PinPublication,
    PinterestPortfolioPlan,
    PinterestPortfolioSlot,
    Product,
    ProductIntelligence,
    PublicationStatus,
    Store,
)
from app.services.content_engine import ProductFacts, propose_content

PORTFOLIO_POLICY_VERSION = "PINTEREST_PORTFOLIO_V1"
COUNTED_PUBLICATION_STATUSES = (
    PublicationStatus.SCHEDULED,
    PublicationStatus.PUBLISHING,
    PublicationStatus.PUBLISHED,
)


class PortfolioPlanningError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    product_id: str
    brand_key: str
    board_id: str
    board_key: str
    angle_id: str
    angle_key: str
    keyword_cluster_id: str | None
    manual_priority: int
    eligibility_score: float
    candidate_fingerprint: str
    reason: str
    keywords: tuple[str, ...]


def _hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _month_bounds(month_key: str, zone: ZoneInfo) -> tuple[datetime, datetime]:
    try:
        year_text, month_text = month_key.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        if len(month_key) != 7 or not 1 <= month <= 12:
            raise ValueError
    except Exception as exc:
        raise PortfolioPlanningError("INVALID_MONTH_KEY") from exc

    start_local = datetime(year, month, 1, tzinfo=zone)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=zone)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=zone)
    return start_local, end_local


def _resolve_store(db, store_id: str | None) -> Store:
    if store_id:
        store = db.get(Store, store_id)
        if store is None:
            raise PortfolioPlanningError("STORE_NOT_FOUND")
        return store

    stores = list(db.scalars(select(Store).order_by(Store.id)).all())
    if not stores:
        raise PortfolioPlanningError("STORE_NOT_FOUND")
    if len(stores) != 1:
        raise PortfolioPlanningError("STORE_ID_REQUIRED")
    return stores[0]


def _facts(product: Product, intelligence: ProductIntelligence, *, now: datetime) -> ProductFacts:
    normalized = intelligence.normalized_data or {}
    category = normalized.get("normalization_category")
    created = product.shopify_created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    is_new = bool(created and (now.astimezone(timezone.utc) - created.astimezone(timezone.utc)).days <= 90)

    return ProductFacts(
        product_id=product.id,
        title=product.title,
        vendor=product.vendor,
        product_type=product.product_type,
        price=Decimal(product.price_min) if product.price_min is not None else None,
        inventory_total=int(product.inventory_total or 0),
        product_url=product.product_url,
        gender=intelligence.audience,
        fragrance_family=intelligence.fragrance_family,
        notes=tuple(str(item) for item in (intelligence.fragrance_notes or [])),
        is_arabian=bool(intelligence.arabian_classification),
        is_niche=bool(intelligence.niche),
        is_designer=bool(intelligence.designer),
        is_gift_set=bool(intelligence.gift_suitability),
        is_home_fragrance=category == "home_fragrance",
        is_new_arrival=is_new,
    )


def _eligible_products(db, store_id: str):
    return list(db.execute(
        select(Product, ProductIntelligence)
        .join(ProductIntelligence, ProductIntelligence.product_id == Product.id)
        .where(
            Product.store_id == store_id,
            Product.status == "ACTIVE",
            Product.excluded_from_editorial.is_(False),
            Product.inventory_total > 0,
            ProductIntelligence.eligibility_status == "ELIGIBLE",
            ProductIntelligence.inventory_eligible.is_(True),
            ProductIntelligence.image_available.is_(True),
            ProductIntelligence.normalization_status != "UNKNOWN",
        )
        .order_by(
            Product.manual_priority.desc(),
            ProductIntelligence.eligibility_score.desc(),
            Product.vendor,
            Product.title,
            Product.id,
        )
    ).all())


def _keyword_cluster_for(proposal_keywords: tuple[str, ...], clusters: list[KeywordCluster]) -> str | None:
    wanted = {str(item).strip().casefold() for item in proposal_keywords if str(item).strip()}
    if not wanted:
        return None
    ranked = []
    for cluster in clusters:
        cluster_keywords = {
            str(item).strip().casefold()
            for item in (cluster.keywords or [])
            if str(item).strip()
        }
        overlap = len(wanted & cluster_keywords)
        if overlap:
            ranked.append((-overlap, cluster.key, cluster.id))
    ranked.sort()
    return ranked[0][2] if ranked else None


def _existing_history(
    db,
    *,
    store_id: str,
    start_utc: datetime,
    end_utc: datetime,
):
    publications = list(db.scalars(
        select(PinPublication)
        .join(PinDraft, PinDraft.id == PinPublication.draft_id)
        .join(PinConcept, PinConcept.id == PinDraft.concept_id)
        .where(
            PinConcept.store_id == store_id,
            PinPublication.scheduled_for.is_not(None),
            PinPublication.scheduled_for >= start_utc,
            PinPublication.scheduled_for < end_utc,
            PinPublication.status.in_(COUNTED_PUBLICATION_STATUSES),
        )
        .order_by(PinPublication.scheduled_for, PinPublication.id)
    ).all())

    product_counts: Counter[str] = Counter()
    brand_counts: Counter[str] = Counter()
    board_counts: Counter[str] = Counter()
    angle_counts: Counter[str] = Counter()
    used_identity: set[tuple[str, str, str]] = set()
    resolved = 0

    for publication in publications:
        draft = db.get(PinDraft, publication.draft_id)
        concept = db.get(PinConcept, draft.concept_id) if draft else None
        if concept is None or concept.store_id != store_id:
            continue
        product = db.get(Product, concept.product_id)
        angle = db.get(ContentAngle, concept.content_angle_id)
        board = db.get(Board, concept.board_id) if concept.board_id else None
        if product is None or angle is None or board is None:
            continue
        brand = (product.vendor or product.title or product.id).strip().casefold()
        product_counts[product.id] += 1
        brand_counts[brand] += 1
        board_counts[board.id] += 1
        angle_counts[angle.id] += 1
        used_identity.add((product.id, board.id, angle.id))
        resolved += 1

    return {
        "publications": publications,
        "existing_count": len(publications),
        "resolved_count": resolved,
        "product_counts": product_counts,
        "brand_counts": brand_counts,
        "board_counts": board_counts,
        "angle_counts": angle_counts,
        "used_identity": used_identity,
    }


def _candidate_pool(
    db,
    *,
    store_id: str,
    month_key: str,
    used_identity: set[tuple[str, str, str]],
    now: datetime,
):
    boards = {
        board.slug: board
        for board in db.scalars(
            select(Board)
            .where(Board.store_id == store_id, Board.active.is_(True))
            .order_by(Board.slug, Board.id)
        ).all()
    }
    angles = {
        angle.key: angle
        for angle in db.scalars(
            select(ContentAngle)
            .where(ContentAngle.active.is_(True))
            .order_by(ContentAngle.key, ContentAngle.id)
        ).all()
    }
    clusters = list(db.scalars(
        select(KeywordCluster).order_by(KeywordCluster.key, KeywordCluster.id)
    ).all())

    missing_boards: Counter[str] = Counter()
    missing_angles: Counter[str] = Counter()
    candidates: list[Candidate] = []
    seen: set[str] = set()

    products = _eligible_products(db, store_id)
    for product, intelligence in products:
        facts = _facts(product, intelligence, now=now)
        brand_key = (intelligence.brand or product.vendor or product.title or product.id).strip().casefold()
        # Portfolio planning needs the full bounded set of deterministic angles;
        # the UI-oriented default limit can otherwise hide valid diversification
        # opportunities later in the proposal order.
        for proposal in propose_content(facts, limit=10):
            board = boards.get(proposal.board_key)
            if board is None:
                missing_boards[proposal.board_key] += 1
                continue
            angle = angles.get(proposal.angle_key)
            if angle is None:
                missing_angles[proposal.angle_key] += 1
                continue
            identity = (product.id, board.id, angle.id)
            if identity in used_identity:
                continue
            candidate_fingerprint = _hash({
                "policy_version": PORTFOLIO_POLICY_VERSION,
                "month_key": month_key,
                "product_id": product.id,
                "board_id": board.id,
                "content_angle_id": angle.id,
            })
            if candidate_fingerprint in seen:
                continue
            seen.add(candidate_fingerprint)
            candidates.append(Candidate(
                product_id=product.id,
                brand_key=brand_key,
                board_id=board.id,
                board_key=board.slug,
                angle_id=angle.id,
                angle_key=angle.key,
                keyword_cluster_id=_keyword_cluster_for(proposal.keywords, clusters),
                manual_priority=int(product.manual_priority or 0),
                eligibility_score=float(intelligence.eligibility_score or 0),
                candidate_fingerprint=candidate_fingerprint,
                reason=proposal.reason,
                keywords=proposal.keywords,
            ))

    candidates.sort(key=lambda item: (
        -item.manual_priority,
        -item.eligibility_score,
        item.brand_key,
        item.board_key,
        item.angle_key,
        item.product_id,
        item.candidate_fingerprint,
    ))
    return {
        "candidates": candidates,
        "eligible_product_count": len(products),
        "missing_boards": {key: missing_boards[key] for key in sorted(missing_boards)},
        "missing_angles": {key: missing_angles[key] for key in sorted(missing_angles)},
    }


def _select_candidates(
    candidates: list[Candidate],
    *,
    needed: int,
    product_cap: int,
    product_counts: Counter[str],
    brand_counts: Counter[str],
    board_counts: Counter[str],
    angle_counts: Counter[str],
) -> list[Candidate]:
    selected: list[Candidate] = []
    remaining = list(candidates)

    while len(selected) < needed:
        available = [
            candidate
            for candidate in remaining
            if product_counts[candidate.product_id] < product_cap
        ]
        if not available:
            break
        available.sort(key=lambda item: (
            product_counts[item.product_id],
            brand_counts[item.brand_key],
            board_counts[item.board_id],
            angle_counts[item.angle_id],
            -item.manual_priority,
            -item.eligibility_score,
            item.brand_key,
            item.board_key,
            item.angle_key,
            item.product_id,
            item.candidate_fingerprint,
        ))
        chosen = available[0]
        selected.append(chosen)
        remaining.remove(chosen)
        product_counts[chosen.product_id] += 1
        brand_counts[chosen.brand_key] += 1
        board_counts[chosen.board_id] += 1
        angle_counts[chosen.angle_id] += 1

    return selected


def _daily_counts(total: int, days: int) -> list[int]:
    if total <= 0:
        return [0] * max(days, 0)
    if days <= 0:
        return []
    return [
        ((index + 1) * total) // days - (index * total) // days
        for index in range(days)
    ]


def _eligible_dates(
    *,
    month_start: datetime,
    month_end: datetime,
    zone: ZoneInfo,
    now: datetime,
    window_end_hour: int,
) -> list[date]:
    first = month_start.date()
    last = (month_end - timedelta(days=1)).date()
    now_local = now.astimezone(zone)
    current_month = (now_local.year, now_local.month)
    target_month = (month_start.year, month_start.month)
    if target_month < current_month:
        return []
    if target_month > current_month:
        start = first
    else:
        start = now_local.date()
        if now_local.hour >= window_end_hour:
            start += timedelta(days=1)
    if start < first:
        start = first
    if start > last:
        return []
    count = (last - start).days + 1
    return [start + timedelta(days=index) for index in range(count)]


def _times_for_day(
    day: date,
    count: int,
    *,
    zone: ZoneInfo,
    now: datetime,
    start_hour: int,
    end_hour: int,
) -> list[datetime]:
    if count <= 0:
        return []
    now_local = now.astimezone(zone)
    start_minutes = start_hour * 60
    if day == now_local.date():
        current_minutes = now_local.hour * 60 + now_local.minute + 1
        start_minutes = max(start_minutes, current_minutes)
    end_minutes = end_hour * 60
    if start_minutes >= end_minutes:
        return []

    span = end_minutes - start_minutes
    values: list[datetime] = []
    for index in range(count):
        offset = ((2 * index + 1) * span) // (2 * count)
        minute_of_day = min(start_minutes + offset, end_minutes - 1)
        local = datetime.combine(
            day,
            time(hour=minute_of_day // 60, minute=minute_of_day % 60),
            tzinfo=zone,
        )
        if local <= now_local:
            local = now_local.replace(second=0, microsecond=0) + timedelta(minutes=1)
        values.append(local.astimezone(timezone.utc))
    return values


def _schedule_times(
    total: int,
    *,
    month_start: datetime,
    month_end: datetime,
    zone: ZoneInfo,
    now: datetime,
    start_hour: int,
    end_hour: int,
) -> tuple[list[datetime], dict[str, int]]:
    dates = _eligible_dates(
        month_start=month_start,
        month_end=month_end,
        zone=zone,
        now=now,
        window_end_hour=end_hour,
    )
    counts = _daily_counts(total, len(dates))
    values: list[datetime] = []
    daily: dict[str, int] = {}
    for day, count in zip(dates, counts):
        times = _times_for_day(
            day,
            count,
            zone=zone,
            now=now,
            start_hour=start_hour,
            end_hour=end_hour,
        )
        if len(times) != count:
            raise PortfolioPlanningError("INSUFFICIENT_FUTURE_DAILY_WINDOW")
        values.extend(times)
        daily[day.isoformat()] = count
    return values, daily


def portfolio_preview(
    db,
    *,
    month_key: str,
    store_id: str | None = None,
    target_count: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    try:
        zone = ZoneInfo(settings.pinterest_portfolio_timezone)
    except ZoneInfoNotFoundError as exc:
        raise PortfolioPlanningError("INVALID_PORTFOLIO_TIMEZONE") from exc
    if settings.pinterest_portfolio_window_start_hour >= settings.pinterest_portfolio_window_end_hour:
        raise PortfolioPlanningError("INVALID_PORTFOLIO_WINDOW")

    store = _resolve_store(db, store_id)
    month_start, month_end = _month_bounds(month_key, zone)
    target = int(
        settings.pinterest_monthly_pin_target
        if target_count is None
        else target_count
    )
    if target <= 0:
        raise PortfolioPlanningError("INVALID_MONTHLY_TARGET")

    if (month_start.year, month_start.month) < (
        now.astimezone(zone).year,
        now.astimezone(zone).month,
    ):
        return {
            "policy_version": PORTFOLIO_POLICY_VERSION,
            "ready": False,
            "blockers": ["PAST_MONTH"],
            "store_id": store.id,
            "month_key": month_key,
            "target_count": target,
            "state_mutated": False,
            "provider_called": False,
        }

    start_utc = month_start.astimezone(timezone.utc)
    end_utc = month_end.astimezone(timezone.utc)
    history = _existing_history(
        db,
        store_id=store.id,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    remaining = max(0, target - history["existing_count"])
    reserve_count = math.ceil(remaining * settings.pinterest_portfolio_reserve_percentage) if remaining else 0

    pool = _candidate_pool(
        db,
        store_id=store.id,
        month_key=month_key,
        used_identity=history["used_identity"],
        now=now,
    )
    needed = remaining + reserve_count
    selected = _select_candidates(
        pool["candidates"],
        needed=needed,
        product_cap=settings.pinterest_product_monthly_cap,
        product_counts=Counter(history["product_counts"]),
        brand_counts=Counter(history["brand_counts"]),
        board_counts=Counter(history["board_counts"]),
        angle_counts=Counter(history["angle_counts"]),
    )

    blockers: list[str] = []
    if len(selected) < needed:
        blockers.append("INSUFFICIENT_DIVERSIFIED_CANDIDATES")

    try:
        schedule_times, daily_counts = _schedule_times(
            remaining,
            month_start=month_start,
            month_end=month_end,
            zone=zone,
            now=now,
            start_hour=settings.pinterest_portfolio_window_start_hour,
            end_hour=settings.pinterest_portfolio_window_end_hour,
        )
    except PortfolioPlanningError as exc:
        blockers.append(str(exc))
        schedule_times, daily_counts = [], {}

    if len(schedule_times) != remaining:
        if remaining and "NO_FUTURE_SLOTS" not in blockers:
            blockers.append("NO_FUTURE_SLOTS")

    if blockers:
        return {
            "policy_version": PORTFOLIO_POLICY_VERSION,
            "ready": False,
            "blockers": blockers,
            "store_id": store.id,
            "month_key": month_key,
            "timezone": str(zone),
            "target_count": target,
            "existing_count": history["existing_count"],
            "remaining_count": remaining,
            "reserve_count": reserve_count,
            "eligible_product_count": pool["eligible_product_count"],
            "candidate_count": len(pool["candidates"]),
            "selected_capacity": len(selected),
            "missing_board_mappings": pool["missing_boards"],
            "missing_angle_mappings": pool["missing_angles"],
            "daily_counts": daily_counts,
            "state_mutated": False,
            "provider_called": False,
        }

    planned = selected[:remaining]
    reserve = selected[remaining:]
    slot_specs = []
    sequence = 1
    for candidate, scheduled_for in zip(planned, schedule_times):
        slot_specs.append({
            "sequence_no": sequence,
            "slot_kind": "PLANNED",
            "scheduled_for": scheduled_for,
            "product_id": candidate.product_id,
            "board_id": candidate.board_id,
            "board_key": candidate.board_key,
            "content_angle_id": candidate.angle_id,
            "angle_key": candidate.angle_key,
            "keyword_cluster_id": candidate.keyword_cluster_id,
            "candidate_fingerprint": candidate.candidate_fingerprint,
            "brand_key": candidate.brand_key,
            "reason": candidate.reason,
            "keywords": list(candidate.keywords),
        })
        sequence += 1
    for candidate in reserve:
        slot_specs.append({
            "sequence_no": sequence,
            "slot_kind": "RESERVE",
            "scheduled_for": None,
            "product_id": candidate.product_id,
            "board_id": candidate.board_id,
            "board_key": candidate.board_key,
            "content_angle_id": candidate.angle_id,
            "angle_key": candidate.angle_key,
            "keyword_cluster_id": candidate.keyword_cluster_id,
            "candidate_fingerprint": candidate.candidate_fingerprint,
            "brand_key": candidate.brand_key,
            "reason": candidate.reason,
            "keywords": list(candidate.keywords),
        })
        sequence += 1

    fingerprint_input = {
        "policy_version": PORTFOLIO_POLICY_VERSION,
        "store_id": store.id,
        "month_key": month_key,
        "timezone": str(zone),
        "target_count": target,
        "existing_publication_ids": [row.id for row in history["publications"]],
        "product_monthly_cap": settings.pinterest_product_monthly_cap,
        "reserve_percentage": settings.pinterest_portfolio_reserve_percentage,
        "window": [
            settings.pinterest_portfolio_window_start_hour,
            settings.pinterest_portfolio_window_end_hour,
        ],
        "slots": [
            {
                "sequence_no": slot["sequence_no"],
                "slot_kind": slot["slot_kind"],
                "scheduled_for": slot["scheduled_for"].isoformat() if slot["scheduled_for"] else None,
                "candidate_fingerprint": slot["candidate_fingerprint"],
            }
            for slot in slot_specs
        ],
    }
    plan_fingerprint = _hash(fingerprint_input)

    for slot in slot_specs:
        slot["slot_fingerprint"] = _hash({
            "plan_fingerprint": plan_fingerprint,
            "sequence_no": slot["sequence_no"],
            "slot_kind": slot["slot_kind"],
            "scheduled_for": slot["scheduled_for"].isoformat() if slot["scheduled_for"] else None,
            "candidate_fingerprint": slot["candidate_fingerprint"],
        })

    return {
        "policy_version": PORTFOLIO_POLICY_VERSION,
        "ready": True,
        "blockers": [],
        "store_id": store.id,
        "month_key": month_key,
        "timezone": str(zone),
        "target_count": target,
        "existing_count": history["existing_count"],
        "existing_resolved_count": history["resolved_count"],
        "remaining_count": remaining,
        "planned_count": len(planned),
        "reserve_count": len(reserve),
        "eligible_product_count": pool["eligible_product_count"],
        "candidate_count": len(pool["candidates"]),
        "missing_board_mappings": pool["missing_boards"],
        "missing_angle_mappings": pool["missing_angles"],
        "daily_counts": daily_counts,
        "slots": slot_specs,
        "plan_fingerprint": plan_fingerprint,
        "state_mutated": False,
        "provider_called": False,
    }


def create_draft_portfolio_plan(
    db,
    *,
    month_key: str,
    store_id: str | None = None,
    target_count: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PinterestPortfolioPlan:
    settings = settings or get_settings()
    if settings.pinterest_portfolio_planning_enabled is not True:
        raise PortfolioPlanningError("PORTFOLIO_PLANNING_DISABLED")

    preview = portfolio_preview(
        db,
        month_key=month_key,
        store_id=store_id,
        target_count=target_count,
        settings=settings,
        now=now,
    )
    if preview.get("ready") is not True:
        raise PortfolioPlanningError((preview.get("blockers") or ["PORTFOLIO_PLAN_BLOCKED"])[0])

    existing = db.scalar(
        select(PinterestPortfolioPlan)
        .where(
            PinterestPortfolioPlan.store_id == preview["store_id"],
            PinterestPortfolioPlan.month_key == month_key,
        )
        .limit(1)
    )
    if existing is not None:
        if existing.plan_fingerprint == preview["plan_fingerprint"]:
            return existing
        raise PortfolioPlanningError("PORTFOLIO_PLAN_MONTH_CONFLICT")

    generated_at = now or datetime.now(timezone.utc)
    plan = PinterestPortfolioPlan(
        store_id=preview["store_id"],
        month_key=month_key,
        timezone=preview["timezone"],
        target_count=preview["target_count"],
        existing_count=preview["existing_count"],
        planned_count=preview["planned_count"],
        reserve_count=preview["reserve_count"],
        policy_version=PORTFOLIO_POLICY_VERSION,
        plan_fingerprint=preview["plan_fingerprint"],
        status="DRAFT",
        summary={
            "remaining_count": preview["remaining_count"],
            "eligible_product_count": preview["eligible_product_count"],
            "candidate_count": preview["candidate_count"],
            "missing_board_mappings": preview["missing_board_mappings"],
            "missing_angle_mappings": preview["missing_angle_mappings"],
            "daily_counts": preview["daily_counts"],
        },
        generated_at=generated_at,
    )
    db.add(plan)
    db.flush()

    for slot in preview["slots"]:
        db.add(PinterestPortfolioSlot(
            plan_id=plan.id,
            sequence_no=slot["sequence_no"],
            slot_kind=slot["slot_kind"],
            scheduled_for=slot["scheduled_for"],
            product_id=slot["product_id"],
            board_id=slot["board_id"],
            content_angle_id=slot["content_angle_id"],
            keyword_cluster_id=slot["keyword_cluster_id"],
            candidate_fingerprint=slot["candidate_fingerprint"],
            slot_fingerprint=slot["slot_fingerprint"],
            brand_key=slot["brand_key"],
            rationale={
                "board_key": slot["board_key"],
                "angle_key": slot["angle_key"],
                "reason": slot["reason"],
                "keywords": slot["keywords"],
            },
        ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(PinterestPortfolioPlan)
            .where(
                PinterestPortfolioPlan.store_id == preview["store_id"],
                PinterestPortfolioPlan.month_key == month_key,
            )
            .limit(1)
        )
        if existing and existing.plan_fingerprint == preview["plan_fingerprint"]:
            return existing
        raise PortfolioPlanningError("PORTFOLIO_PLAN_MONTH_CONFLICT") from None
    except Exception:
        db.rollback()
        raise
    db.refresh(plan)
    return plan
