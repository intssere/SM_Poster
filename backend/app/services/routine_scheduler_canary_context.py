"""Fail-closed context for one explicitly authorized offline scheduler tick."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from app.core.config import Settings


class CanarySafetyError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_CLOSED_SETTINGS_GATES = (
    "routine_pinterest_scheduler_enabled",
    "routine_pinterest_worker_enabled",
    "routine_scheduled_autonomy_enabled",
    "publishing_enabled",
    "buffer_publishing_enabled",
    "routine_buffer_dispatch_enabled",
    "routine_scheduled_live_admission_enabled",
    "routine_autonomous_authorization_enabled",
    "pinterest_write_scope_enabled",
    "pinterest_board_write_scope_enabled",
    "pinterest_board_provisioning_enabled",
    "pinterest_autonomous_board_ensure_enabled",
    "pinterest_seo_brief_persistence_enabled",
    "pinterest_autonomous_generation_enabled",
    "pinterest_autonomous_execution_enabled",
    "pinterest_portfolio_planner_enabled",
    "pinterest_optimizer_enabled",
    "pinterest_optimizer_apply_enabled",
    "pinterest_portfolio_activation_enabled",
    "pinterest_analytics_ingestion_enabled",
    "pinterest_learning_snapshot_persistence_enabled",
    "buffer_single_pin_pilot_enabled",
    "pinterest_single_pin_pilot_enabled",
)


@dataclass(frozen=True)
class RoutineSchedulerCanaryContext:
    lease: object
    idempotency_key: str
    target_publication_id: str
    target_permit_id: str
    expected_publication_fingerprint: str
    expected_request_fingerprint: str
    expected_route_id: str
    deadline_monotonic: float

    def validate(self, settings: Settings, *, monotonic_now: float | None = None) -> None:
        if getattr(settings, "routine_scheduler_canary_enabled", None) is not True:
            raise CanarySafetyError("CANARY_GATE_NOT_ENABLED")
        if any(getattr(settings, key, None) is not False for key in _CLOSED_SETTINGS_GATES):
            raise CanarySafetyError("CANARY_SETTINGS_NOT_CLOSED")
        if getattr(settings, "routine_pinterest_dry_run", None) is not True:
            raise CanarySafetyError("CANARY_DRY_RUN_REQUIRED")
        if getattr(settings, "routine_pinterest_batch_size", None) != 1:
            raise CanarySafetyError("CANARY_BATCH_SIZE_MUST_BE_ONE")
        if getattr(settings, "routine_pinterest_daily_write_limit", None) != 1:
            raise CanarySafetyError("CANARY_DAILY_LIMIT_MUST_BE_ONE")
        bindings = (
            self.idempotency_key,
            self.target_publication_id,
            self.target_permit_id,
            self.expected_publication_fingerprint,
            self.expected_request_fingerprint,
            self.expected_route_id,
        )
        if any(not isinstance(value, str) or not value.strip() for value in bindings):
            raise CanarySafetyError("CANARY_TARGET_BINDING_REQUIRED")
        current = time.monotonic() if monotonic_now is None else monotonic_now
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
            or isinstance(self.deadline_monotonic, bool)
            or not isinstance(self.deadline_monotonic, (int, float))
            or not math.isfinite(self.deadline_monotonic)
            or current >= self.deadline_monotonic
        ):
            raise CanarySafetyError("CANARY_CONTEXT_EXPIRED")
        try:
            held = bool(getattr(self.lease, "held", False))
        except Exception as exc:
            raise CanarySafetyError("CANARY_LEASE_INVALID") from exc
        if not held:
            raise CanarySafetyError("CANARY_LEASE_NOT_HELD")
        try:
            valid = self.lease.validate()
        except Exception as exc:
            raise CanarySafetyError("CANARY_LEASE_INVALID") from exc
        if valid is not True:
            raise CanarySafetyError("CANARY_LEASE_INVALID")


def validate_canary_context(
    context: RoutineSchedulerCanaryContext,
    settings: Settings,
    *,
    monotonic_now: float | None = None,
) -> None:
    if not isinstance(context, RoutineSchedulerCanaryContext):
        raise CanarySafetyError("CANARY_CONTEXT_INVALID")
    context.validate(settings, monotonic_now=monotonic_now)