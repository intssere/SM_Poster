"""Fail-closed admission checks for local canary media awaiting promotion."""

from types import SimpleNamespace

from sqlalchemy import select

from app.models.domain import PinCreative, PinPublication
from app.services.pinterest_local_canary_media import (
    durable_media_required,
    has_local_canary_media_marker,
    read_verified_promoted,
)


def pending_local_canary_creative_ids():
    """Return a composable query for creatives whose staged media is not promoted."""
    return select(PinCreative.id).where(PinCreative.render_status == "STAGED")


def publication_has_pending_local_canary_media(
    db, publication_or_id, *, settings=None, storage=None
) -> bool:
    """Check only this publication's local artifact, leaving history untouched."""
    if isinstance(publication_or_id, PinPublication):
        creative_id = publication_or_id.creative_id
    elif isinstance(publication_or_id, str):
        creative_id = db.scalar(
            select(PinPublication.creative_id).where(
                PinPublication.id == publication_or_id
            )
        )
    else:
        # Lightweight test doubles and unrelated objects are not database
        # identities; callers should only pass ORM publications or persisted IDs.
        return False
    if not creative_id:
        return False
    row = db.execute(
        select(
            PinCreative.id,
            PinCreative.render_status,
            PinCreative.render_spec,
            PinCreative.sha256,
            PinCreative.source_image_id,
        ).where(PinCreative.id == creative_id)
    ).one_or_none()
    if row is None:
        return False
    creative = SimpleNamespace(
        id=row.id,
        render_status=row.render_status,
        render_spec=row.render_spec,
        sha256=row.sha256,
        source_image_id=row.source_image_id,
    )
    if not has_local_canary_media_marker(creative):
        return False
    if creative.render_status != "RENDERED":
        return True
    try:
        # Preserve the small ordinary-media projection. Full authoritative
        # provenance is required only for a marked canary, never optional.
        if durable_media_required(settings):
            binding = db.execute(
                select(
                    PinCreative.creative_fingerprint,
                    PinCreative.size_bytes.label("rendered_size"),
                ).where(PinCreative.id == creative_id)
            ).one_or_none()
            if binding is None:
                return True
            creative.creative_fingerprint = binding.creative_fingerprint
            creative.rendered_size = binding.rendered_size
        read_options = {}
        if settings is not None:
            read_options["settings"] = settings
        if storage is not None:
            read_options["storage"] = storage
        read_verified_promoted(
            creative, digest=creative.sha256, **read_options
        )
    except Exception:
        # A persisted local receipt with missing, corrupt, or inaccessible bytes
        # is never sufficient to admit this publication.
        return True
    return False