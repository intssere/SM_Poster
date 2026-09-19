from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import Settings, get_settings
from app.integrations.buffer.gateway import BufferGateway
from app.models.domain import PinPublication, PublicationStatus
from app.services.buffer_execution_preflight import BufferPreflightError
from app.services.publication_scheduler import due_publications
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.routine_buffer_dispatch import RoutineDispatchError, dispatch_routine_buffer, recover_stale_routine_claims
from app.services.routine_buffer_preflight import build_routine_execution_evidence
from app.services.routine_dispatch_authorization import active_permit, validate_permit
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
):
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    if settings.routine_pinterest_worker_enabled is not True:
        return {"status": "WORKER_DISABLED", "dispatched": 0}
    control = get_control(db)
    if control.state == "PAUSED":
        return {"status": "PAUSED", "dispatched": 0, "reason": control.pause_reason}
    mode = "DRY_RUN" if settings.routine_pinterest_dry_run or control.state == "DRY_RUN" else "LIVE"
    if target_publication_id is not None and mode != "DRY_RUN":
        return {"status": "ROUTINE_TARGETED_RUN_DRY_RUN_ONLY", "dispatched": 0}
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
        if target_publication_id is None:
            recover_stale_routine_claims(db, stale_seconds=settings.routine_claim_stale_seconds, now=now)
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
            validated = validate_permit(db, publication, permit, now=now, require_due=True)
            if not validated["valid"]:
                run.skipped += 1
                continue
            run.eligible += 1
            try:
                evidence = await build_routine_execution_evidence(
                    db, publication, settings=settings, gateway=gateway,
                    media_client=media_client, resolver=resolver, now=now,
                )
            except BufferPreflightError:
                run.skipped += 1
                continue
            if mode == "DRY_RUN":
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
        finish_run(db, run, status="SUCCEEDED", error_code=run.error_code)
        return {
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
    except Exception:
        db.rollback()
        try:
            finish_run(db, run, status="FAILED", error_code="ROUTINE_WORKER_UNEXPECTED_ERROR")
        except Exception:
            db.rollback()
        raise
