from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    Board,
    ContentAngle,
    PinConcept,
    PinDraft,
    PinPublication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    Product,
    ProductIntelligence,
    PublicationStatus,
    Store,
)
from app.services.content_engine import ProductFacts, propose_content


PORTFOLIO_POLICY_VERSION = "PINTEREST_PORTFOLIO_V2"
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
    vendor_key: str
    local_board_id: str
    board_key: str
    content_angle_id: str
    angle_key: str
    seed_keywords: tuple[str, ...]
    manual_priority: int
    eligibility_score: Decimal
    candidate_fingerprint: str


@dataclass(frozen=True)
class SelectedCandidate:
    candidate: Candidate
    selection_stage: str
    relaxed_vendor_cap: bool = False
    relaxed_board_cap: bool = False


def _hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_month(month_key: str) -> tuple[date, date, date]:
    try:
        year_text, month_text = month_key.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        if len(month_key) != 7 or not 1 <= month <= 12:
            raise ValueError
    except Exception as exc:
        raise PortfolioPlanningError("INVALID_MONTH_KEY") from exc

    month_start = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return month_start, next_month - timedelta(days=1), next_month


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
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        is_new = (now.astimezone(timezone.utc) - created.astimezone(timezone.utc)).days <= 90
    else:
        is_new = False

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
            Product.id,
        )
    ).all())


def _publish_unknown_count(db) -> int:
    return len(list(db.scalars(
        select(PinPublication.id)
        .where(PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN)
    ).all()))


def _existing_history(
    db,
    *,
    store_id: str,
    month_start: date,
    next_month: date,
) -> dict:
    start_utc = datetime.combine(month_start, time.min, tzinfo=timezone.utc)
    end_utc = datetime.combine(next_month, time.min, tzinfo=timezone.utc)
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
    vendor_counts: Counter[str] = Counter()
    board_counts: Counter[str] = Counter()
    angle_counts: Counter[str] = Counter()
    used_identity: set[tuple[str, str, str]] = set()
    resolved_count = 0

    for publication in publications:
        draft = db.get(PinDraft, publication.draft_id)
        concept = db.get(PinConcept, draft.concept_id) if draft else None
        if concept is None or concept.store_id != store_id:
            continue
        product = db.get(Product, concept.product_id)
        board = db.get(Board, concept.board_id) if concept.board_id else None
        angle = db.get(ContentAngle, concept.content_angle_id)
        if product is None or board is None or angle is None:
            continue
        vendor_key = (product.vendor or product.title or product.id).strip().casefold()
        product_counts[product.id] += 1
        vendor_counts[vendor_key] += 1
        board_counts[board.id] += 1
        angle_counts[angle.id] += 1
        used_identity.add((product.id, board.id, angle.id))
        resolved_count += 1

    return {
        "publications": publications,
        "existing_commitments": len(publications),
        "resolved_commitments": resolved_count,
        "product_counts": product_counts,
        "vendor_counts": vendor_counts,
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
) -> dict:
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

    missing_boards: Counter[str] = Counter()
    missing_angles: Counter[str] = Counter()
    candidates: list[Candidate] = []
    seen_candidate_fingerprints: set[str] = set()
    products = _eligible_products(db, store_id)

    for product, intelligence in products:
        facts = _facts(product, intelligence, now=now)
        vendor_key = (
            intelligence.brand or product.vendor or product.title or product.id
        ).strip().casefold()
        product_missing_boards: set[str] = set()
        product_missing_angles: set[str] = set()

        for proposal in propose_content(facts, limit=20):
            board = boards.get(proposal.board_key)
            if board is None:
                product_missing_boards.add(proposal.board_key)
                continue
            angle = angles.get(proposal.angle_key)
            if angle is None:
                product_missing_angles.add(proposal.angle_key)
                continue

            identity = (product.id, board.id, angle.id)
            if identity in used_identity:
                continue

            fingerprint = _hash({
                "policy_version": PORTFOLIO_POLICY_VERSION,
                "month_key": month_key,
                "product_id": product.id,
                "local_board_id": board.id,
                "content_angle_id": angle.id,
            })
            if fingerprint in seen_candidate_fingerprints:
                continue
            seen_candidate_fingerprints.add(fingerprint)
            candidates.append(Candidate(
                product_id=product.id,
                vendor_key=vendor_key,
                local_board_id=board.id,
                board_key=board.slug,
                content_angle_id=angle.id,
                angle_key=angle.key,
                seed_keywords=tuple(str(item) for item in proposal.keywords),
                manual_priority=int(product.manual_priority or 0),
                eligibility_score=Decimal(str(intelligence.eligibility_score or 0)),
                candidate_fingerprint=fingerprint,
            ))

        for key in product_missing_boards:
            missing_boards[key] += 1
        for key in product_missing_angles:
            missing_angles[key] += 1

    candidates.sort(key=lambda candidate: (
        -candidate.manual_priority,
        -candidate.eligibility_score,
        candidate.product_id,
        candidate.board_key,
        candidate.angle_key,
        candidate.candidate_fingerprint,
    ))
    return {
        "candidates": candidates,
        "eligible_product_count": len(products),
        "missing_board_mappings": {
            key: missing_boards[key] for key in sorted(missing_boards)
        },
        "missing_angle_mappings": {
            key: missing_angles[key] for key in sorted(missing_angles)
        },
    }


