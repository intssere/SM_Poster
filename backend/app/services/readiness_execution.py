"""Fail-closed management orchestration. Only the admission table is written."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import time

from sqlalchemy import text

from app.core.config import Settings
from app.services.deployment_attestation import read_build_provenance
from app.services.readiness_execution_contract import ReadinessBinding, ReadinessError

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]
OVERLAY_PATH = ROOT / ".replit"
PROBE_PATH = ROOT / "backend" / "scripts" / "object_storage_readiness.py"
PARENT_GATE = "OBJECT_STORAGE_READINESS_PROBE_ENABLED"
_COUNTER_TABLES = (
    "publication_attempts", "routine_attempt_boundaries", "routine_dispatch_permits",
    "routine_scheduled_quota_reservations", "routine_publishing_runs",
)


def require_static_safety(settings: Settings, *, executing: bool = True) -> None:
    if executing and settings.object_storage_readiness_management_enabled != "true":
        raise ReadinessError("MANAGEMENT_GATE_DISABLED")
    if settings.auth_disabled:
        raise ReadinessError("REAL_ADMIN_AUTH_REQUIRED")
    if os.environ.get(PARENT_GATE, "false") != "false":
        raise ReadinessError("PARENT_GATE_NOT_CLOSED")
    for name in Settings.model_fields:
        if name.endswith("_enabled") and name != "object_storage_readiness_management_enabled":
            if getattr(settings, name) is not False:
                raise ReadinessError("OPERATIONAL_GATES_NOT_CLOSED")
    if (
        settings.routine_pinterest_dry_run is not True
        or settings.routine_pinterest_batch_size != 1
        or settings.routine_pinterest_daily_write_limit != 1
    ):
        raise ReadinessError("DRY_RUN_SAFETY_REQUIRED")
    if os.environ.get("REPLIT_DEPLOYMENT") != "1":
        raise ReadinessError("PUBLISHED_RUNTIME_REQUIRED")


def require_runtime_binding(binding: ReadinessBinding) -> None:
    provenance = read_build_provenance()
    if (
        not provenance.present or not provenance.valid
        or provenance.commit_sha != binding.canonical_commit_sha
        or provenance.tree_sha != binding.canonical_tree_sha
        or provenance.release_commit_sha != binding.release_commit_sha
        or provenance.release_tree_sha != binding.release_tree_sha
        or provenance.overlay_sha256 != binding.overlay_sha256
        or provenance.topology != binding.topology
    ):
        raise ReadinessError("RELEASE_BINDING_MISMATCH")
    try:
        overlay = hashlib.sha256(OVERLAY_PATH.read_bytes()).hexdigest()
        probe = hashlib.sha256(PROBE_PATH.read_bytes()).hexdigest()
    except OSError:
        raise ReadinessError("RELEASE_ARTIFACT_UNREADABLE") from None
    if overlay != binding.overlay_sha256 or probe != binding.probe_sha256:
        raise ReadinessError("RELEASE_ARTIFACT_MISMATCH")


def execution_engine():
    # Do not even construct application engine/settings before authorization.
    from app.db.session import engine
    return engine


def business_snapshot(engine) -> dict:
    """Bounded, read-only baseline; no ORM, token decryption or business actions."""
    if engine.dialect.name != "postgresql":
        raise ReadinessError("POSTGRESQL_REQUIRED")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL search_path TO public, pg_catalog")
            connection.exec_driver_sql("SET LOCAL statement_timeout = 5000")
            state = connection.execute(text(
                "SELECT state FROM routine_publishing_control WHERE id = 'default'"
            )).scalar_one_or_none()
            if state != "PAUSED":
                raise ReadinessError("ROUTINE_NOT_PAUSED")
            active = connection.scalar(text(
                "SELECT count(*) FROM routine_publishing_runs WHERE status = 'RUNNING'"
            ))
            uncertain = connection.scalar(text(
                "SELECT count(*) FROM pin_publications "
                "WHERE status IN ('PUBLISHING', 'PUBLISH_UNKNOWN')"
            ))
            if active or uncertain:
                raise ReadinessError("ACTIVE_OR_UNCERTAIN_BUSINESS_WORK")
            snapshot = {table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                        for table in _COUNTER_TABLES}
            snapshot["consumed_permits"] = connection.scalar(text(
                "SELECT count(*) FROM routine_dispatch_permits WHERE consumed_at IS NOT NULL"
            ))
            snapshot["control"] = state
            return snapshot
    except ReadinessError:
        raise
    except Exception:
        raise ReadinessError("BASELINE_UNAVAILABLE") from None


def require_scheduler_stopped(settings: Settings) -> None:
    from app.services.routine_pinterest_scheduler import scheduler_status
    state = scheduler_status(settings)
    if any(state.get(key) is not False
           for key in ("enabled", "started", "task_running", "tick_running", "lease_held")):
        raise ReadinessError("SCHEDULER_NOT_STOPPED")


def execute_readiness(settings: Settings, binding: ReadinessBinding, claims: dict) -> dict:
    from app.services.readiness_execution_admission import PostgresReadinessAdmission
    from app.services.readiness_execution_runner import launch_probe

    # Recheck inside the thread, immediately before durable consume/launch.
    require_static_safety(settings)
    require_runtime_binding(binding)
    require_scheduler_stopped(settings)
    if not claims["iat"] <= int(time.time()) < claims["exp"]:
        raise ReadinessError("EXECUTION_AUTHORIZATION_EXPIRED", 403)
    engine = execution_engine()
    baseline = business_snapshot(engine)
    store = PostgresReadinessAdmission(engine)
    if not store.consume(binding, claims["jti"], claims["actor"], expires_at=claims["exp"]):
        logger.info("readiness-management event=replay-blocked")
        raise ReadinessError("ADMISSION_ALREADY_CONSUMED", 409)
    logger.info("readiness-management event=admission-consumed")
    try:
        # Admission may block on another transaction. Never launch using an
        # expired grant or changed preconditions after its durable commit.
        require_static_safety(settings)
        require_runtime_binding(binding)
        require_scheduler_stopped(settings)
        if not claims["iat"] <= int(time.time()) < claims["exp"]:
            raise ReadinessError("EXECUTION_AUTHORIZATION_EXPIRED", 403)
        if business_snapshot(engine) != baseline:
            raise ReadinessError("BUSINESS_BASELINE_CHANGED")
        if not claims["iat"] <= int(time.time()) < claims["exp"]:
            raise ReadinessError("EXECUTION_AUTHORIZATION_EXPIRED", 403)
        result = launch_probe()
    except ReadinessError as error:
        result = {"outcome": "UNKNOWN", "exit_code": None, "receipt": None,
                  "error_code": error.code}
    except Exception:
        # Includes spawn failure: consumed evidence is NEVER reset or retried.
        result = {"outcome": "UNKNOWN", "exit_code": None, "receipt": None}
    try:
        if business_snapshot(engine) != baseline:
            result["outcome"] = "UNKNOWN"
            result["error_code"] = "BUSINESS_BASELINE_CHANGED"
    except ReadinessError:
        result["outcome"] = "UNKNOWN"
        result["error_code"] = "POST_BASELINE_UNAVAILABLE"
    store.finish(binding, result["outcome"], result["exit_code"], result["receipt"])
    logger.info("readiness-management event=outcome-recorded outcome=%s", result["outcome"])
    return {
        "admission_state": "CONSUMED", "outcome": result["outcome"],
        "exit_code": result["exit_code"], "receipt": result["receipt"],
        **({"error_code": result["error_code"]} if "error_code" in result else {}),
    }


def lookup_readiness(binding: ReadinessBinding) -> dict:
    from app.services.readiness_execution_admission import PostgresReadinessAdmission
    from app.services.readiness_execution_runner import validate_receipt
    result = PostgresReadinessAdmission(execution_engine()).lookup(binding)
    if result is None:
        return {"admission_state": "NOT_ADMITTED", "outcome": "NOT_RUN",
                "exit_code": None, "receipt": None}
    if result["receipt"] is not None:
        try:
            result["receipt"] = validate_receipt(result["receipt"], result["exit_code"])
        except Exception:
            raise ReadinessError("STORED_RECEIPT_INVALID") from None
    if result["outcome"] == "PASS" and (
        result["exit_code"] != 0 or result["receipt"] is None
        or result["receipt"]["final_status"] != "PASS"
    ):
        raise ReadinessError("STORED_RECEIPT_INVALID")
    if result["outcome"] == "FAILED" and (
        result["receipt"] is None
        or result["receipt"]["final_status"] not in {"FAILED", "BLOCKED"}
    ):
        raise ReadinessError("STORED_RECEIPT_INVALID")
    return result