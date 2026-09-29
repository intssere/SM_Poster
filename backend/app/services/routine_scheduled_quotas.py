"""Transactional quota reservations for scheduled autonomous publications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.models.routine_publishing import (
    RoutinePublishingControl,
    RoutineScheduledQuotaReservation,
)


class ScheduledQuotaError(RuntimeError):
    """A quota reservation or assessment could not satisfy its invariants."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


MAX_MONTH_ROWS_TO_VALIDATE = 10000


@dataclass(frozen=True)
class ScheduledQuotaLimits:
    """Positive maxima; product/vendor/board maxima are scoped to a month."""

    daily: int
    monthly: int
    product: int
    vendor: int
    board: int

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in self.__dict__.values()):
            raise ValueError("SCHEDULED_QUOTA_LIMITS_MUST_BE_POSITIVE_INTEGERS")


@dataclass(frozen=True)
class ScheduledQuotaAssessment:
    """Read-only quota usage and remaining room for one additional reservation."""

    daily_used: int
    monthly_used: int
    product_used: int
    vendor_used: int
    board_used: int
    daily_remaining: int
    monthly_remaining: int
    product_remaining: int
    vendor_remaining: int
    board_remaining: int
    can_reserve: bool
    already_reserved: bool
    already_committed: bool = False


def _reservation_identity(
    *,
    publication_id: str,
    plan_id: str,
    plan_item_id: str,
    product_id: str,
    vendor_key: str,
    board_id: str,
    scheduled_for: date,
) -> dict:
    if type(scheduled_for) is not date:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_DATE_REQUIRED")
    identity = {
        "publication_id": publication_id,
        "plan_id": plan_id,
        "plan_item_id": plan_item_id,
        "product_id": product_id,
        "vendor_key": vendor_key.strip().casefold() if isinstance(vendor_key, str) else "",
        "board_id": board_id,
        "scheduled_for": scheduled_for,
        "month_start": date(scheduled_for.year, scheduled_for.month, 1),
    }
    if any(
        not isinstance(identity[key], str) or not identity[key].strip()
        for key in (
            "publication_id",
            "plan_id",
            "plan_item_id",
            "product_id",
            "vendor_key",
            "board_id",
        )
    ):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_IDENTITY_REQUIRED")
    return identity


def _validate_limits(limits: ScheduledQuotaLimits):
    if not isinstance(limits, ScheduledQuotaLimits):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_LIMITS_REQUIRED")
    if any(
        type(value) is not int or value < 1
        for value in (
            limits.daily,
            limits.monthly,
            limits.product,
            limits.vendor,
            limits.board,
        )
    ):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_LIMITS_INVALID")


def _quota_usage(db, identity: dict) -> tuple[dict[str, int], object | None]:
    """Read and validate the relevant ledger month without autoflushing changes.

    A corrupt month bucket or noncanonical vendor could evade one of the five
    counts. Inspect both the bucket and the actual scheduled dates so those
    rows cannot silently disappear from the cap calculation.
    """
    ledger = RoutineScheduledQuotaReservation
    month = ledger.month_start == identity["month_start"]
    first = identity["month_start"]
    following = date(first.year + 1, 1, 1) if first.month == 12 else date(
        first.year, first.month + 1, 1
    )

    def count_for(*criteria):
        return int(
            db.scalar(
                select(func.count())
                .select_from(ledger)
                .where(*criteria)
            )
            or 0
        )

    with db.no_autoflush:
        # Fetch columns, not ORM instances: an already-loaded identity-map
        # object can be stale after another transaction changes a ledger row.
        existing = db.execute(
            select(
                ledger.id,
                ledger.publication_id,
                ledger.plan_id,
                ledger.plan_item_id,
                ledger.product_id,
                ledger.vendor_key,
                ledger.board_id,
                ledger.scheduled_for,
                ledger.month_start,
            ).where(ledger.publication_id == identity["publication_id"])
        ).one_or_none()
        month_rows = db.execute(
            select(ledger.scheduled_for, ledger.month_start, ledger.vendor_key).where(
                or_(
                    month,
                    (ledger.scheduled_for >= first) & (ledger.scheduled_for < following),
                )
            ).limit(MAX_MONTH_ROWS_TO_VALIDATE + 1)
        ).all()
        if len(month_rows) > MAX_MONTH_ROWS_TO_VALIDATE:
            raise ScheduledQuotaError("SCHEDULED_QUOTA_LEDGER_TOO_LARGE")
        for row in month_rows:
            if (
                row.month_start != date(row.scheduled_for.year, row.scheduled_for.month, 1)
                or not row.vendor_key
                or row.vendor_key != row.vendor_key.strip().casefold()
            ):
                raise ScheduledQuotaError("SCHEDULED_QUOTA_LEDGER_INCONSISTENT")
        counts = {
            "daily": count_for(ledger.scheduled_for == identity["scheduled_for"]),
            "monthly": count_for(month),
            "product": count_for(month, ledger.product_id == identity["product_id"]),
            "vendor": count_for(month, ledger.vendor_key == identity["vendor_key"]),
            "board": count_for(month, ledger.board_id == identity["board_id"]),
        }
    if existing is not None and any(
        getattr(existing, key) != value for key, value in identity.items()
    ):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_PUBLICATION_IDENTITY_CONFLICT")
    return counts, existing


