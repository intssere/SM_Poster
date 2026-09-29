from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.integrations.buffer.gateway import BufferGateway
from app.models.domain import PinPublication, PinterestPortfolioPlanItem, PublicationStatus
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.buffer_execution_preflight import BufferPreflightError
from app.services.publication_scheduler import due_publications, request_fingerprint_for
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.routine_buffer_dispatch import RoutineDispatchError, dispatch_routine_buffer, recover_stale_routine_claims
from app.services.routine_buffer_preflight import build_routine_execution_evidence
from app.services.routine_offline_preflight import build_routine_offline_evidence, RoutineOfflinePreflightError
from app.services.routine_dispatch_authorization import active_permit, validate_permit
from app.services.routine_scheduled_admission import admit_scheduled_publication
from app.services.routine_scheduled_quotas import ScheduledQuotaError
from app.services.scheduled_autonomous_readiness import scheduled_autonomous_readiness
from app.services.routine_scheduler_canary_context import (
    CanarySafetyError,
    RoutineSchedulerCanaryContext,
    validate_canary_context,
)
from app.services.routine_publishing_control import (
    RoutineControlError,
    daily_provider_write_count,
    finish_run,
    get_control,
    heartbeat_run,
    start_run,
)


async def run_once(
    db,
    *,
    settings: Settings | None = None,
    gateway: BufferGateway | None = None,
    media_client=None,
    resolver=None,
    now=None,
    target_publication_id: str | None = None,
    target_permit_id: str | None = None,
    allow_targeted_live: bool = False,
    certified_run=None,
    canary_context: RoutineSchedulerCanaryContext | None = None,
):
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    if canary_context is not None:
        validate_canary_context(canary_context, settings)
        if (
            gateway is not None or media_client is not None or resolver is not None
            or allow_targeted_live or certified_run is not None
            or target_publication_id not in (None, canary_context.target_publication_id)
            or target_permit_id not in (None, canary_context.target_permit_id)
        ):
            raise CanarySafetyError("CANARY_PROVIDER_OR_TARGETED_LIVE_PATH_FORBIDDEN")
        target_publication_id = canary_context.target_publication_id
        target_permit_id = canary_context.target_permit_id
    if settings.routine_pinterest_worker_enabled is not True and canary_context is None:
        return {"status": "WORKER_DISABLED", "dispatched": 0}
    control = get_control(db, create=canary_context is None)
    if control is None:
        if canary_context is not None:
            return {"status": "NOT_EXERCISED", "reason": "CANARY_CONTROL_ROW_MISSING", "dispatched": 0}
        return {"status": "PAUSED", "dispatched": 0}
    if control.state == "PAUSED":
        if canary_context is None:
            return {"status": "PAUSED", "dispatched": 0, "reason": control.pause_reason}
    if canary_context is not None and control.state != "PAUSED":
        raise CanarySafetyError("CANARY_CONTROL_STATE_NOT_PAUSED")
    mode = "DRY_RUN" if settings.routine_pinterest_dry_run or control.state == "DRY_RUN" else "LIVE"
    if canary_context is not None and mode != "DRY_RUN":
        raise CanarySafetyError("CANARY_DRY_RUN_REQUIRED")
    if settings.routine_scheduled_autonomy_enabled and mode != "DRY_RUN":
        return {"status": "SCHEDULED_AUTONOMY_LIVE_DISABLED", "dispatched": 0}
    if target_publication_id is not None and mode != "DRY_RUN" and not allow_targeted_live:
        return {"status": "ROUTINE_TARGETED_RUN_DRY_RUN_ONLY", "dispatched": 0}
    if allow_targeted_live and (target_publication_id is None or mode != "LIVE"):
        return {"status": "ROUTINE_TARGETED_LIVE_INVALID_CONTEXT", "dispatched": 0}
    if allow_targeted_live and certified_run is None:
        return {"status": "CERTIFIED_LIVE_RESERVATION_REQUIRED", "dispatched": 0}
    if certified_run is not None:
        db.refresh(certified_run)
        binding = certified_run.metadata_json or {}
        if (certified_run.status != "RUNNING" or mode != "LIVE" or not allow_targeted_live
                or binding.get("publication_id") != target_publication_id
                or binding.get("permit_id") != target_permit_id
                or binding.get("certified_live_version") != "ROUTINE_CERTIFIED_LIVE_PREFLIGHT_V1"):
            return {"status": "CERTIFIED_LIVE_RUN_MISMATCH", "dispatched": 0}
        run = certified_run
    else:
        if canary_context is not None:
            # Insert only after all target bindings are verified. The control
            # service disables stale recovery and stamps an idempotency marker.
            run = None
        else:
            try:
                run = start_run(
                    db,
                    mode=mode,
                    now=now,
                    stale_seconds=settings.routine_claim_stale_seconds,
                )
            except RoutineControlError as exc:
                return {"status": str(exc), "dispatched": 0}
    try:
        if canary_context is not None:
            # Candidate and permit ambiguity must not leave a run receipt behind.
            candidates = due_publications(db, now=now, limit=2)
            if len(candidates) != 1 or candidates[0].id != canary_context.target_publication_id:
                return {"status": "NOT_EXERCISED", "reason": "CANARY_DUE_CANDIDATE_AMBIGUOUS", "dispatched": 0}
            publication = candidates[0]
            permit_rows = list(db.scalars(
                select(RoutineDispatchPermit)
                .where(
                    RoutineDispatchPermit.publication_id == publication.id,
                    RoutineDispatchPermit.status == "ACTIVE",
                )
                .order_by(RoutineDispatchPermit.authorized_at.desc(), RoutineDispatchPermit.id)
                .limit(2)
            ).all())
            if len(permit_rows) != 1 or permit_rows[0].id != canary_context.target_permit_id:
                return {"status": "NOT_EXERCISED", "reason": "CANARY_PERMIT_AMBIGUOUS", "dispatched": 0}
            permit = permit_rows[0]
            validated = validate_permit(db, publication, permit, now=now, require_due=True)
            if not validated["valid"]:
                return {"status": "NOT_EXERCISED", "reason": validated["status"], "dispatched": 0}
            if (
                publication.publication_fingerprint != canary_context.expected_publication_fingerprint
                or request_fingerprint_for(publication) != canary_context.expected_request_fingerprint
                or publication.pinterest_board_record_id != canary_context.expected_route_id
                or permit.publication_fingerprint != canary_context.expected_publication_fingerprint
                or permit.request_fingerprint != canary_context.expected_request_fingerprint
                or permit.pinterest_board_record_id != canary_context.expected_route_id
            ):
                return {"status": "NOT_EXERCISED", "reason": "CANARY_TARGET_FINGERPRINT_OR_ROUTE_MISMATCH", "dispatched": 0}
            plan_items = list(db.scalars(
                select(PinterestPortfolioPlanItem)
                .where(PinterestPortfolioPlanItem.publication_id == publication.id)
                .order_by(PinterestPortfolioPlanItem.id)
                .limit(2)
            ).all())
            if len(plan_items) != 1:
                return {"status": "NOT_EXERCISED", "reason": "CANARY_PLAN_BINDING_AMBIGUOUS", "dispatched": 0}
            preflight_certificate = scheduled_autonomous_readiness(
                db, plan_items[0].id, settings=settings, now=now,
                canary_context=canary_context,
            )
            route_check = next(
                (item for item in preflight_certificate["checks"]
                 if item["code"] == "PERSISTED_BOARD_ROUTE_UNAMBIGUOUS"),
                None,
            )
            binding_check = next(
                (item for item in preflight_certificate["checks"]
                 if item["code"] == "CANARY_TARGET_BINDING_CURRENT"),
                None,
            )
            if not (route_check and route_check["passed"] and binding_check and binding_check["passed"]):
                return {"status": "NOT_EXERCISED", "reason": "CANARY_ROUTE_OR_TARGET_BINDING_INVALID", "dispatched": 0}
            validate_canary_context(canary_context, settings)
            try:
                run = start_run(
                    db,
                    mode="DRY_RUN",
                    now=now,
                    stale_seconds=settings.routine_claim_stale_seconds,
                    recover_stale=False,
                    metadata_json={
                        "scheduler_canary": {
                            "key": canary_context.idempotency_key,
                            "target_publication_id": canary_context.target_publication_id,
                            "publication_fingerprint": canary_context.expected_publication_fingerprint,
                            "request_fingerprint": canary_context.expected_request_fingerprint,
                            "route_id": canary_context.expected_route_id,
                            "lease_backend_pid": getattr(canary_context.lease, "backend_pid", None),
                        },
                    },
                )
            except RoutineControlError as exc:
                return {"status": "NOT_EXERCISED", "reason": str(exc), "dispatched": 0}
            candidates = [publication]
        elif target_publication_id is None:
            # Scheduled-autonomy DRY_RUN must only observe existing publications.
            # Recovery changes publication/attempt state and belongs to the
            # separately gated live worker, never to this provider-free gate.
            if canary_context is None and not settings.routine_scheduled_autonomy_enabled:
                recover_stale_routine_claims(
                    db, stale_seconds=settings.routine_claim_stale_seconds, now=now,
                )
            candidates = due_publications(db, now=now, limit=settings.routine_pinterest_batch_size)
        else:
            publication = db.get(PinPublication, target_publication_id)
            scheduled_for = normalize_persisted_utc(publication.scheduled_for) if publication and publication.scheduled_for else None
            candidates = [publication] if (
                publication
                and publication.status == PublicationStatus.SCHEDULED
                and scheduled_for is not None
                and scheduled_for <= normalize_persisted_utc(now)
            ) else []
        run.scanned = len(candidates)
        heartbeat_run(db, run, now=now)
        day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        for publication in candidates:
            heartbeat_run(db, run)
            db.refresh(publication)
            permit = active_permit(db, publication.id)
            if target_permit_id is not None and (permit is None or permit.id != target_permit_id):
                run.skipped += 1
                run.error_code = "ROUTINE_PERMIT_ID_MISMATCH"
                continue
            validated = validate_permit(db, publication, permit, now=now, require_due=True)
            if not validated["valid"]:
                run.skipped += 1
                run.error_code = validated["status"]
                continue
            run.eligible += 1
            if mode == "DRY_RUN":
                certificate = None
                if settings.routine_scheduled_autonomy_enabled or canary_context is not None:
                    items = list(db.scalars(
                        select(PinterestPortfolioPlanItem)
                        .where(PinterestPortfolioPlanItem.publication_id == publication.id)
                        .limit(2)
                    ).all())
                    if len(items) != 1:
                        run.skipped += 1
                        run.error_code = "SCHEDULED_AUTONOMY_PLAN_BINDING_REQUIRED"
                        continue
                    certificate = scheduled_autonomous_readiness(
                        db, items[0].id, settings=settings, now=now,
                        canary_context=canary_context,
                    )
                    admission_ready = False
                    admission_reason = None
                    if certificate["ready"]:
                        try:
                            if canary_context is not None:
                                validate_canary_context(canary_context, settings)
                            # Exercise the actual PostgreSQL row lock, reconciliation,
                            # reservation, and claim CAS. Roll back *all* of them before
                            # the run receipt is persisted; no permit is consumed.
                            try:
                                with db.begin_nested() as preview:
                                    admit_scheduled_publication(
                                        db, publication_id=publication.id,
                                        plan_item_id=items[0].id,
                                        settings=settings, now=now,
                                    )
                                    preview.rollback()
                            finally:
                                if canary_context is not None:
                                    validate_canary_context(canary_context, settings)
                            admission_ready = True
                        except (ScheduledQuotaError, ValueError) as exc:
                            admission_reason = getattr(exc, "code", str(exc))
                    blockers = list(certificate["blockers"])
                    if certificate["ready"] and not admission_ready:
                        blockers.append("SCHEDULED_QUOTA_ATOMIC_ADMISSION")
                    quota_check = next(
                        (check for check in certificate["checks"]
                         if check["code"] == "SCHEDULED_QUOTA_HEADROOM"),
                        None,
                    )
                    if blockers:
                        run.skipped += 1
                        run.error_code = blockers[0]
                    run.metadata_json = {
                        **(run.metadata_json or {}),
                        "scheduled_autonomy_certificates": [
                            *((run.metadata_json or {}).get("scheduled_autonomy_certificates") or []),
                            {
                                "publication_id": publication.id,
                                "portfolio_item_id": items[0].id,
                                "fingerprint": certificate["certificate_fingerprint"],
                                "ready": certificate["ready"] and admission_ready,
                                "blockers": blockers,
                                **({
                                    "quota_decisions": {
                                        "passed": quota_check["passed"],
                                        "headroom": quota_check["context"].get("headroom"),
                                        "reason": quota_check["context"].get("reason"),
                                    } if quota_check else None,
                                } if canary_context is not None else {}),
                                "atomic_admission": {
                                    "evaluated": certificate["ready"],
                                    "would_admit": admission_ready,
                                    "reason": admission_reason,
                                    "claim_committed": False,
                                    "reservation_committed": False,
                                },
                                "external_requests": 0,
                            },
                        ],
                    }
                    if blockers:
                        continue
                try:
                    if canary_context is not None:
                        validate_canary_context(canary_context, settings)
                    offline = build_routine_offline_evidence(
                        db, publication, permit=permit, now=now,
                    )
                    if certificate is not None:
                        record = (run.metadata_json or {})["scheduled_autonomy_certificates"][-1]
                        run.metadata_json = {
                            **(run.metadata_json or {}),
                            "scheduled_autonomy_certificates": [
                                *((run.metadata_json or {})["scheduled_autonomy_certificates"][:-1]),
                                {**record, "offline_validated": True,
                                 "external_requests": offline.external_requests},
                            ],
                        }
                except RoutineOfflinePreflightError as exc:
                    run.skipped += 1
                    run.error_code = str(exc)
                continue
            try:
                evidence = await build_routine_execution_evidence(
                    db, publication, settings=settings, gateway=gateway,
                    media_client=media_client, resolver=resolver, now=now,
                )
            except BufferPreflightError as exc:
                run.skipped += 1
                run.error_code = str(exc)
                continue
            if daily_provider_write_count(db, day_start=day_start) >= settings.routine_pinterest_daily_write_limit:
                run.error_code = "ROUTINE_DAILY_WRITE_LIMIT_REACHED"
                break
            try:
                result = await dispatch_routine_buffer(
                    db, publication, evidence=evidence, settings=settings, gateway=gateway, now=now,
                )
                run.claimed += 1
                run.dispatched += 1
                if result.status == PublicationStatus.PUBLISHED:
                    run.published += 1
                elif result.status == PublicationStatus.PUBLISH_FAILED:
                    run.failed += 1
                elif result.status == PublicationStatus.PUBLISH_UNKNOWN:
                    run.unknown += 1
                    run.error_code = "PUBLISH_UNKNOWN_CIRCUIT_BREAKER"
                    break
            except RoutineDispatchError as exc:
                run.skipped += 1
                run.error_code = str(exc)
                continue
        if canary_context is not None:
            validate_canary_context(canary_context, settings)
        finish_run(db, run, status="SUCCEEDED", error_code=run.error_code)
        result = {
            "status": "SUCCEEDED",
            "mode": mode,
            "run_id": run.id,
            "scanned": run.scanned,
            "eligible": run.eligible,
            "skipped": run.skipped,
            "claimed": run.claimed,
            "dispatched": run.dispatched,
            "published": run.published,
            "failed": run.failed,
            "unknown": run.unknown,
            "error_code": run.error_code,
        }
        if canary_context is not None:
            result["canary_evidence"] = {
                "scheduler_canary": (run.metadata_json or {}).get("scheduler_canary"),
                "scheduled_autonomy_certificates": (
                    (run.metadata_json or {}).get("scheduled_autonomy_certificates") or []
                ),
                "provider_calls": 0,
                "claim_committed": False,
                "reservation_committed": False,
            }
        return result
    except Exception:
        db.rollback()
        try:
            if canary_context is not None:
                validate_canary_context(canary_context, settings)
            finish_run(db, run, status="FAILED", error_code="ROUTINE_WORKER_UNEXPECTED_ERROR")
        except Exception:
            db.rollback()
        raise
