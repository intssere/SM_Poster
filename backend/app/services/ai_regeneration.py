"""Provider-neutral, review-only regeneration with immutable revision lineage."""
from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy import delete, func, select

from app.db.session import SessionLocal
from app.models.domain import (
    AISettings,
    ContentRevision,
    ContentVersionSelection,
    CreativeTemplate,
    DraftStatus,
    PinConcept,
    PinCreative,
    PinDraft,
    Product,
    ProductImage,
    ProductIntelligence,
)
from app.services.creative_rendering import CreativeRenderError, CreativeRenderService, TEMPLATES
from app.services.pin_proposals import CREATIVE_TEMPLATES
from app.services.fingerprints import text_fingerprint


PROVIDER_MODES = ("disabled", "local_free", "hosted_paid")
UNSUPPORTED_CLAIM_PATTERNS = (
    re.compile(r"\b(?:best|#1|number one|popular|trending|viral|bestseller)\b", re.I),
    re.compile(r"\b(?:sale|discount|deal|save|limited time|exclusive)\b", re.I),
    re.compile(r"\b(?:long[- ]lasting|long lasting|all[- ]day|projection|sillage|compliment)\b", re.I),
    re.compile(r"\b(?:mood|seasonal|for summer|for winter|date night)\b", re.I),
)


class AIRegenerationError(ValueError):
    """A safe, user-facing regeneration error."""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _claims(text: str, product: Product) -> list[str]:
    scrubbed = text
    for source in (product.title, product.vendor or ""):
        if source:
            scrubbed = re.sub(re.escape(source), "", scrubbed, flags=re.I)
    return sorted({pattern.pattern for pattern in UNSUPPORTED_CLAIM_PATTERNS if pattern.search(scrubbed)})


def _verified_image(image: ProductImage, product_id: str, rationale: dict[str, Any]) -> dict[str, Any]:
    persisted = rationale.get("authentic_image") or {}
    parsed = urlparse(image.source_url)
    if (
        image.product_id != product_id
        or not image.shopify_media_id
        or not image.editorial_eligible
        or image.source_url != persisted.get("url")
        or parsed.scheme != "https"
        or parsed.hostname != "cdn.shopify.com"
    ):
        raise AIRegenerationError("The proposal does not have a verified authentic Shopify image.")
    return {
        "id": image.id,
        "shopify_media_id": image.shopify_media_id,
        "provenance_url": image.source_url,
        "checksum_sha256": image.source_sha256,
        "verification": "content_checksum_verified" if image.source_sha256 else "persisted_shopify_metadata",
        "checksum_basis": "persisted" if image.source_sha256 else "not_yet_rendered",
        "generated_background": False,
    }


def _facts(product: Product, intelligence: ProductIntelligence, image: ProductImage) -> dict[str, Any]:
    normalized = intelligence.normalized_data or {}
    facts: dict[str, Any] = {
        "title": product.title,
        "product_url": product.product_url,
        "normalization_category": normalized.get("normalization_category", "other"),
        "normalization_status": intelligence.normalization_status or "UNKNOWN",
        "image": image.source_url,
    }
    for field in (
        "brand", "audience", "designer", "niche", "arabian_classification",
        "fragrance_family", "concentration", "size", "price_band", "gift_suitability",
    ):
        value = getattr(intelligence, field, None)
        if value:
            facts[field] = value
    return facts


def _snapshot(
    *,
    draft: PinDraft,
    rationale: dict[str, Any],
    facts: dict[str, Any],
    image: ProductImage,
    creative: PinCreative | None,
    template_key: str | None = None,
) -> dict[str, Any]:
    creative_spec = (creative.render_spec or {}) if creative else {}
    return {
        "headline": rationale.get("headline", ""),
        "title": draft.title,
        "description": draft.description,
        "alt_text": draft.alt_text,
        "cta": rationale.get("cta", "Shop the authentic product"),
        "content_angle": rationale.get("content_angle", ""),
        "content_angle_key": rationale.get("content_angle_key", ""),
        "creative_template": rationale.get("creative_template", ""),
        "creative_template_key": template_key or rationale.get("creative_template_key", ""),
        "destination_url": draft.destination_url,
        "utm_url": draft.utm_url,
        "keywords": list(rationale.get("keywords") or []),
        "facts_used": facts,
        "warnings": list(rationale.get("warnings") or []),
        "missing_facts": list(rationale.get("missing_facts") or []),
        "unsupported_claims": list(rationale.get("unsupported_claims") or []),
        "text_fingerprint": draft.text_fingerprint,
        "creative_fingerprint": creative.creative_fingerprint if creative else None,
        "creative_id": creative.id if creative else None,
        "source_image_id": image.id,
        "provenance": creative_spec.get("image") or _verified_image(image, image.product_id, rationale),
    }


