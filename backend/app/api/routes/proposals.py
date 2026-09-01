from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from app.schemas.pins import (
    CreativeRenderBatchRequest,
    ProposalDecision,
    ProposalGenerateRequest,
    RegenerationRequest,
    VersionSelectionRequest,
)
from app.services.ai_regeneration import AIRegenerationError, AIRegenerationService
from app.services.ai_creative_generation import (
    AICreativeGenerationError,
    AICreativeGenerationService,
    AIGeneratedAssetStorage,
)
from app.services.creative_rendering import CreativeRenderError, CreativeRenderService, CreativeStorage
from app.services.pin_proposals import PinProposalService


router = APIRouter(prefix="/pins", tags=["pin-proposals"])


@router.get("/summary")
def proposal_summary():
    return PinProposalService().summary()


@router.post("/generate")
def generate_proposals(body: ProposalGenerateRequest):
    service = PinProposalService()
    filters = body.model_dump(
        exclude={"product_limit", "max_proposals_per_product", "dry_run"},
        exclude_none=True,
    )
    return service.generate_controlled_batch(
        product_limit=body.product_limit,
        max_proposals_per_product=body.max_proposals_per_product,
        filters=filters,
        dry_run=body.dry_run,
    )


@router.get("/proposals")
def list_proposals(
    status: str | None = Query(default=None, pattern="^(GENERATED|REVIEW|APPROVED|REJECTED)$"),
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
):
    return {
        "items": PinProposalService().list_proposals(
            status=status,
            search=search,
            limit=limit,
        ),
    }


@router.get("/proposals/qa")
def proposal_qa():
    return PinProposalService().qa_report()


@router.post("/creatives/render")
def render_creatives(body: CreativeRenderBatchRequest):
    """Render a controlled, local-only batch of REVIEW proposals."""
    try:
        return CreativeRenderService().render_review_batch(body.limit)
    except CreativeRenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/creatives/qa")
def creative_qa():
    return CreativeRenderService().qa_report()


@router.get("/proposals/{draft_id}/versions")
def proposal_versions(draft_id: str):
    try:
        return AIRegenerationService().versions(draft_id)
    except AIRegenerationError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/proposals/{draft_id}/versions/{version_id}/preview")
def proposal_version_preview(draft_id: str, version_id: str):
    """Return a non-persisted deterministic preview using the authentic Shopify image."""
    try:
        png = CreativeRenderService().preview_version_png(draft_id, version_id)
        return Response(
            content=png,
            media_type="image/png",
            headers={
                "Cache-Control": "private, no-store",
                "X-Preview-Persistence": "none",
                "X-Publishing-Enabled": "false",
            },
        )
    except CreativeRenderError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 429 if "busy" in str(exc).lower() else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/proposals/{draft_id}/regenerate")
def regenerate_proposal(draft_id: str, body: RegenerationRequest):
    service = AIRegenerationService()
    try:
        if body.kind == "copy":
            variants = [service.regenerate_copy(draft_id) for _ in range(body.count)]
        elif body.kind == "creative":
            if not body.template_key:
                raise AIRegenerationError("A creative template is required for a creative variant.")
            if body.count != 1:
                raise AIRegenerationError("Deterministic creative template variants are created one at a time.")
            variants = [service.regenerate_creative(draft_id, body.template_key)]
        else:
            generation = AICreativeGenerationService()
            if body.kind == "image_background":
                if not body.style_key:
                    raise AICreativeGenerationError("A background style is required.")
                variants = [
                    generation.generate_background(draft_id, body.style_key, body.channel)
                    for _ in range(body.count)
                ]
            else:
                variants = [
                    generation.generate_structured(draft_id, body.kind, body.channel)
                    for _ in range(body.count)
                ]
        return variants[0] if len(variants) == 1 else {"variants": variants}
    except (AIRegenerationError, AICreativeGenerationError, CreativeRenderError) as exc:
        status_code = 404 if "not found" in str(exc).lower() else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/proposals/{draft_id}/active-version")
def select_proposal_version(draft_id: str, body: VersionSelectionRequest):
    try:
        return AIRegenerationService().select_version(draft_id, body.version_id)
    except AIRegenerationError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/creatives/{creative_id}/image")
def creative_image(creative_id: str):
    storage = CreativeStorage()
    try:
        path = storage.path_for(creative_id)
    except CreativeRenderError as exc:
        raise HTTPException(status_code=404, detail="Creative image was not found.") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Creative image was not found.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/ai-assets/{asset_id}/image")
def generated_asset_image(asset_id: str):
    storage = AIGeneratedAssetStorage()
    try:
        path = storage.path_for(asset_id)
    except AICreativeGenerationError as exc:
        raise HTTPException(status_code=404, detail="Generated asset was not found.") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Generated asset was not found.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/proposals/{draft_id}/approve")
def approve_proposal(draft_id: str, body: ProposalDecision | None = None):
    try:
        return PinProposalService().decide(
            draft_id,
            "APPROVED",
            body.note if body else None,
            body.creative_id if body else None,
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "not found" in message.lower() else 409
        raise HTTPException(status_code=status_code, detail=message) from exc


@router.post("/proposals/{draft_id}/reject")
def reject_proposal(draft_id: str, body: ProposalDecision | None = None):
    try:
        return PinProposalService().decide(
            draft_id,
            "REJECTED",
            body.note if body else None,
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "not found" in message.lower() else 409
        raise HTTPException(status_code=status_code, detail=message) from exc