def assess_scheduled_quota(
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
    """Assess ledger-only headroom; never reserves or commits.

    For a scheduled publication, use the read-only commitment reconciliation
    assessment instead; this primitive alone cannot see unreserved publications.
    Unlike reservation, this query takes no lock and works on any dialect.
    Missing ledger/schema and query failures fail closed.
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
    _validate_limits(limits)
    try:
        counts, existing = _quota_usage(db, identity)
    except ScheduledQuotaError:
        raise
    except SQLAlchemyError as exc:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_LEDGER_UNAVAILABLE") from exc

    if existing is not None and any(
        counts[name] > getattr(limits, name)
        for name in ("daily", "monthly", "product", "vendor", "board")
    ):
        raise ScheduledQuotaError("SCHEDULED_QUOTA_EXISTING_LIMIT_EXCEEDED")
    remaining = {
        name: max(0, getattr(limits, name) - counts[name])
        for name in ("daily", "monthly", "product", "vendor", "board")
    }
    return ScheduledQuotaAssessment(
        daily_used=counts["daily"],
        monthly_used=counts["monthly"],
        product_used=counts["product"],
        vendor_used=counts["vendor"],
        board_used=counts["board"],
        daily_remaining=remaining["daily"],
        monthly_remaining=remaining["monthly"],
        product_remaining=remaining["product"],
        vendor_remaining=remaining["vendor"],
        board_remaining=remaining["board"],
        can_reserve=all(value > 0 for value in remaining.values()),
        already_reserved=existing is not None,
    )


def reserve_scheduled_quota(
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
) -> RoutineScheduledQuotaReservation:
    """Reserve quota in the caller's transaction; the caller commits or rolls back.

    PostgreSQL's singleton control-row lock serializes both the duplicate check
    and all quota counts. No other database is accepted for a real reservation.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        raise ScheduledQuotaError("SCHEDULED_QUOTA_DATABASE_UNSUPPORTED")
    identity = _reservation_identity(
        publication_id=publication_id,
        plan_id=plan_id,
        plan_item_id=plan_item_id,
        product_id=product_id,
        vendor_key=vendor_key,
        board_id=board_id,
        scheduled_for=scheduled_for,
    )
    _validate_limits(limits)

    control = db.scalar(
        select(RoutinePublishingControl)
        .where(RoutinePublishingControl.id == "default")
        .with_for_update()
    )
    if control is None:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_CONTROL_ROW_MISSING")

    try:
        counts, existing = _quota_usage(db, identity)
    except SQLAlchemyError as exc:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_LEDGER_UNAVAILABLE") from exc
    if existing is not None:
        if any(
            counts[name] > getattr(limits, name)
            for name in ("daily", "monthly", "product", "vendor", "board")
        ):
            raise ScheduledQuotaError("SCHEDULED_QUOTA_EXISTING_LIMIT_EXCEEDED")
        current = db.get(RoutineScheduledQuotaReservation, existing.id, populate_existing=True)
        if current is None:
            raise ScheduledQuotaError("SCHEDULED_QUOTA_LEDGER_INCONSISTENT")
        return current

    if counts["daily"] >= limits.daily:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_DAILY_LIMIT")
    if counts["monthly"] >= limits.monthly:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_MONTHLY_LIMIT")
    if counts["product"] >= limits.product:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_PRODUCT_LIMIT")
    if counts["vendor"] >= limits.vendor:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_VENDOR_LIMIT")
    if counts["board"] >= limits.board:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_BOARD_LIMIT")

    reservation = RoutineScheduledQuotaReservation(**identity)
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ScheduledQuotaError("SCHEDULED_QUOTA_RESERVATION_CONFLICT") from exc
    return reservation