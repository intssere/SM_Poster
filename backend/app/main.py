import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.health import router as health_router
from app.api.routes.utilities import router as utilities_router
from app.api.routes.phase1 import router as phase1_router
from app.api.routes.catalog import router as catalog_router
from app.api.routes.proposals import router as proposals_router
from app.api.routes.channels import router as channels_router

cors_origins = ["http://localhost:5000", "http://127.0.0.1:5000"]
if replit_domain := os.getenv("REPLIT_DEV_DOMAIN"):
    cors_origins.append(f"https://{replit_domain}")

app = FastAPI(title="Diamond Shelf Social Studio", version="0.1.0-phase0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health_router, prefix="/api")
app.include_router(utilities_router, prefix="/api")
app.include_router(phase1_router, prefix="/api")
app.include_router(catalog_router, prefix="/api")
app.include_router(proposals_router, prefix="/api")
app.include_router(channels_router, prefix="/api")


@app.get("/")
def root():
    return {
        "name": "Diamond Shelf Social Studio",
        "legacy_name": "Diamond Shelf Pinterest Engine",
        "phase": 0,
        "message": "Social Studio foundation active. Pinterest remains internal-preview only and production publishing is disabled.",
    }
