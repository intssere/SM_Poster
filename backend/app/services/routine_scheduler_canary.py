"""Explicit, one-shot, provider-free scheduler canary."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.migration_adoption import verify_frozen_schema_at_head
from app.db.session import engine
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
    RoutineScheduledQuotaReservation,
)
from app.services.deployment_attestation import read_build_provenance
from app.services.routine_canary_fixture import _assert_static_safety
from app.services.routine_dispatch_authorization import validate_permit
from app.services.routine_offline_preflight import (
    RoutineOfflinePreflightError,
    build_routine_offline_evidence,
)
from app.services.routine_operational_readiness import routine_readiness_snapshot
from app.services.routine_pinterest_scheduler import scheduler_status, scheduler_tick
from app.services.routine_scheduler_canary_context import (
    _CLOSED_SETTINGS_GATES,
    CanarySafetyError,
    RoutineSchedulerCanaryContext,
    validate_canary_context,
)
from app.services.routine_scheduler_lease import LEASE_BACKEND, PostgresSchedulerLeaderLease


CONFIRMATION_TEXT_VERSION = "ROUTINE_SCHEDULER_CANARY_V1"
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE_RE = re.compile(r"^[A-Z0-9_]{1,100}$")


class RoutineSchedulerCanaryError(RuntimeError):
    def __init__(self, code: str, *, status: str = "BLOCKED"):
        self.code = code
        self.status = status
        super().__init__(code)


def _safe_result(status: str, code: str, **details) -> dict:
    return {
        "status": status,
        "code": code,
        "external_calls": 0,
        **details,
    }


def _target_configuration(settings: Settings) -> dict:
    target = {
        "publication_id": settings.routine_scheduler_canary_publication_id,
        "permit_id": settings.routine_scheduler_canary_permit_id,
        "publication_fingerprint": settings.routine_scheduler_canary_publication_fingerprint,
        "request_fingerprint": settings.routine_scheduler_canary_request_fingerprint,
        "route_id": settings.routine_scheduler_canary_route_id,
        "release_commit_sha": settings.routine_scheduler_canary_release_commit_sha,
        "release_tree_sha": settings.routine_scheduler_canary_release_tree_sha,
    }
    if not all(isinstance(value, str) and value.strip() for value in target.values()):
        raise RoutineSchedulerCanaryError("CANARY_FIXED_TARGET_CONFIGURATION_REQUIRED")
    if not _SHA256_RE.fullmatch(target["publication_fingerprint"]):
        raise RoutineSchedulerCanaryError("CANARY_PUBLICATION_FINGERPRINT_PIN_INVALID")
    if not _SHA256_RE.fullmatch(target["request_fingerprint"]):
        raise RoutineSchedulerCanaryError("CANARY_REQUEST_FINGERPRINT_PIN_INVALID")
    if not _SHA1_RE.fullmatch(target["release_commit_sha"]):
        raise RoutineSchedulerCanaryError("CANARY_RELEASE_COMMIT_PIN_INVALID")
    if not _SHA1_RE.fullmatch(target["release_tree_sha"]):
        raise RoutineSchedulerCanaryError("CANARY_RELEASE_TREE_PIN_INVALID")
    return target


def _idempotency_key(target: dict) -> str:
    # One canary attempt per pinned release, regardless of target/config edits.
    material = "|".join((
        "routine-scheduler-canary",
        target["release_commit_sha"],
        target["release_tree_sha"],
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _schema_is_exact_0031(timeout_seconds: float = 30) -> bool:
    if engine.dialect.name != "postgresql":
        return False
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                f"SET LOCAL statement_timeout = {max(1, int(timeout_seconds * 1000))}"
            )
            connection.exec_driver_sql("SET LOCAL search_path TO public, pg_catalog")
            verify_frozen_schema_at_head(connection, revision="0031")
        return True
    except Exception:
        return False


def _release_matches_pins(target: dict) -> bool:
    provenance = read_build_provenance()
    return bool(
        provenance.present
        and provenance.valid
        and provenance.release_commit_sha == target["release_commit_sha"]
        and provenance.release_tree_sha == target["release_tree_sha"]
    )


def _row_snapshot(db, model, ident: str) -> dict | None:
    row = db.get(model, ident)
    if row is None:
        return None
    return {column.key: getattr(row, column.key) for column in model.__table__.columns}


def _count(db, model, *criteria) -> int:
    query = select(func.count()).select_from(model)
    if criteria:
        query = query.where(*criteria)
    return int(db.scalar(query) or 0)


def _counts_snapshot(db) -> dict:
    return {
        "attempts": _count(db, PublicationAttempt),
        "boundaries": _count(db, RoutineAttemptBoundary),
        "permits": _count(db, RoutineDispatchPermit),
        "consumed_permits": _count(
            db, RoutineDispatchPermit, RoutineDispatchPermit.consumed_at.is_not(None)
        ),
        "reservations": _count(db, RoutineScheduledQuotaReservation),
        "unknown_publications": _count(
            db, PinPublication, PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN
        ),
        "publishing_publications": _count(
            db, PinPublication, PinPublication.status == PublicationStatus.PUBLISHING
        ),
    }


def _dedicated_session(timeout_seconds: int):
    connection = engine.connect()
    try:
        connection.detach()
        timeout_ms = max(1, int(timeout_seconds * 1000))
        connection.execute(text(f"SET statement_timeout = {timeout_ms}"))
        connection.execute(text(f"SET lock_timeout = {timeout_ms}"))
        connection.commit()
        return connection, sessionmaker(
            bind=connection, autoflush=False, autocommit=False
        )()
    except Exception:
        connection.close()
        raise


def _existing_idempotency_run(db):
    # The runner's durable marker does not persist release identity separately.
    # Therefore fail closed on any prior canary marker, not just the current
    # fingerprint-derived key; this prevents config edits from rearming a run.
    for run in db.scalars(select(RoutinePublishingRun)).all():
        metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
        marker = metadata.get("scheduler_canary")
        if isinstance(marker, dict) and (
            isinstance(marker.get("key"), str) and marker.get("key")
            or isinstance(marker.get("target_publication_id"), str)
            and marker.get("target_publication_id")
        ):
            return run
    return None


def _preflight(db, *, settings: Settings, target: dict, key: str) -> dict:
    now = datetime.now(timezone.utc)
    control = db.get(RoutinePublishingControl, "default")
    if control is None or control.state != "PAUSED":
        raise RoutineSchedulerCanaryError("CANARY_CONTROL_NOT_PAUSED", status="NOT_EXERCISED")

    if _existing_idempotency_run(db) is not None:
        raise RoutineSchedulerCanaryError("CANARY_IDEMPOTENCY_REQUIRES_RECONCILIATION")

    scheduler = scheduler_status(settings)
    try:
        _assert_static_safety(db, settings, control, scheduler)
    except Exception as exc:
        code = str(exc) if isinstance(exc, RuntimeError) else ""
        raise RoutineSchedulerCanaryError(
            code if _SAFE_CODE_RE.fullmatch(code) else "CANARY_STATIC_SAFETY_BLOCKED"
        ) from None

    operational = routine_readiness_snapshot(
        db, settings=settings, scheduler_snapshot=scheduler, now=now
    )
    if any(
        isinstance(alert, dict) and alert.get("severity") == "critical"
        for alert in operational.get("alerts", [])
    ):
        raise RoutineSchedulerCanaryError("CANARY_CRITICAL_OPERATIONAL_ALERT")

    if (
        _count(db, RoutinePublishingRun, RoutinePublishingRun.status == "RUNNING") != 0
        or _count(db, PinPublication, PinPublication.status == PublicationStatus.PUBLISHING) != 0
        or _count(db, PinPublication, PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN) != 0
    ):
        raise RoutineSchedulerCanaryError("CANARY_ACTIVE_OR_UNCERTAIN_WORK_PRESENT")

    due = list(db.scalars(
        select(PinPublication)
        .where(
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for <= now,
        )
        .order_by(PinPublication.scheduled_for, PinPublication.id)
        .limit(2)
    ).all())
    if len(due) != 1 or due[0].id != target["publication_id"]:
        raise RoutineSchedulerCanaryError(
            "CANARY_DUE_PUBLICATION_IDENTITY_OR_CARDINALITY",
            status="NOT_EXERCISED",
        )
    publication = due[0]
    permits = list(db.scalars(
        select(RoutineDispatchPermit)
        .where(RoutineDispatchPermit.status == "ACTIVE")
        .order_by(RoutineDispatchPermit.id)
        .limit(2)
    ).all())
    if len(permits) != 1 or permits[0].id != target["permit_id"]:
        raise RoutineSchedulerCanaryError(
            "CANARY_ACTIVE_PERMIT_IDENTITY_OR_CARDINALITY",
            status="NOT_EXERCISED",
        )
    permit = permits[0]
    valid = validate_permit(db, publication, permit, now=now, require_due=True)
    if valid.get("valid") is not True:
        raise RoutineSchedulerCanaryError("CANARY_PERMIT_INVALID", status="NOT_EXERCISED")
    if (
        publication.publication_fingerprint != target["publication_fingerprint"]
        or permit.publication_fingerprint != target["publication_fingerprint"]
        or permit.request_fingerprint != target["request_fingerprint"]
        or permit.pinterest_board_record_id != target["route_id"]
        or publication.pinterest_board_record_id != target["route_id"]
    ):
        raise RoutineSchedulerCanaryError("CANARY_FIXED_TARGET_BINDING_MISMATCH", status="NOT_EXERCISED")
    from app.services.publication_scheduler import request_fingerprint_for
    if request_fingerprint_for(publication) != target["request_fingerprint"]:
        raise RoutineSchedulerCanaryError("CANARY_REQUEST_FINGERPRINT_MISMATCH", status="NOT_EXERCISED")

    try:
        evidence = build_routine_offline_evidence(db, publication, permit=permit, now=now)
    except RoutineOfflinePreflightError:
        raise RoutineSchedulerCanaryError("CANARY_OFFLINE_ROUTE_PREFLIGHT_FAILED", status="NOT_EXERCISED") from None
    if (
        getattr(evidence, "publication_id", None) != publication.id
        or getattr(evidence, "permit_validated", None) is not True
        or getattr(evidence, "persisted_route_validated", None) is not True
        or getattr(evidence, "external_requests", None) != 0
    ):
        raise RoutineSchedulerCanaryError("CANARY_OFFLINE_ROUTE_EVIDENCE_INVALID", status="NOT_EXERCISED")

    counts = _counts_snapshot(db)
    if counts["unknown_publications"] or counts["publishing_publications"]:
        raise RoutineSchedulerCanaryError("CANARY_UNCERTAIN_PUBLICATION_PRESENT")
    return {
        "counts": counts,
        "publication": _row_snapshot(db, PinPublication, publication.id),
        "permit": _row_snapshot(db, RoutineDispatchPermit, permit.id),
        "control": _row_snapshot(db, RoutinePublishingControl, "default"),
    }


def _verify_postconditions(db, before: dict, target: dict) -> dict:
    after_counts = _counts_snapshot(db)
    if after_counts != before["counts"]:
        raise RoutineSchedulerCanaryError("CANARY_PROHIBITED_ROW_COUNTS_CHANGED")
    if _row_snapshot(db, PinPublication, target["publication_id"]) != before["publication"]:
        raise RoutineSchedulerCanaryError("CANARY_PUBLICATION_MUTATED")
    if _row_snapshot(db, RoutineDispatchPermit, target["permit_id"]) != before["permit"]:
        raise RoutineSchedulerCanaryError("CANARY_PERMIT_MUTATED")
    if _row_snapshot(db, RoutinePublishingControl, "default") != before["control"]:
        raise RoutineSchedulerCanaryError("CANARY_CONTROL_MUTATED")
    if after_counts["unknown_publications"] or after_counts["publishing_publications"]:
        raise RoutineSchedulerCanaryError("CANARY_UNCERTAIN_PUBLICATION_AFTER_TICK")
    return after_counts


def _certificate_outcomes(result: dict) -> list[dict]:
    evidence = result.get("canary_evidence")
    if not isinstance(evidence, dict) or evidence.get("provider_calls") != 0:
        raise RoutineSchedulerCanaryError("CANARY_RUNNER_EVIDENCE_MISSING")
    certificates = evidence.get("scheduled_autonomy_certificates")
    if not isinstance(certificates, list) or len(certificates) != 1:
        raise RoutineSchedulerCanaryError("CANARY_CERTIFICATE_CARDINALITY_INVALID")
    outcomes = []
    for certificate in certificates:
        if not isinstance(certificate, dict):
            raise RoutineSchedulerCanaryError("CANARY_CERTIFICATE_MALFORMED")
        atomic = certificate.get("atomic_admission")
        if not isinstance(atomic, dict):
            raise RoutineSchedulerCanaryError("CANARY_QUOTA_OUTCOME_MISSING")
        quota = certificate.get("quota_decisions")
        if not isinstance(quota, dict) or quota.get("passed") is not True:
            raise RoutineSchedulerCanaryError("CANARY_FIVE_DIMENSION_QUOTA_EVIDENCE_MISSING")
        headroom = quota.get("headroom")
        dimensions = ("daily", "monthly", "product", "vendor", "board")
        if (
            not isinstance(headroom, dict)
            or any(type(headroom.get(name)) is not int or headroom[name] < 0 for name in dimensions)
            or type(headroom.get("already_committed")) is not bool
            or type(headroom.get("already_reserved")) is not bool
        ):
            raise RoutineSchedulerCanaryError("CANARY_FIVE_DIMENSION_QUOTA_EVIDENCE_INVALID")
        outcome = {
            "ready": certificate.get("ready") is True,
            "offline_validated": certificate.get("offline_validated") is True,
            "blockers": [
                str(item)
                for item in certificate.get("blockers", [])
                if isinstance(item, str) and _SAFE_CODE_RE.fullmatch(item)
            ][:20],
            "atomic_admission_evaluated": atomic.get("evaluated") is True,
            "would_admit": atomic.get("would_admit") is True,
            "quota_headroom": {name: headroom[name] for name in dimensions},
            "already_committed": headroom["already_committed"],
            "already_reserved": headroom["already_reserved"],
            "claim_committed": atomic.get("claim_committed") is True,
            "reservation_committed": atomic.get("reservation_committed") is True,
            "external_requests": certificate.get("external_requests"),
        }
        outcomes.append(outcome)
        if (
            outcome["ready"] is not True
            or outcome["offline_validated"] is not True
            or outcome["atomic_admission_evaluated"] is not True
            or outcome["would_admit"] is not True
            or outcome["claim_committed"] is not False
            or outcome["reservation_committed"] is not False
            or outcome["external_requests"] != 0
        ):
            raise RoutineSchedulerCanaryError("CANARY_ADMISSION_NOT_EXERCISED")
    if evidence.get("claim_committed") is not False or evidence.get("reservation_committed") is not False:
        raise RoutineSchedulerCanaryError("CANARY_MUTATION_EVIDENCE_INVALID")
    return outcomes


async def run_routine_scheduler_canary(*, settings: Settings) -> dict:
    """Run a single scheduled DRY_RUN tick under an exclusive PG lease."""
    lease = PostgresSchedulerLeaderLease(engine)
    worker_connection = None
    lease_acquire_status = "NOT_ATTEMPTED"
    lease_backend_pid = None
    lease_release_exception = False
    scheduler_state_reset_failed = False
    result = None
    target = None
    try:
        if settings.routine_scheduler_canary_enabled is not True:
            raise RoutineSchedulerCanaryError("CANARY_GATE_DISABLED")
        if engine.dialect.name != "postgresql":
            raise RoutineSchedulerCanaryError("CANARY_POSTGRESQL_REQUIRED")
        target = _target_configuration(settings)
        if not _release_matches_pins(target):
            raise RoutineSchedulerCanaryError("CANARY_RELEASE_PROVENANCE_MISMATCH")
        timeout = settings.routine_scheduler_canary_timeout_seconds
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise RoutineSchedulerCanaryError("CANARY_TICK_TIMEOUT")
            return seconds

        lease_acquire_status = lease.acquire()
        if lease_acquire_status != "ACQUIRED":
            raise RoutineSchedulerCanaryError(
                "CANARY_LEASE_STANDBY" if lease_acquire_status == "STANDBY" else "CANARY_LEASE_UNAVAILABLE"
            )
        lease_backend_pid = getattr(lease, "backend_pid", None)

        if not _schema_is_exact_0031(remaining()):
            raise RoutineSchedulerCanaryError("CANARY_SCHEMA_REVISION_NOT_EXACTLY_0031")
        remaining()

        key = _idempotency_key(target)
        context = RoutineSchedulerCanaryContext(
            lease=lease,
            idempotency_key=key,
            target_publication_id=target["publication_id"],
            target_permit_id=target["permit_id"],
            expected_publication_fingerprint=target["publication_fingerprint"],
            expected_request_fingerprint=target["request_fingerprint"],
            expected_route_id=target["route_id"],
            deadline_monotonic=deadline,
        )
        validate_canary_context(context, settings)

        preflight_connection, preflight_db = _dedicated_session(remaining())
        try:
            before = _preflight(preflight_db, settings=settings, target=target, key=key)
        finally:
            preflight_db.rollback()
            preflight_db.close()
            preflight_connection.close()

        # Keep the worker on a physically dedicated PostgreSQL session with a
        # server-side statement deadline. Detaching ensures close cannot return
        # a timeout-configured session to the shared pool.
        worker_connection = engine.connect()
        worker_connection.detach()
        statement_timeout_ms = max(1, int(remaining() * 1000))
        worker_connection.execute(text(
            f"SET statement_timeout = {statement_timeout_ms}"
        ))
        worker_connection.execute(text(
            f"SET lock_timeout = {statement_timeout_ms}"
        ))
        worker_connection.commit()
        worker_factory = sessionmaker(
            bind=worker_connection, autoflush=False, autocommit=False
        )

        try:
            worker_result = await asyncio.wait_for(
                scheduler_tick(
                    settings=settings,
                    canary_context=context,
                    leader_lease=lease,
                    session_factory=worker_factory,
                ),
                timeout=remaining(),
            )
        except asyncio.TimeoutError:
            raise RoutineSchedulerCanaryError("CANARY_TICK_TIMEOUT") from None

        validate_canary_context(context, settings)
        if not isinstance(worker_result, dict):
            raise RoutineSchedulerCanaryError("CANARY_TICK_RESULT_INVALID")
        if worker_result.get("status") == "NOT_EXERCISED":
            raise RoutineSchedulerCanaryError(
                "CANARY_WORKER_NOT_EXERCISED",
                status="NOT_EXERCISED",
            )
        if (
            worker_result.get("status") != "SUCCEEDED"
            or worker_result.get("mode") != "DRY_RUN"
            or worker_result.get("scanned") != 1
            or worker_result.get("eligible") != 1
            or worker_result.get("skipped") != 0
            or any(worker_result.get(field) != 0 for field in (
                "claimed", "dispatched", "published", "failed", "unknown"
            ))
        ):
            raise RoutineSchedulerCanaryError("CANARY_TICK_DID_NOT_COMPLETE_SAFELY")
        canary_evidence = worker_result.get("canary_evidence")
        marker = canary_evidence.get("scheduler_canary") if isinstance(canary_evidence, dict) else None
        if (
            not isinstance(marker, dict)
            or marker.get("key") != key
            or marker.get("target_publication_id") != target["publication_id"]
        ):
            raise RoutineSchedulerCanaryError("CANARY_IDEMPOTENCY_MARKER_MISMATCH")
        quota_outcomes = _certificate_outcomes(worker_result)

        verify_connection, verify_db = _dedicated_session(remaining())
        try:
            after_counts = _verify_postconditions(verify_db, before, target)
        finally:
            verify_db.rollback()
            verify_db.close()
            verify_connection.close()

        # Recheck the config gates and advisory lease after the worker's
        # bookkeeping and before reporting an exercised admission.
        validate_canary_context(context, settings)
        result = {
            "status": "PASS",
            "code": "CANARY_ADMISSION_EXERCISED_PROVIDER_FREE",
            "release_commit_sha": target["release_commit_sha"],
            "release_tree_sha": target["release_tree_sha"],
            "schema_revision": "0031",
            "gate_snapshot": {
                "routine_scheduler_canary_enabled": True,
                "routine_pinterest_dry_run": True,
                **{key: False for key in _CLOSED_SETTINGS_GATES},
            },
            "publication_id": target["publication_id"],
            "publication_fingerprint": target["publication_fingerprint"],
            "request_fingerprint": target["request_fingerprint"],
            "route_fingerprint": hashlib.sha256(target["route_id"].encode("utf-8")).hexdigest(),
            "external_calls": 0,
            "counts": {
                "scanned": 1,
                "eligible": 1,
                "claimed": 0,
                "dispatched": 0,
                "published": 0,
                "failed": 0,
                "unknown": 0,
            },
            "row_counts_before": before["counts"],
            "row_counts_after": after_counts,
            "certificate_quota_outcomes": quota_outcomes,
        }
    except RoutineSchedulerCanaryError as exc:
        result = _safe_result(exc.status, exc.code)
    except CanarySafetyError as exc:
        result = _safe_result("BLOCKED", exc.code)
    except Exception:
        result = _safe_result("BLOCKED", "CANARY_UNEXPECTED_FAILURE")
    finally:
        if worker_connection is not None:
            try:
                worker_connection.close()
            except Exception:
                if result and result.get("status") == "PASS":
                    result = _safe_result("BLOCKED", "CANARY_WORKER_CONNECTION_RELEASE_FAILED")
        try:
            lease.release()
        except Exception:
            lease_release_exception = True
            if result and result.get("status") == "PASS":
                result = _safe_result("BLOCKED", "CANARY_LEASE_RELEASE_FAILED")
        if lease_acquire_status == "ACQUIRED":
            try:
                # scheduler_tick recorded this manually acquired lease as the
                # leader. Clear that process-local state after releasing it,
                # without starting or stopping a scheduler task.
                from app.services import routine_pinterest_scheduler
                routine_pinterest_scheduler._record_lease(lease, role="stopped")
            except Exception:
                scheduler_state_reset_failed = True
        release_failed = bool(
            lease_release_exception
            or getattr(lease, "last_error", None)
            or (
                lease_acquire_status == "ACQUIRED"
                and getattr(lease, "last_status", None) != "RELEASED"
            )
            or scheduler_state_reset_failed
        )
        if release_failed and result and result.get("status") == "PASS":
            result = _safe_result("BLOCKED", "CANARY_LEASE_RELEASE_FAILED")

    result["lease"] = {
        "backend": LEASE_BACKEND,
        "backend_pid": lease_backend_pid,
        "acquire_status": lease_acquire_status,
        "release_status": getattr(lease, "last_status", "UNKNOWN"),
        "release_succeeded": bool(
            lease_acquire_status == "ACQUIRED"
            and not lease_release_exception
            and not getattr(lease, "last_error", None)
            and getattr(lease, "last_status", None) == "RELEASED"
            and not scheduler_state_reset_failed
        ),
    }
    return result