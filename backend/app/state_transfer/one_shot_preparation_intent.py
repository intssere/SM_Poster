"""Pure, non-executing admission boundary for the one-shot five-pin runner.

This is NOT an executable production entrypoint. It cannot reserve, prepare,
schedule, publish, or invoke providers. The subsequent runner must persist the
reservation in its own transaction BEFORE creating any bounded batch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from .one_shot_bounded_admission import validate_preflight_for_one_shot
from .one_shot_operation_reservation import OPERATION_KEY


class OneShotIntentRefusal(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OneShotPreparationIntent:
    operation_key: str
    batch_id: str
    preflight_fingerprint: str
    plan_id: str
    plan_fingerprint: str
    candidate_item_ids: tuple[str, ...]
    current_date: str


def build_intent(*, report: dict, operation_key: str, batch_id: str,
                 today: str) -> OneShotPreparationIntent:
    """Bind exactly one five-candidate 0035 receipt to operator-supplied IDs.

    Caller must supply a genuinely fresh, uncached provider-free report. This
    method does not certify freshness independently or create a reservation.
    """
    if not isinstance(report, dict) or report.get("database_revision") != "0035":
        raise OneShotIntentRefusal("ONE_SHOT_SCHEMA_0035_REQUIRED")
    if not isinstance(operation_key, str) or not OPERATION_KEY.fullmatch(operation_key):
        raise OneShotIntentRefusal("ONE_SHOT_OPERATION_KEY_INVALID")
    try:
        parsed = UUID(batch_id)
        if parsed.version != 4 or str(parsed) != batch_id:
            raise ValueError("batch ID must be canonical UUIDv4")
    except (ValueError, TypeError, AttributeError):
        raise OneShotIntentRefusal("ONE_SHOT_BATCH_ID_INVALID") from None
    try:
        if date.fromisoformat(today).isoformat() != today:
            raise ValueError("date is not canonical")
    except (TypeError, ValueError, AttributeError):
        raise OneShotIntentRefusal("ONE_SHOT_DATE_INVALID") from None

    receipt = validate_preflight_for_one_shot(report, today=today)
    candidates = receipt["candidates"]
    return OneShotPreparationIntent(
        operation_key=operation_key,
        batch_id=batch_id,
        preflight_fingerprint=receipt["preflight_fingerprint"],
        plan_id=receipt["plan_id"],
        plan_fingerprint=receipt["plan_fingerprint"],
        candidate_item_ids=tuple(candidate["item_id"] for candidate in candidates),
        current_date=receipt["current_date"],
    )
