from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.schemas.pins import CreativeRenderBatchRequest, ProposalDecision, ProposalGenerateRequest
from app.services.creative_rendering import CreativeRenderError, CreativeRenderService
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


@router.get("/creatives/{creative_id}/image")
def creative_image(creative_id: str):
    storage = CreativeRenderService().storage
    try:
        path = storage.path_for(creative_id)
    except CreativeRenderError as exc:
        raise HTTPException(status_code=404, detail="Creative image was not found.") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Creative image was not found.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/proposals/{draft_id}/approve")
def approve_proposal(draft_id: str, body: ProposalDecision | None = None):
    try:
        return PinProposalService().decide(
            draft_id,
            "APPROVED",
            body.note if body else None,
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