def _copy_variant(
    draft: PinDraft,
    rationale: dict[str, Any],
    product: Product,
    intelligence: ProductIntelligence,
    version: int,
) -> dict[str, Any]:
    vendor = _clean(intelligence.brand or product.vendor)
    title = _clean(product.title)
    angle = _clean(rationale.get("content_angle") or "Editorial Product Pick")
    headline = f"{angle}: {vendor or title} — Alternate {version}"
    copy_title = f"{title} | {angle} (Alternate {version})"
    clauses = [f"Explore {title}{f' by {vendor}' if vendor and vendor.lower() not in title.lower() else ''} at Diamond Shelf."]
    if intelligence.audience:
        clauses.append(f"The catalog lists this product for {intelligence.audience}.")
    if intelligence.concentration:
        clauses.append(f"Listed concentration: {intelligence.concentration}.")
    if intelligence.fragrance_family and (intelligence.normalized_data or {}).get("normalization_category") == "fragrance":
        clauses.append(f"Listed fragrance family: {intelligence.fragrance_family}.")
    if intelligence.size:
        clauses.append(f"Catalog size: {intelligence.size}.")
    if intelligence.gift_suitability:
        clauses.append("The catalog identifies this as a gift set.")
    clauses.append("This alternate edit uses only persisted catalog information.")
    description = " ".join(clauses)[:800].rstrip()
    alt_text = f"{title}{f' by {vendor}' if vendor else ''} — authentic Shopify product image for Diamond Shelf; alternate edit {version}"
    output = {
        "headline": headline[:500].rstrip(),
        "title": copy_title[:500].rstrip(),
        "description": description,
        "alt_text": alt_text[:500].rstrip(),
        "cta": _clean(rationale.get("cta") or "Shop the authentic product"),
    }
    if _claims(" ".join(output.values()), product):
        raise AIRegenerationError("Safe regeneration detected an unsupported claim and was not saved.")
    return output


