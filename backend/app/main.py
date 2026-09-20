from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.middleware import AdminAuthMiddleware
from app.services.multichannel_generation_contract import install_multichannel_generation_contract
from app.services.routine_pinterest_scheduler import start_scheduler, stop_scheduler

install_multichannel_generation_contract()

from app.api.routes.health import router as health_router
from app.api.routes.utilities import router as utilities_router
from app.api.routes.phase1 import router as phase1_router
from app.api.routes.catalog import router as catalog_router
from app.api.routes.proposals import router as proposals_router
from app.api.routes.channels import router as channels_router
from app.api.routes.ai import router as ai_router
from app.api.routes.auth import router as auth_router
from app.api.routes.publications import router as publications_router
from app.api.routes.buffer_reconciliation_discovery import router as buffer_reconciliation_discovery_router
from app.api.routes.buffer_reconciliation import router as buffer_reconciliation_router
from app.api.routes.routine_publishing import router as routine_publishing_router
from app.api.routes.portfolio import router as portfolio_router

cors_origins = get_settings().allowed_origins


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await start_scheduler(settings=settings)
    try:
        yield
    finally:
        await stop_scheduler()


app = FastAPI(title="Diamond Shelf Social Studio", version="0.1.0-phase0", lifespan=lifespan)
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)
app.include_router(health_router, prefix="/api")
app.include_router(utilities_router, prefix="/api")
app.include_router(phase1_router, prefix="/api")
app.include_router(catalog_router, prefix="/api")
app.include_router(proposals_router, prefix="/api")
app.include_router(channels_router, prefix="/api")
app.include_router(ai_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(publications_router, prefix="/api")
app.include_router(buffer_reconciliation_discovery_router, prefix="/api")
app.include_router(buffer_reconciliation_router, prefix="/api")
app.include_router(routine_publishing_router, prefix="/api")
app.include_router(portfolio_router, prefix="/api")


@app.get("/")
def root():
    return {
        "name": "Diamond Shelf Social Studio",
        "legacy_name": "Diamond Shelf Pinterest Engine",
        "phase": 0,
        "message": "Multi-channel content generation and review are active. Pinterest remains the currently implemented publishing channel; other social connections stay unavailable until separately implemented.",
    }
