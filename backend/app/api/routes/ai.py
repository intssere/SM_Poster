from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.pins import AISettingsUpdate
from app.services.ai_regeneration import AIRegenerationError, AISettingsService


router = APIRouter(prefix="/ai", tags=["ai-settings"])


@router.get("/settings")
def get_ai_settings():
    return AISettingsService().get()


@router.put("/settings")
def update_ai_settings(body: AISettingsUpdate):
    try:
        return AISettingsService().update(**body.model_dump())
    except AIRegenerationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc