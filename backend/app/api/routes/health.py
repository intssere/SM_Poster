from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.services.routine_publishing_control import routine_operational_snapshot
from app.services.deployment_attestation import safe_deployment_attestation

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health():
    settings = get_settings()
    database_connected = False
    routine = {"available": False}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        database_connected = True
        db = SessionLocal()
        try:
            snapshot = routine_operational_snapshot(db)
            routine = {
                "available": True,
                "control_state": snapshot["control_state"],
                "latest_run_status": snapshot["latest_run"]["status"] if snapshot["latest_run"] else None,
                "due_backlog": snapshot["due_backlog"],
                "publishing_count": snapshot["publishing_count"],
                "publish_unknown_count": snapshot["publish_unknown_count"],
                "last_provider_mutation_started_at": snapshot["last_provider_mutation_started_at"],
            }
        finally:
            db.close()
    except Exception:
        database_connected = False
        routine = {"available": False}

    return {
        "status": "ok" if database_connected else "degraded",
        "service": "diamond-shelf-pinterest-engine",
        "publishing_enabled": settings.publishing_enabled,
        "database_connected": database_connected,
        "routine_pinterest_worker_enabled": settings.routine_pinterest_worker_enabled,
        "routine_buffer_dispatch_enabled": settings.routine_buffer_dispatch_enabled,
        "routine_pinterest_dry_run": settings.routine_pinterest_dry_run,
        "routine_pinterest_batch_size": settings.routine_pinterest_batch_size,
        "routine_pinterest_daily_write_limit": settings.routine_pinterest_daily_write_limit,
        "routine_publishing": routine,
        "deployment_attestation": safe_deployment_attestation(settings),
    }