def _candidate_rank(
    candidate: Candidate,
    *,
    product_counts: Counter[str],
    vendor_counts: Counter[str],
    board_counts: Counter[str],
    angle_counts: Counter[str],
):
    return (
        product_counts[candidate.product_id],
        vendor_counts[candidate.vendor_key],
        board_counts[candidate.local_board_id],
        angle_counts[candidate.content_angle_id],
        -candidate.manual_priority,
        -candidate.eligibility_score,
        candidate.product_id,
        candidate.board_key,
        candidate.angle_key,
        candidate.candidate_fingerprint,
    )


def _select_active(
    candidates: list[Candidate],
    *,
    needed: int,
    product_cap: int,
    vendor_limit: int,
    board_limit: int,
    product_counts: Counter[str],
    vendor_counts: Counter[str],
    board_counts: Counter[str],
    angle_counts: Counter[str],
) -> tuple[list[SelectedCandidate], list[Candidate], dict]:
    selected: list[SelectedCandidate] = []
    remaining = list(candidates)
    relaxed_vendor = False
    relaxed_board = False

    def choose(allow_soft_relaxation: bool) -> bool:
        nonlocal relaxed_vendor, relaxed_board
        available = []
        for candidate in remaining:
            if product_counts[candidate.product_id] >= product_cap:
                continue
            vendor_over = vendor_counts[candidate.vendor_key] >= vendor_limit
            board_over = board_counts[candidate.local_board_id] >= board_limit
            if not allow_soft_relaxation and (vendor_over or board_over):
                continue
            available.append((candidate, vendor_over, board_over))

        if not available:
            return False

        available.sort(key=lambda row: _candidate_rank(
            row[0],
            product_counts=product_counts,
            vendor_counts=vendor_counts,
            board_counts=board_counts,
            angle_counts=angle_counts,
        ))
        candidate, vendor_over, board_over = available[0]
        remaining.remove(candidate)
        selected.append(SelectedCandidate(
            candidate=candidate,
            selection_stage="RELAXED" if allow_soft_relaxation else "STRICT",
            relaxed_vendor_cap=bool(allow_soft_relaxation and vendor_over),
            relaxed_board_cap=bool(allow_soft_relaxation and board_over),
        ))
        if allow_soft_relaxation:
            relaxed_vendor = relaxed_vendor or vendor_over
            relaxed_board = relaxed_board or board_over

        product_counts[candidate.product_id] += 1
        vendor_counts[candidate.vendor_key] += 1
        board_counts[candidate.local_board_id] += 1
        angle_counts[candidate.content_angle_id] += 1
        return True

    while len(selected) < needed and choose(False):
        pass
    while len(selected) < needed and choose(True):
        pass

    return selected, remaining, {
        "used": relaxed_vendor or relaxed_board,
        "vendor_cap_relaxed": relaxed_vendor,
        "board_cap_relaxed": relaxed_board,
        "vendor_limit": vendor_limit,
        "board_limit": board_limit,
    }


