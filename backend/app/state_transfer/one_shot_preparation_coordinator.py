"""Ordering-only coordinator for an explicitly authorized one-shot.

There is NO CLI, scheduler hook, or production entrypoint. The caller must
supply a freshly validated intent, a separately controlled settings object and
independent certification callback. In particular, failures never trigger
another reservation or batch preparation attempt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .one_shot_preparation_intent import OneShotPreparationIntent
from .one_shot_reservation_boundary import reserve_preparation_intent


class OneShotCoordinatorRefusal(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OneShotCoordinatorResult:
    operation_key: str
    batch_id: str
    certification: str


def coordinate_once(
    *,
    db,
    intent: OneShotPreparationIntent,
    reserve: Callable = reserve_preparation_intent,
    create: Callable,
    prepare: Callable,
    certify: Callable,
    settings,
    expected_preflight: dict,
) -> OneShotCoordinatorResult:
    """Fail-closed ordering; callbacks are explicit and never retried.

    This contract is *not* sufficient to authorize production execution.
    Independent certification must check the durable manifest; it may not
    simply return success on the basis of prepare() returning.
    """
    if type(intent) is not OneShotPreparationIntent:
        raise OneShotCoordinatorRefusal("ONE_SHOT_VALIDATED_INTENT_REQUIRED")
    if not isinstance(expected_preflight, dict):
        raise OneShotCoordinatorRefusal("ONE_SHOT_EXPECTED_PREFLIGHT_REQUIRED")
    if expected_preflight.get("preflight_fingerprint") != intent.preflight_fingerprint:
        raise OneShotCoordinatorRefusal("ONE_SHOT_PREFLIGHT_FINGERPRINT_MISMATCH")
    if not all(callable(fn) for fn in (reserve, create, prepare, certify)):
        raise OneShotCoordinatorRefusal("ONE_SHOT_CALLBACK_REQUIRED")
    # This call must commit before create() is invoked; a lost acknowledgment
    # must propagate as uncertain, not be interpreted as a retry opportunity.
    reserve(db, intent)
    created_id = create(db, settings=settings, batch_id=intent.batch_id)
    if created_id != intent.batch_id:
        raise OneShotCoordinatorRefusal("ONE_SHOT_BATCH_ID_MISMATCH")
    prepared_id = prepare(
        db, intent.batch_id, intent.plan_id, settings=settings,
        expected_preflight=expected_preflight,
    )
    if prepared_id != intent.batch_id:
        raise OneShotCoordinatorRefusal("ONE_SHOT_PREPARED_ID_MISMATCH")
    verdict = certify(intent)
    if verdict is not True:
        raise OneShotCoordinatorRefusal("ONE_SHOT_INDEPENDENT_CERTIFICATION_REQUIRED")
    return OneShotCoordinatorResult(
        operation_key=intent.operation_key, batch_id=intent.batch_id,
        certification="PASS",
    )