class AISettingsService:
    def __init__(self, session_factory: Callable = SessionLocal):
        self.session_factory = session_factory

    @staticmethod
    def _row(db: Any) -> AISettings:
        row = db.scalar(select(AISettings).order_by(AISettings.id))
        if not row:
            row = AISettings(enabled=False, provider_mode="disabled", decorative_backgrounds_enabled=False)
            db.add(row)
            db.flush()
        return row

    @staticmethod
    def serialize(row: AISettings) -> dict[str, Any]:
        effective = row.provider_mode if row.enabled else "disabled"
        return {
            "enabled": row.enabled,
            "provider_mode": row.provider_mode,
            "effective_mode": effective,
            "provider_label": {
                "disabled": "AI disabled",
                "local_free": "Local / free provider",
                "hosted_paid": "Paid hosted provider",
            }[effective],
            "available_provider_modes": [
                {"id": "disabled", "label": "Disabled by default", "available": True},
                {"id": "local_free", "label": "Local / free provider", "available": True},
                {"id": "hosted_paid", "label": "Paid hosted provider", "available": False},
            ],
            "capabilities": {
                "copy_regeneration": True,
                "creative_template_variants": True,
                "decorative_backgrounds": False,
                "hosted_provider_configured": False,
            },
            "decorative_backgrounds_enabled": False,
            "credentials_configured": False,
        }

    def get(self) -> dict[str, Any]:
        db = self.session_factory()
        try:
            row = self._row(db)
            db.commit()
            return self.serialize(row)
        finally:
            db.close()

    def update(self, *, enabled: bool, provider_mode: str, decorative_backgrounds_enabled: bool) -> dict[str, Any]:
        if provider_mode not in PROVIDER_MODES:
            raise AIRegenerationError("Unsupported AI provider mode.")
        if decorative_backgrounds_enabled:
            raise AIRegenerationError("Decorative AI backgrounds are reserved for a future safe provider.")
        db = self.session_factory()
        try:
            row = self._row(db)
            row.enabled = bool(enabled and provider_mode != "disabled")
            row.provider_mode = provider_mode
            row.decorative_backgrounds_enabled = False
            db.commit()
            return self.serialize(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class AIRegenerationService:
    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        creative_renderer: CreativeRenderService | None = None,
    ):
        self.session_factory = session_factory
        self.creative_renderer = creative_renderer or CreativeRenderService(session_factory)

    def _settings(self, db: Any) -> AISettings:
        return AISettingsService._row(db)

    @staticmethod
    def _provider(row: AISettings) -> tuple[str, str]:
        effective = row.provider_mode if row.enabled else "disabled"
        if effective == "hosted_paid":
            raise AIRegenerationError("Paid hosted AI is not configured; no credentials are stored by this foundation.")
        return effective, "deterministic_fallback" if effective == "disabled" else "local_free_deterministic"

    def _source(self, db: Any, draft_id: str) -> tuple[PinDraft, PinConcept, Product, ProductIntelligence, ProductImage, dict[str, Any], PinCreative | None]:
        row = db.execute(
            select(PinDraft, PinConcept, Product, ProductIntelligence)
            .join(PinConcept, PinConcept.id == PinDraft.concept_id)
            .join(Product, Product.id == PinConcept.product_id)
            .join(ProductIntelligence, ProductIntelligence.product_id == Product.id)
            .where(PinDraft.id == draft_id)
            .with_for_update()
        ).first()
        if not row:
            raise AIRegenerationError("Proposal was not found.")
        draft, concept, product, intelligence = row
        if draft.status != DraftStatus.READY_FOR_REVIEW:
            raise AIRegenerationError("Only proposals in REVIEW can be regenerated.")
        rationale = concept.rationale or {}
        image = db.get(ProductImage, (rationale.get("authentic_image") or {}).get("id"))
        if not image:
            raise AIRegenerationError("The proposal's persisted product image was not found.")
        _verified_image(image, product.id, rationale)
        original = db.scalar(select(PinCreative).where(PinCreative.draft_id == draft.id).order_by(PinCreative.created_at.asc(), PinCreative.id))
        return draft, concept, product, intelligence, image, rationale, original

    @staticmethod
    def _parent(db: Any, draft_id: str) -> ContentRevision | None:
        selection = db.scalar(select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft_id))
        if selection:
            return db.get(ContentRevision, selection.revision_id)
        return None

    @staticmethod
    def _next_version(db: Any, draft_id: str) -> int:
        current = db.scalar(select(func.max(ContentRevision.version)).where(ContentRevision.draft_id == draft_id)) or 1
        return int(current) + 1

    def regenerate_copy(self, draft_id: str) -> dict[str, Any]:
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self._source(db, draft_id)
            provider_mode, generation_mode = self._provider(self._settings(db))
            version = self._next_version(db, draft.id)
            copy = _copy_variant(draft, rationale, product, intelligence, version)
            facts = _facts(product, intelligence, image)
            parent = self._parent(db, draft.id)
            revision = ContentRevision(
                draft_id=draft.id,
                parent_revision_id=parent.id if parent else None,
                version=version,
                revision_kind="COPY",
                status="REVIEW",
                **copy,
                content_angle=rationale.get("content_angle", ""),
                content_angle_key=rationale.get("content_angle_key", ""),
                creative_template=rationale.get("creative_template", ""),
                creative_template_key=rationale.get("creative_template_key", ""),
                destination_url=draft.destination_url,
                utm_url=draft.utm_url,
                keywords=list(rationale.get("keywords") or []),
                facts_used=facts,
                warnings=list(rationale.get("warnings") or []),
                missing_facts=list(rationale.get("missing_facts") or []),
                unsupported_claims=[],
                provenance=_verified_image(image, product.id, rationale),
                text_fingerprint=text_fingerprint(
                    title=copy["title"], description=copy["description"], alt_text=copy["alt_text"]
                ),
                creative_fingerprint=None,
                creative_id=None,
                source_image_id=image.id,
                provider_mode=provider_mode,
                generation_mode=generation_mode,
                reason="copy_regeneration",
            )
            db.add(revision)
            db.commit()
            return self.revision_payload(revision, None, None)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def regenerate_creative(self, draft_id: str, template_key: str) -> dict[str, Any]:
        if template_key not in TEMPLATES:
            raise AIRegenerationError("Unsupported creative template.")
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self._source(db, draft_id)
            provider_mode, generation_mode = self._provider(self._settings(db))
            parent = self._parent(db, draft.id)
            if parent:
                base = {
                    "headline": parent.headline,
                    "title": parent.title,
                    "description": parent.description,
                    "alt_text": parent.alt_text,
                    "cta": parent.cta,
                    "content_angle": parent.content_angle,
                    "content_angle_key": parent.content_angle_key,
                    "creative_template": parent.creative_template,
                    "creative_template_key": parent.creative_template_key,
                    "destination_url": parent.destination_url,
                    "utm_url": parent.utm_url,
                    "keywords": parent.keywords,
                    "facts_used": parent.facts_used,
                    "warnings": parent.warnings,
                    "missing_facts": parent.missing_facts,
                    "unsupported_claims": parent.unsupported_claims,
                    "text_fingerprint": parent.text_fingerprint,
                }
            else:
                base = _snapshot(
                    draft=draft, rationale=rationale, facts=_facts(product, intelligence, image),
                    image=image, creative=original,
                )
            if template_key == base["creative_template_key"]:
                raise AIRegenerationError("Choose a different creative template for a new variant.")
            render_result = self.creative_renderer.render_variant(
                draft.id,
                template_key,
                snapshot=base,
                db=db,
            )
            creative = db.get(PinCreative, render_result["creative_id"])
            if not creative:
                raise AIRegenerationError("The rendered creative variant was not found.")
            version = self._next_version(db, draft.id)
            revision = ContentRevision(
                draft_id=draft.id,
                parent_revision_id=parent.id if parent else None,
                version=version,
                revision_kind="CREATIVE",
                status="REVIEW",
                headline=base["headline"],
                title=base["title"],
                description=base["description"],
                alt_text=base["alt_text"],
                cta=base["cta"],
                content_angle=base["content_angle"],
                content_angle_key=base["content_angle_key"],
                creative_template=CREATIVE_TEMPLATES.get(template_key, template_key),
                creative_template_key=template_key,
                destination_url=draft.destination_url,
                utm_url=draft.utm_url,
                keywords=base["keywords"],
                facts_used=base["facts_used"],
                warnings=base["warnings"],
                missing_facts=base["missing_facts"],
                unsupported_claims=[],
                provenance={
                    **((creative.render_spec or {}).get("image") or _verified_image(image, product.id, rationale)),
                    "generated_background": False,
                    "background_source": "deterministic_template_tokens",
                },
                text_fingerprint=base["text_fingerprint"],
                creative_fingerprint=creative.creative_fingerprint,
                creative_id=creative.id,
                source_image_id=image.id,
                provider_mode=provider_mode,
                generation_mode=generation_mode,
                reason="creative_template_variant",
            )
            db.add(revision)
            db.commit()
            return self.revision_payload(revision, creative, None)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def creative_payload(creative: PinCreative | None) -> dict[str, Any] | None:
        if not creative:
            return None
        return {
            "id": creative.id,
            "status": creative.render_status,
            "image_url": creative.rendered_url,
            "error": creative.render_error,
            "width": creative.width,
            "height": creative.height,
            "size_bytes": creative.size_bytes,
            "render_duration_ms": creative.render_duration_ms,
            "duration_ms": creative.render_duration_ms,
            "creative_fingerprint": creative.creative_fingerprint,
            "sha256": creative.sha256,
            "template_version": (creative.render_spec or {}).get("template_version"),
            "specification": creative.render_spec,
        }

    @classmethod
    def revision_payload(cls, revision: ContentRevision, creative: PinCreative | None, active_id: str | None) -> dict[str, Any]:
        return {
            "id": revision.id,
            "version": revision.version,
            "kind": revision.revision_kind,
            "status": revision.status,
            "parent_revision_id": revision.parent_revision_id,
            "active": revision.id == active_id,
            "headline": revision.headline,
            "title": revision.title,
            "description": revision.description,
            "alt_text": revision.alt_text,
            "cta": revision.cta,
            "creative_template": revision.creative_template,
            "creative_template_key": revision.creative_template_key,
            "text_fingerprint": revision.text_fingerprint,
            "creative_fingerprint": revision.creative_fingerprint,
            "facts_used": revision.facts_used,
            "warnings": revision.warnings,
            "missing_facts": revision.missing_facts,
            "unsupported_claims": revision.unsupported_claims,
            "provenance": revision.provenance,
            "provider_mode": revision.provider_mode,
            "generation_mode": revision.generation_mode,
            "reason": revision.reason,
            "created_at": revision.created_at,
            "creative": cls.creative_payload(creative),
        }

    def versions(self, draft_id: str) -> dict[str, Any]:
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self._source(db, draft_id)
            selection = db.scalar(select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft.id))
            active_id = selection.revision_id if selection else None
            facts = rationale.get("facts_used") or _facts(product, intelligence, image)
            original_snapshot = _snapshot(
                draft=draft, rationale=rationale, facts=facts, image=image, creative=original
            )
            originals = {
                "id": None, "version": 1, "kind": "ORIGINAL", "status": "REVIEW",
                "parent_revision_id": None, "active": active_id is None,
                **{key: original_snapshot[key] for key in ("headline", "title", "description", "alt_text", "cta", "creative_template", "creative_template_key", "text_fingerprint", "creative_fingerprint", "facts_used", "warnings", "missing_facts", "unsupported_claims", "provenance")},
                "provider_mode": "deterministic_original", "generation_mode": "original_persisted",
                "reason": "original_persisted", "created_at": draft.created_at,
                "creative": self.creative_payload(original),
            }
            revisions = []
            for revision in db.scalars(
                select(ContentRevision).where(ContentRevision.draft_id == draft.id).order_by(ContentRevision.version)
            ):
                revisions.append(self.revision_payload(
                    revision, db.get(PinCreative, revision.creative_id) if revision.creative_id else None, active_id
                ))
            return {"draft_id": draft.id, "active_version": selection.revision_id if selection else None, "active_version_number": selection and db.get(ContentRevision, selection.revision_id).version or 1, "versions": [originals, *revisions], "publishing_enabled": False}
        finally:
            db.close()

    def select_version(self, draft_id: str, version_id: str) -> dict[str, Any]:
        db = self.session_factory()
        try:
            draft = db.get(PinDraft, draft_id)
            if not draft:
                raise AIRegenerationError("Proposal was not found.")
            if draft.status != DraftStatus.READY_FOR_REVIEW:
                raise AIRegenerationError("Only proposals in REVIEW can select a version.")
            existing = db.scalar(select(ContentVersionSelection).where(ContentVersionSelection.draft_id == draft_id))
            if version_id == "original":
                if existing:
                    db.delete(existing)
            else:
                revision = db.get(ContentRevision, version_id)
                if not revision or revision.draft_id != draft_id:
                    raise AIRegenerationError("Revision was not found for this proposal.")
                if revision.status != "REVIEW":
                    raise AIRegenerationError("Only revisions in REVIEW can become active.")
                if existing:
                    existing.revision_id = revision.id
                    existing.selected_by = "manual_dashboard_action"
                else:
                    db.add(ContentVersionSelection(
                        draft_id=draft_id,
                        revision_id=revision.id,
                        selected_by="manual_dashboard_action",
                    ))
            db.commit()
            return self.versions(draft_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()