from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.db.session import get_db
from app.services.buffer_unknown_reconciliation_discovery import (
    BufferUnknownDiscoveryError,
    discover_buffer_unknown,
)


router = APIRouter(prefix="/publications", tags=["publications"])


@router.get("/{publication_id}/buffer-unknown-discovery")
async def buffer_unknown_discovery(publication_id: str, request: Request, db: Session = Depends(get_db)):
    """Protected, read-only Buffer discovery for a no-operation-id UNKNOWN attempt."""
    if not current_user(request):
        raise HTTPException(401, "Authentication required")
    try:
        return await discover_buffer_unknown(db, publication_id)
    except BufferUnknownDiscoveryError as exc:
        raise HTTPException(409, str(exc)) from None
