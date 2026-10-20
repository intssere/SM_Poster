"""Task 61.76I: fail-closed ledger lifecycle around an injected coordinator.

Not an executable entrypoint. No automatic recovery, retries, scheduler or
provider operations. Any crash leaves an immutable consumed reservation which
requires independent operator inspection before further action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .one_shot_preparation_intent import OneShotPreparationIntent
from .one_shot_operation_reservation import mark_outcome
from .one_shot_preparation_coordinator import coordinate_once


class OneShotLifecycleRefusal(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OneShotLifecycleResult:
    operation_key: str
    batch_id: str
    state: str


def coordinate_ledger_lifecycle(
    *, db, intent: OneShotPreparationIntent, settings, expected_preflight: dict,
    reserve: Callable, create: Callable, prepare: Callable, certify: Callable,
    transition: Callable = mark_outcome,
) -> OneShotLifecycleResult:
    """Run exactly one injected sequence with committed ledger transitions.

    RESERVED is inserted by reserve; PREPARING is committed *before* batch
    creation. A failed/canceled/ambiguous downstream step only attempts a
    terminal UNCERTAIN transition, never permits a second attempt. A lost
    CAS acknowledgement also remains operator-review-only.
    """
    if type(intent) is not OneShotPreparationIntent:
        raise OneShotLifecycleRefusal("ONE_SHOT_VALIDATED_INTENT_REQUIRED")
    if not isinstance(expected_preflight, dict) or (
        expected_preflight.get("preflight_fingerprint") != intent.preflight_fingerprint
    ):
        raise OneShotLifecycleRefusal("ONE_SHOT_PREFLIGHT_MISMATCH")
    if not all(callable(fn) for fn in (reserve, create, prepare, certify, transition)):
        raise OneShotLifecycleRefusal("ONE_SHOT_CALLBACK_REQUIRED")

    preparing_confirmed = False

    def reserve_then_transition(session, bound):
        nonlocal preparing_confirmed
        reserve(session, bound)  # must commit before returning
        transition(
            session, operation_key=bound.operation_key,
            expected_state="RESERVED", next_state="PREPARING",
        )  # must commit before batch creation
        preparing_confirmed = True

    try:
        result = coordinate_once(
            db=db, intent=intent, settings=settings,
            expected_preflight=expected_preflight,
            reserve=reserve_then_transition, create=create,
            prepare=prepare, certify=certify,
        )
    except Exception:
        # No retry and no assertion that the DB mutation failed. A reserve
        # failure might have persisted, or a preparation might have committed
        # internal artifacts. Do not mark FAILED without independently
        # verifying the batch and all associated permits.
        if preparing_confirmed:
            try:
                transition(
                    db, operation_key=intent.operation_key,
                    expected_state="PREPARING", next_state="UNCERTAIN",
                )
            except Exception:
                pass  # preserve original refusal; operator must inspect ledger
        raise
    transition(
        db, operation_key=intent.operation_key,
        expected_state="PREPARING", next_state="READY",
    )
    return OneShotLifecycleResult(
        operation_key=result.operation_key,
        batch_id=result.batch_id,
        state="READY",
    )
