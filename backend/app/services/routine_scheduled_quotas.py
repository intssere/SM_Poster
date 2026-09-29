"""Transactional quota reservations for scheduled autonomous publications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
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
    """Read all relevant counts without flushing caller-owned pending changes."""
    ledger = RoutineScheduledQuotaReservation
    month = ledger.month_start == identity["month_start"]

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
        existing = db.scalar(
            select(ledger).where(ledger.publication_id == identity["publication_id"])
        )
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
    """Assess quota headroom using read-only queries; never reserves or commits.

    Unlike reservation, assessment can run on any database dialect because it
    takes no lock and makes no write. Missing ledger/schema and query failures
    are reported as a fail-closed ``ScheduledQuotaError``.
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

    existing = db.scalar(
        select(RoutineScheduledQuotaReservation).where(
            RoutineScheduledQuotaReservation.publication_id == publication_id
        )
    )
    if existing is not None:
        if all(getattr(existing, key) == value for key, value in identity.items()):
            return existing
        raise ScheduledQuotaError("SCHEDULED_QUOTA_PUBLICATION_IDENTITY_CONFLICT")

    def count_for(*criteria):
        return db.scalar(
            select(func.count())
            .select_from(RoutineScheduledQuotaReservation)
            .where(*criteria)
        ) or 0

    ledger = RoutineScheduledQuotaReservation
    month = ledger.month_start == identity["month_start"]
    if count_for(ledger.scheduled_for == scheduled_for) >= limits.daily:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_DAILY_LIMIT")
    if count_for(month) >= limits.monthly:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_MONTHLY_LIMIT")
    if count_for(month, ledger.product_id == product_id) >= limits.product:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_PRODUCT_LIMIT")
    if count_for(month, ledger.vendor_key == identity["vendor_key"]) >= limits.vendor:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_VENDOR_LIMIT")
    if count_for(month, ledger.board_id == board_id) >= limits.board:
        raise ScheduledQuotaError("SCHEDULED_QUOTA_BOARD_LIMIT")

    reservation = RoutineScheduledQuotaReservation(**identity)
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ScheduledQuotaError("SCHEDULED_QUOTA_RESERVATION_CONFLICT") from exc
    return reservation