from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health():
    settings = get_settings()
    database_connected = False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        database_connected = True
    except Exception:
        database_connected = False

    return {
        "status": "ok" if database_connected else "degraded",
        "service": "diamond-shelf-pinterest-engine",
        "publishing_enabled": settings.publishing_enabled,
        "database_connected": database_connected,
    }
