"""Read-only reconciliation of scheduled publications with quota reservations.

This is a DRY_RUN observation, not a lock or authorization for live admission.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.models.domain import (
    Board,
    PinConcept,
    PinDraft,
    PinPublication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    Product,
    PublicationStatus,
)
from app.models.routine_publishing import RoutineScheduledQuotaReservation
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.routine_scheduled_quotas import (
    ScheduledQuotaAssessment,
    ScheduledQuotaError,
    ScheduledQuotaLimits,
    _reservation_identity,
    assess_scheduled_quota,
)

MAX_COMMITMENTS_TO_VALIDATE = 10000
DIMENSIONS = ("daily", "monthly", "product", "vendor", "board")
COUNTED_STATUSES = {
    PublicationStatus.SCHEDULED,
    PublicationStatus.PUBLISHING,
    PublicationStatus.PUBLISHED,
}


def _following_month(first: date) -> date:
    return date(first.year + 1, 1, 1) if first.month == 12 else date(
        first.year, first.month + 1, 1
    )


def _publication_rows(db, first: date, following: date, ledger_ids: list[str]):
    pub = PinPublication
    draft = PinDraft
    concept = PinConcept
    item = PinterestPortfolioPlanItem
    plan = PinterestPortfolioPlan
    product = Product
    board = Board
    start = datetime.combine(first, time.min, tzinfo=timezone.utc)
    end = datetime.combine(following, time.min, tzinfo=timezone.utc)
    return db.execute(
        select(
            pub.id.label("publication_id"),
            pub.status.label("status"),
            pub.scheduled_for.label("publication_scheduled_for"),
            pub.board_id.label("publication_board_id"),
            draft.id.label("draft_id"),
            concept.id.label("concept_id"),
            concept.store_id.label("concept_store_id"),
            concept.product_id.label("concept_product_id"),
            concept.board_id.label("concept_board_id"),
            concept.content_angle_id.label("concept_angle_id"),
            item.id.label("plan_item_id"),
            item.publication_id.label("item_publication_id"),
            item.plan_id.label("item_plan_id"),
            item.planned_date.label("planned_date"),
            item.product_id.label("item_product_id"),
            item.local_board_id.label("item_board_id"),
            item.board_key_snapshot.label("board_key_snapshot"),
            item.content_angle_id.label("item_angle_id"),
            item.is_reserve.label("is_reserve"),
            item.status.label("item_status"),
            plan.id.label("plan_id"),
            plan.store_id.label("plan_store_id"),
            plan.month_start.label("plan_month_start"),
            plan.month_end.label("plan_month_end"),
            plan.status.label("plan_status"),
            product.id.label("product_id"),
            product.store_id.label("product_store_id"),
            product.vendor.label("vendor"),
            board.id.label("board_id"),
            board.store_id.label("board_store_id"),
            board.slug.label("board_slug"),
            board.active.label("board_active"),
        )
        .select_from(pub)
        .outerjoin(draft, draft.id == pub.draft_id)
        .outerjoin(concept, concept.id == draft.concept_id)
        .outerjoin(item, item.publication_id == pub.id)
        .outerjoin(plan, plan.id == item.plan_id)
        .outerjoin(product, product.id == concept.product_id)
        .outerjoin(board, board.id == item.local_board_id)
        .where(or_(
            (pub.scheduled_for >= start) & (pub.scheduled_for < end),
            plan.month_start == first,
            (item.planned_date >= first) & (item.planned_date < following),
            pub.id.in_(ledger_ids),
        ))
        .order_by(pub.id, item.id)
        .limit(MAX_COMMITMENTS_TO_VALIDATE + 1)
    ).mappings().all()


def _canonical_identity(row, first: date) -> dict:
    """Resolve every count from persisted publication and plan lineage."""
    scheduled = row["publication_scheduled_for"]
    if scheduled is None:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_STALE")
    day = normalize_persisted_utc(scheduled).date()
    if date(day.year, day.month, 1) != first:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_STALE")
    status = row["status"]
    if status not in COUNTED_STATUSES:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_STALE")
    if (
        not row["draft_id"] or not row["concept_id"]
        or not row["plan_item_id"] or not row["plan_id"]
        or not row["product_id"] or not row["board_id"]
        or not row["vendor"] or not row["vendor"].strip()
        or row["item_publication_id"] != row["publication_id"]
    ):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_AMBIGUOUS")
    if (
        row["concept_product_id"] != row["product_id"]
        or row["item_product_id"] != row["product_id"]
        or row["concept_board_id"] != row["board_id"]
        or row["item_board_id"] != row["board_id"]
        or row["publication_board_id"] not in (None, row["board_id"])
        or row["concept_angle_id"] != row["item_angle_id"]
        or row["board_key_snapshot"] != row["board_slug"]
        or row["concept_store_id"] != row["plan_store_id"]
        or row["product_store_id"] != row["plan_store_id"]
        or row["board_store_id"] != row["plan_store_id"]
        or not row["board_active"]
        or row["item_plan_id"] != row["plan_id"]
        or row["is_reserve"]
        or row["planned_date"] != day
        or row["plan_month_start"] != first
        or not row["plan_month_end"]
        or not row["plan_month_start"] <= day <= row["plan_month_end"]
        or (status == PublicationStatus.PUBLISHED and row["item_status"] != "PUBLISHED")
        or (status != PublicationStatus.PUBLISHED and row["item_status"] != "SCHEDULED")
        or (status != PublicationStatus.PUBLISHED and row["plan_status"] != "ACTIVE")
        or (status == PublicationStatus.PUBLISHED and row["plan_status"] not in ("ACTIVE", "COMPLETED"))
    ):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_CONFLICT")
    return _reservation_identity(
        publication_id=row["publication_id"],
        plan_id=row["plan_id"],
        plan_item_id=row["plan_item_id"],
        product_id=row["product_id"],
        vendor_key=row["vendor"],
        board_id=row["board_id"],
        scheduled_for=day,
    )


def assess_scheduled_quota_with_commitments(
    db,
    *,
    publication_id: str,
    plan_id: str,
    plan_item_id: str,
    product_id: str,
    vendor_key: str,
    board_id: str,
    scheduled_for: date,
    limits: ScheduledQuotaLimits,
) -> ScheduledQuotaAssessment:
    """Observe ledger plus unreserved persisted commitments; never write or lock.

    A matching publication contributes one slot, whether or not it has a ledger
    reservation. Any unresolved or drifted commitment blocks the certificate.
    The result is provisional and must not be used as a live admission decision.
    """
    identity = _reservation_identity(
        publication_id=publication_id,
        plan_id=plan_id,
        plan_item_id=plan_item_id,
        product_id=product_id,
        vendor_key=vendor_key,
        board_id=board_id,
        scheduled_for=scheduled_for,
    )
    ledger_assessment = assess_scheduled_quota(
        db,
        **{key: value for key, value in identity.items() if key != "month_start"},
        limits=limits,
    )
    first = identity["month_start"]
    following = _following_month(first)
    ledger = RoutineScheduledQuotaReservation
    try:
        with db.no_autoflush:
            ledger_columns = (
                ledger.publication_id, ledger.plan_id, ledger.plan_item_id,
                ledger.product_id, ledger.vendor_key, ledger.board_id,
                ledger.scheduled_for, ledger.month_start,
            )
            month_ledger = db.execute(
                select(*ledger_columns).where(or_(
                    ledger.month_start == first,
                    (ledger.scheduled_for >= first) & (ledger.scheduled_for < following),
                )).limit(MAX_COMMITMENTS_TO_VALIDATE + 1)
            ).mappings().all()
            if len(month_ledger) > MAX_COMMITMENTS_TO_VALIDATE:
                raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENTS_TOO_LARGE")
            rows = _publication_rows(
                db, first, following, [row["publication_id"] for row in month_ledger]
            )
            if len(rows) > MAX_COMMITMENTS_TO_VALIDATE:
                raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENTS_TOO_LARGE")
            ledger_rows = db.execute(
                select(*ledger_columns).where(or_(
                    ledger.month_start == first,
                    (ledger.scheduled_for >= first) & (ledger.scheduled_for < following),
                    ledger.publication_id.in_([row["publication_id"] for row in rows]),
                )).limit(MAX_COMMITMENTS_TO_VALIDATE + 1)
            ).mappings().all()
    except ScheduledQuotaError:
        raise
    except SQLAlchemyError as exc:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENTS_UNAVAILABLE") from exc
    if len(ledger_rows) > MAX_COMMITMENTS_TO_VALIDATE:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENTS_TOO_LARGE")
    reserved = {row["publication_id"]: row for row in ledger_rows}

    seen: set[str] = set()
    extra: Counter[str] = Counter()
    already_committed = False
    for row in rows:
        pub_id = row["publication_id"]
        if pub_id in seen:
            raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_AMBIGUOUS")
        seen.add(pub_id)
        status = row["status"]
        if status == PublicationStatus.CANCELLED and pub_id not in reserved:
            continue
        canonical = _canonical_identity(row, first)
        if pub_id == publication_id:
            if canonical != identity:
                raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_CONFLICT")
            already_committed = True
        if pub_id in reserved:
            if any(reserved[pub_id][key] != value for key, value in canonical.items()):
                raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_CONFLICT")
            continue
        extra["monthly"] += 1
        if canonical["scheduled_for"] == scheduled_for:
            extra["daily"] += 1
        if canonical["product_id"] == product_id:
            extra["product"] += 1
        if canonical["vendor_key"] == identity["vendor_key"]:
            extra["vendor"] += 1
        if canonical["board_id"] == board_id:
            extra["board"] += 1
    if reserved.keys() - seen:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_STALE")
    if publication_id not in seen:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_COMMITMENT_STALE")

    used = {name: getattr(ledger_assessment, f"{name}_used") + extra[name] for name in DIMENSIONS}
    if already_committed and any(used[name] > getattr(limits, name) for name in DIMENSIONS):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_EXISTING_LIMIT_EXCEEDED")
    remaining = {name: max(0, getattr(limits, name) - used[name]) for name in DIMENSIONS}
    return replace(
        ledger_assessment,
        **{f"{name}_used": used[name] for name in DIMENSIONS},
        **{f"{name}_remaining": remaining[name] for name in DIMENSIONS},
        can_reserve=all(value > 0 for value in remaining.values()),
        already_committed=already_committed,
    )