def _select_reserve(
    candidates: list[Candidate],
    *,
    needed: int,
    product_cap: int,
    product_counts: Counter[str],
    vendor_counts: Counter[str],
    board_counts: Counter[str],
    angle_counts: Counter[str],
) -> list[SelectedCandidate]:
    selected: list[SelectedCandidate] = []
    remaining = list(candidates)
    while len(selected) < needed:
        available = [
            candidate for candidate in remaining
            if product_counts[candidate.product_id] < product_cap
        ]
        if not available:
            break
        available.sort(key=lambda candidate: _candidate_rank(
            candidate,
            product_counts=product_counts,
            vendor_counts=vendor_counts,
            board_counts=board_counts,
            angle_counts=angle_counts,
        ))
        candidate = available[0]
        remaining.remove(candidate)
        selected.append(SelectedCandidate(
            candidate=candidate,
            selection_stage="RESERVE",
        ))
        product_counts[candidate.product_id] += 1
        vendor_counts[candidate.vendor_key] += 1
        board_counts[candidate.local_board_id] += 1
        angle_counts[candidate.content_angle_id] += 1
    return selected


def _eligible_dates(month_start: date, month_end: date, *, today: date) -> list[date]:
    if month_end < today:
        return []
    start = max(month_start, today)
    return [
        start + timedelta(days=index)
        for index in range((month_end - start).days + 1)
    ]


def _daily_pacing(total: int, days: list[date]) -> tuple[list[date], dict[str, int]]:
    if total <= 0:
        return [], {day.isoformat(): 0 for day in days}
    if not days:
        return [], {}

    counts = [
        ((index + 1) * total) // len(days) - (index * total) // len(days)
        for index in range(len(days))
    ]
    planned_dates: list[date] = []
    pacing: dict[str, int] = {}
    for day, count in zip(days, counts):
        pacing[day.isoformat()] = count
        planned_dates.extend([day] * count)
    return planned_dates, pacing


