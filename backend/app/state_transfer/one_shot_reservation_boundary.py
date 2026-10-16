"""Task 61.76G: durable reservation barrier, not a batch runner.

Only admits an already validated one-shot intent and commits its operation
identity using the 0035 ledger. Nothing here creates or prepares batches.
An uncertain result is terminal to the caller: NEVER retry automatically.
"""
from __future__ import annotations

from .one_shot_preparation_intent import OneShotPreparationIntent
from .one_shot_operation_reservation import (
    OneShotReservationRefusal, reserve_once,
)


def reserve_preparation_intent(db, intent: OneShotPreparationIntent) -> None:
    """Commit reservation before ANY subsequent batch write by a future runner.

    The future execution coordinator must obtain fresh preflight in the same
    invocation, validate it with build_intent, then invoke this barrier exactly
    once. Do not interpret exceptions as permission to retry or prepare.
    """
    if type(intent) is not OneShotPreparationIntent:
        raise OneShotReservationRefusal("ONE_SHOT_VALIDATED_INTENT_REQUIRED")
    reserve_once(
        db,
        operation_key=intent.operation_key,
        preflight_fingerprint=intent.preflight_fingerprint,
        batch_id=intent.batch_id,
    )
