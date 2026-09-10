from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Depends, Request
from app.db.session import get_db
from app.models.domain import PinCreative
from app.services.public_creative_media import verified_png
from fastapi.responses import FileResponse, Response
from app.services.media_storage import StorageMissing, StorageUnavailable

from app.schemas.pins import (
    CreativeRenderBatchRequest,
    ExactProductProposalRequest,
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


@router.get("/public-creatives/{creative_id}/{digest}.png", operation_id="public_creative_image_get")
@router.head("/public-creatives/{creative_id}/{digest}.png", include_in_schema=False)
def public_creative_image(creative_id: str, digest: str, request: Request, db=Depends(get_db)):
    try:
        with db.no_autoflush:
            row = db.get(PinCreative, creative_id)
            storage = CreativeStorage()
            contents = verified_png(row, digest, storage=storage)
    except StorageUnavailable:
        raise HTTPException(status_code=503, detail="Media storage unavailable.")
    if contents is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=contents if request.method == "GET" else b"", media_type="image/png",
                    headers={"Cache-Control": "public, max-age=31536000, immutable",
                             "Content-Length": str(len(contents)), "X-Content-Type-Options": "nosniff"})


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


@router.post("/generate/exact-product")
def generate_exact_product(body: ExactProductProposalRequest):
    try:
        return {
            **PinProposalService().generate_controlled_batch(
            product_limit=1,
            max_proposals_per_product=body.max_proposals_per_product,
            exact_product_id=body.product_id,
            renderer=CreativeRenderService(),
            ),
            "publishing_enabled": False,
        }
    except (ValueError, CreativeRenderError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
                if body.count != 1:
                    raise AICreativeGenerationError("Image-background variants are generated one at a time.")
                variants = [generation.generate_background(draft_id, body.style_key, body.channel)]
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


@router.post("/proposals/{draft_id}/versions/{revision_id}/reject")
def reject_image_background_revision(draft_id: str, revision_id: str):
    """Reject only a REVIEW generated-background revision; never decide the proposal."""
    try:
        return AIRegenerationService().reject_image_background_revision(draft_id, revision_id)
    except AIRegenerationError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/creatives/{creative_id}/image")
def creative_image(creative_id: str, db=Depends(get_db)):
    try:
        storage = CreativeStorage()
        row = db.get(PinCreative, creative_id) if hasattr(db, "get") else None
        if row is not None and row.sha256:
            contents = storage.read_png(creative_id, row.sha256)
            return Response(content=contents, media_type="image/png", headers={"Cache-Control": "private, no-store"})
        path = storage.path_for(creative_id)
    except StorageUnavailable:
        raise HTTPException(status_code=503, detail="Media storage unavailable.")
    except CreativeRenderError as exc:
        if isinstance(exc.__cause__, StorageUnavailable):
            raise HTTPException(status_code=503, detail="Media storage unavailable.") from exc
        raise HTTPException(status_code=404, detail="Creative image was not found.") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Creative image was not found.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/ai-assets/{asset_id}/image")
def generated_asset_image(asset_id: str, db=Depends(get_db)):
    try:
        storage = AIGeneratedAssetStorage()
        from app.models.domain import AIGeneratedAsset
        row = db.get(AIGeneratedAsset, asset_id) if hasattr(db, "get") else None
        if row is not None and row.sha256:
            contents = storage.read_png(asset_id, row.sha256)
            return Response(content=contents, media_type="image/png", headers={"Cache-Control": "private, no-store"})
        path = storage.path_for(asset_id)
    except StorageUnavailable:
        raise HTTPException(status_code=503, detail="Media storage unavailable.")
    except AICreativeGenerationError as exc:
        if isinstance(exc.__cause__, StorageUnavailable):
            raise HTTPException(status_code=503, detail="Media storage unavailable.") from exc
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


@router.post("/proposals/{draft_id}/return-to-review")
def return_proposal_to_review(draft_id: str):
    try:
        return PinProposalService().return_to_review(draft_id)
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