def _selection_score(candidate: Candidate) -> Decimal:
    return (
        Decimal(candidate.manual_priority) * Decimal("100000")
        + candidate.eligibility_score
    ).quantize(Decimal("0.000001"))


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def portfolio_preview(
    db,
    *,
    month_key: str,
    store_id: str | None = None,
    target_pins: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    month_start, month_end, next_month = _parse_month(month_key)
    today = now.astimezone(timezone.utc).date()
    store = _resolve_store(db, store_id)
    target = int(
        settings.pinterest_monthly_pin_target
        if target_pins is None
        else target_pins
    )
    if not 1 <= target <= 10000:
        raise PortfolioPlanningError("INVALID_MONTHLY_TARGET")

    if month_end < today:
        return {
            "policy_version": PORTFOLIO_POLICY_VERSION,
            "ready": False,
            "blockers": ["PAST_MONTH"],
            "store_id": store.id,
            "month_key": month_key,
            "target_pins": target,
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        }

    publish_unknown_count = _publish_unknown_count(db)
    if publish_unknown_count:
        return {
            "policy_version": PORTFOLIO_POLICY_VERSION,
            "ready": False,
            "blockers": ["PUBLISH_UNKNOWN_PRESENT"],
            "store_id": store.id,
            "month_key": month_key,
            "target_pins": target,
            "publish_unknown_count": publish_unknown_count,
            "state_mutated": False,
            "provider_called": False,
            "ai_called": False,
        }

    history = _existing_history(
        db,
        store_id=store.id,
        month_start=month_start,
        next_month=next_month,
    )
    remaining_active = max(0, target - history["existing_commitments"])
    reserve_target = (
        math.ceil(remaining_active * settings.pinterest_portfolio_reserve_percentage)
        if remaining_active
        else 0
    )

    pool = _candidate_pool(
        db,
        store_id=store.id,
        month_key=month_key,
        used_identity=history["used_identity"],
        now=now,
    )

    product_counts = Counter(history["product_counts"])
    vendor_counts = Counter(history["vendor_counts"])
    board_counts = Counter(history["board_counts"])
    angle_counts = Counter(history["angle_counts"])

    vendor_limit = max(
        1,
        math.ceil(target * settings.pinterest_portfolio_max_vendor_share),
    )
    board_limit = max(
        1,
        math.ceil(target * settings.pinterest_portfolio_max_board_share),
    )

    active, remaining_candidates, relaxation = _select_active(
        pool["candidates"],
        needed=remaining_active,
        product_cap=settings.pinterest_portfolio_max_pins_per_product,
        vendor_limit=vendor_limit,
        board_limit=board_limit,
        product_counts=product_counts,
        vendor_counts=vendor_counts,
        board_counts=board_counts,
        angle_counts=angle_counts,
    )

    blockers: list[str] = []
    if len(active) < remaining_active:
        blockers.append("INSUFFICIENT_ACTIVE_CAPACITY")

    reserve = _select_reserve(
        remaining_candidates,
        needed=reserve_target,
        product_cap=settings.pinterest_portfolio_max_pins_per_product,
        product_counts=product_counts,
        vendor_counts=vendor_counts,
        board_counts=board_counts,
        angle_counts=angle_counts,
    )
    if len(reserve) < reserve_target:
        blockers.append("INSUFFICIENT_RESERVE_CAPACITY")

    eligible_dates = _eligible_dates(month_start, month_end, today=today)
    planned_dates, daily_pacing = _daily_pacing(remaining_active, eligible_dates)
    if remaining_active and len(planned_dates) != remaining_active:
        blockers.append("NO_FUTURE_DATES")

    input_payload = {
        "policy_version": PORTFOLIO_POLICY_VERSION,
        "store_id": store.id,
        "month_key": month_key,
        "target_pins": target,
        "max_pins_per_product": settings.pinterest_portfolio_max_pins_per_product,
        "max_vendor_share": settings.pinterest_portfolio_max_vendor_share,
        "max_board_share": settings.pinterest_portfolio_max_board_share,
        "reserve_percentage": settings.pinterest_portfolio_reserve_percentage,
        "existing_publications": [
            {
                "id": row.id,
                "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                "scheduled_for": row.scheduled_for.isoformat() if row.scheduled_for else None,
            }
            for row in history["publications"]
        ],
        "candidate_fingerprints": [
            candidate.candidate_fingerprint for candidate in pool["candidates"]
        ],
        "taxonomy_gaps": {
            "boards": pool["missing_board_mappings"],
            "angles": pool["missing_angle_mappings"],
        },
    }
    input_fingerprint = _hash(input_payload)

    item_specs: list[dict] = []
    for index, (selected, planned_date) in enumerate(
        zip(active, planned_dates),
        start=1,
    ):
        candidate = selected.candidate
        item_specs.append({
            "slot_index": index,
            "is_reserve": False,
            "planned_date": planned_date,
            "product_id": candidate.product_id,
            "local_board_id": candidate.local_board_id,
            "board_key_snapshot": candidate.board_key,
            "content_angle_id": candidate.content_angle_id,
            "angle_key_snapshot": candidate.angle_key,
            "seed_keywords": list(candidate.seed_keywords),
            "selection_score": str(_selection_score(candidate)),
            "selection_metadata": {
                "candidate_fingerprint": candidate.candidate_fingerprint,
                "vendor_key": candidate.vendor_key,
                "selection_stage": selected.selection_stage,
                "relaxed_vendor_cap": selected.relaxed_vendor_cap,
                "relaxed_board_cap": selected.relaxed_board_cap,
            },
        })

    next_index = len(item_specs) + 1
    for offset, selected in enumerate(reserve):
        candidate = selected.candidate
        item_specs.append({
            "slot_index": next_index + offset,
            "is_reserve": True,
            "planned_date": None,
            "product_id": candidate.product_id,
            "local_board_id": candidate.local_board_id,
            "board_key_snapshot": candidate.board_key,
            "content_angle_id": candidate.content_angle_id,
            "angle_key_snapshot": candidate.angle_key,
            "seed_keywords": list(candidate.seed_keywords),
            "selection_score": str(_selection_score(candidate)),
            "selection_metadata": {
                "candidate_fingerprint": candidate.candidate_fingerprint,
                "vendor_key": candidate.vendor_key,
                "selection_stage": "RESERVE",
                "relaxed_vendor_cap": False,
                "relaxed_board_cap": False,
            },
        })

    plan_payload = {
        "input_fingerprint": input_fingerprint,
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "items": [
            {
                "slot_index": item["slot_index"],
                "is_reserve": item["is_reserve"],
                "planned_date": item["planned_date"].isoformat() if item["planned_date"] else None,
                "product_id": item["product_id"],
                "local_board_id": item["local_board_id"],
                "content_angle_id": item["content_angle_id"],
                "candidate_fingerprint": item["selection_metadata"]["candidate_fingerprint"],
            }
            for item in item_specs
        ],
        "cap_relaxation": relaxation,
    }
    plan_fingerprint = _hash(plan_payload)

    for item in item_specs:
        item["item_fingerprint"] = _hash({
            "plan_fingerprint": plan_fingerprint,
            "slot_index": item["slot_index"],
            "is_reserve": item["is_reserve"],
            "planned_date": item["planned_date"].isoformat() if item["planned_date"] else None,
            "candidate_fingerprint": item["selection_metadata"]["candidate_fingerprint"],
        })

    response = {
        "policy_version": PORTFOLIO_POLICY_VERSION,
        "ready": not blockers,
        "blockers": blockers,
        "store_id": store.id,
        "month_key": month_key,
        "month_start": month_start,
        "month_end": month_end,
        "target_pins": target,
        "existing_commitments": history["existing_commitments"],
        "resolved_existing_commitments": history["resolved_commitments"],
        "remaining_active_slots": remaining_active,
        "planned_active_slots": len(active),
        "reserve_target": reserve_target,
        "reserve_slots": len(reserve),
        "eligible_product_count": pool["eligible_product_count"],
        "candidate_count": len(pool["candidates"]),
        "taxonomy_gaps": {
            "boards": pool["missing_board_mappings"],
            "angles": pool["missing_angle_mappings"],
        },
        "cap_policy": {
            "max_pins_per_product": settings.pinterest_portfolio_max_pins_per_product,
            "max_vendor_share": settings.pinterest_portfolio_max_vendor_share,
            "max_board_share": settings.pinterest_portfolio_max_board_share,
            "vendor_limit": vendor_limit,
            "board_limit": board_limit,
        },
        "cap_relaxation": relaxation,
        "daily_pacing": daily_pacing,
        "distributions": {
            "product": _counter_dict(product_counts),
            "vendor": _counter_dict(vendor_counts),
            "board": _counter_dict(board_counts),
            "angle": _counter_dict(angle_counts),
        },
        "items": item_specs,
        "input_fingerprint": input_fingerprint,
        "preview_fingerprint": plan_fingerprint,
        "state_mutated": False,
        "provider_called": False,
        "ai_called": False,
    }
    return response


def create_draft_portfolio_plan(
    db,
    *,
    month_key: str,
    store_id: str | None = None,
    target_pins: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PinterestPortfolioPlan:
    settings = settings or get_settings()
    if settings.pinterest_portfolio_planner_enabled is not True:
        raise PortfolioPlanningError("PORTFOLIO_PLANNER_DISABLED")

    preview = portfolio_preview(
        db,
        month_key=month_key,
        store_id=store_id,
        target_pins=target_pins,
        settings=settings,
        now=now,
    )
    if preview.get("ready") is not True:
        raise PortfolioPlanningError(
            (preview.get("blockers") or ["PORTFOLIO_PLAN_BLOCKED"])[0]
        )

    same_fingerprint = db.scalar(
        select(PinterestPortfolioPlan)
        .where(PinterestPortfolioPlan.plan_fingerprint == preview["preview_fingerprint"])
        .order_by(PinterestPortfolioPlan.created_at, PinterestPortfolioPlan.id)
        .limit(1)
    )
    if same_fingerprint is not None:
        return same_fingerprint

    existing = db.scalar(
        select(PinterestPortfolioPlan)
        .where(
            PinterestPortfolioPlan.store_id == preview["store_id"],
            PinterestPortfolioPlan.month_start == preview["month_start"],
            PinterestPortfolioPlan.status.in_(("DRAFT", "ACTIVE")),
        )
        .order_by(PinterestPortfolioPlan.created_at, PinterestPortfolioPlan.id)
        .limit(1)
    )
    if existing is not None:
        raise PortfolioPlanningError("PORTFOLIO_PLAN_MONTH_CONFLICT")

    plan = PinterestPortfolioPlan(
        store_id=preview["store_id"],
        month_start=preview["month_start"],
        month_end=preview["month_end"],
        target_pins=preview["target_pins"],
        existing_commitments=preview["existing_commitments"],
        planned_active_slots=preview["planned_active_slots"],
        reserve_slots=preview["reserve_slots"],
        policy_version=PORTFOLIO_POLICY_VERSION,
        input_fingerprint=preview["input_fingerprint"],
        plan_fingerprint=preview["preview_fingerprint"],
        status="DRAFT",
        metadata_json={
            "resolved_existing_commitments": preview["resolved_existing_commitments"],
            "eligible_product_count": preview["eligible_product_count"],
            "candidate_count": preview["candidate_count"],
            "taxonomy_gaps": preview["taxonomy_gaps"],
            "cap_policy": preview["cap_policy"],
            "cap_relaxation": preview["cap_relaxation"],
            "daily_pacing": preview["daily_pacing"],
            "distributions": preview["distributions"],
        },
    )
    db.add(plan)
    db.flush()

    for item in preview["items"]:
        db.add(PinterestPortfolioPlanItem(
            plan_id=plan.id,
            slot_index=item["slot_index"],
            is_reserve=item["is_reserve"],
            planned_date=item["planned_date"],
            product_id=item["product_id"],
            local_board_id=item["local_board_id"],
            board_key_snapshot=item["board_key_snapshot"],
            content_angle_id=item["content_angle_id"],
            angle_key_snapshot=item["angle_key_snapshot"],
            seed_keywords=item["seed_keywords"],
            selection_score=Decimal(item["selection_score"]),
            selection_metadata=item["selection_metadata"],
            item_fingerprint=item["item_fingerprint"],
            status="PLANNED",
            publication_id=None,
        ))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        same_fingerprint = db.scalar(
            select(PinterestPortfolioPlan)
            .where(PinterestPortfolioPlan.plan_fingerprint == preview["preview_fingerprint"])
            .limit(1)
        )
        if same_fingerprint is not None:
            return same_fingerprint
        raise PortfolioPlanningError("PORTFOLIO_PLAN_MONTH_CONFLICT") from None
    except Exception:
        db.rollback()
        raise

    db.refresh(plan)
    return plan
