"""Review-only AI content, generated-background, and video specification variants."""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from PIL import Image
from sqlalchemy import func, select, text

from app.db.session import SessionLocal
from app.models.domain import AIGeneratedAsset, AIRequestTelemetry, ContentRevision, PinCreative
from app.services.ai_providers import (
    ProviderUnavailable,
    image_provider_for_settings,
    provider_for_settings,
    video_provider_for_settings,
)
from app.services.ai_regeneration import (
    AIRegenerationError,
    AIRegenerationService,
    _claims,
    _facts,
    _cost,
    _decimal,
    _estimated_tokens,
    _pricing,
    _snapshot,
    _validate_provider_copy,
    _verified_image,
)
from app.services.creative_rendering import CreativeRenderError, CreativeRenderService, _decode_source
from app.services.fingerprints import text_fingerprint


CHANNELS = {"pinterest", "instagram", "facebook", "tiktok", "youtube_shorts"}
BACKGROUND_STYLES = {
    "quiet_luxury": "Quiet luxury, warm ivory stone, soft natural shadows, subtle champagne accents, premium editorial lighting.",
    "modern_gradient": "Modern abstract gradient in muted charcoal, bronze, and cream with restrained depth and soft studio light.",
    "botanical_editorial": "Refined botanical shadows and soft neutral plaster, airy editorial styling, no literal flowers in the foreground.",
    "bold_color_block": "Sophisticated geometric color blocks with generous negative space and clean high-contrast editorial lighting.",
}
DEFAULT_IMAGE_COSTS = {"gpt-image-1": Decimal("0.042"), "gpt-image-1-mini": Decimal("0.011")}
SAFE_CREATIVE_WORDS = {
    "a", "an", "and", "are", "as", "at", "authentic", "background", "be", "board", "by",
    "caption", "catalog", "center", "close", "composition",
    "concept", "creative", "detail", "discover", "editorial", "explore", "feature", "foreground",
    "for", "frame", "from", "guide", "hook", "image", "in", "inspiration", "is", "it", "light",
    "listed", "look", "of", "on", "only", "or", "overlay", "pinterest",
    "product", "review", "scene", "script", "shop", "shot", "social", "source", "storyboard",
    "style", "the", "this", "to", "using", "video", "view", "visual", "voiceover", "with",
    "without", "your", "details", "details", "neutral", "same", "crop", "shown", "shopify",
}


class AICreativeGenerationError(ValueError):
    pass


class AIGeneratedAssetStorage:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path(__file__).resolve().parents[2] / "generated-ai-assets").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write_png(self, asset_id: str, contents: bytes) -> tuple[str, str]:
        if not asset_id or any(c not in "0123456789abcdef-" for c in asset_id.lower()):
            raise AICreativeGenerationError("Invalid generated asset storage key.")
        path = (self.root / f"{asset_id}.png").resolve()
        if path.parent != self.root:
            raise AICreativeGenerationError("Invalid generated asset storage path.")
        path.write_bytes(contents)
        return str(path), f"/api/pins/ai-assets/{asset_id}/image"

    def path_for(self, asset_id: str) -> Path:
        path = (self.root / f"{asset_id}.png").resolve()
        if path.parent != self.root:
            raise AICreativeGenerationError("Invalid generated asset key.")
        return path


def _normalize_png(data: bytes) -> tuple[bytes, int, int]:
    image = _decode_source(data)
    if image.width < 512 or image.height < 512:
        raise AICreativeGenerationError("Generated background dimensions must be at least 512 × 512.")
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=False)
    contents = output.getvalue()
    if len(contents) > 8 * 1024 * 1024:
        raise AICreativeGenerationError("Generated background exceeds the maximum stored size.")
    return contents, image.width, image.height


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _all_strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _all_strings(item)]
    return []


def _words(value: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]+", value.lower(), flags=re.UNICODE))


def _safe_structured_text(payload: dict[str, Any], facts: dict[str, Any], product: Any) -> None:
    texts = _all_strings(payload)
    if not texts or product.title.lower() not in " ".join(texts).lower():
        raise AICreativeGenerationError("Generated content did not retain the persisted product title.")
    for value in texts:
        if _claims(value, product):
            raise AICreativeGenerationError("Generated content introduced an unsupported numeric or promotional claim.")
    trusted_strings = [str(value) for value in facts.values() if isinstance(value, (str, int, float))]
    allowed = set().union(*(_words(value) for value in trusted_strings)) | SAFE_CREATIVE_WORDS
    unknown = set().union(*(_words(value) for value in texts)) - allowed
    if unknown:
        raise AICreativeGenerationError(
            "Generated content drifted beyond persisted catalog facts: " + ", ".join(sorted(unknown)[:8])
        )


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise AICreativeGenerationError("AI provider returned invalid structured content.") from exc
    if not isinstance(value, dict):
        raise AICreativeGenerationError("AI provider returned invalid structured content.")
    return value


class AICreativeGenerationService:
    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        *,
        image_provider_factory: Callable = image_provider_for_settings,
        text_provider_factory: Callable = provider_for_settings,
        video_provider_factory: Callable = video_provider_for_settings,
        renderer: CreativeRenderService | None = None,
        asset_storage: AIGeneratedAssetStorage | None = None,
    ):
        self.session_factory = session_factory
        self.image_provider_factory = image_provider_factory
        self.text_provider_factory = text_provider_factory
        self.video_provider_factory = video_provider_factory
        self.renderer = renderer or CreativeRenderService(session_factory=session_factory)
        self.asset_storage = asset_storage or AIGeneratedAssetStorage()
        self.regeneration = AIRegenerationService(session_factory=session_factory, creative_renderer=self.renderer)

    @staticmethod
    def _telemetry(
        draft_id: str,
        generation_type: str,
        provider: str,
        model: str,
        *,
        status: str,
        latency_ms: int = 0,
        estimated_cost: Decimal | None = None,
        actual_cost: Decimal | None = None,
        failure_code: str | None = None,
        fallback_reason: str | None = None,
        validation_reason: str | None = None,
        request_type: str = "provider_attempt",
    ) -> AIRequestTelemetry:
        return AIRequestTelemetry(
            draft_id=draft_id,
            operation=f"{generation_type}_generation",
            request_type=request_type,
            generation_type=generation_type,
            provider=provider,
            model=model,
            success=status == "success",
            latency_ms=latency_ms,
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=actual_cost,
            failure_code=failure_code,
            fallback_used=status == "fallback",
            fallback_reason=fallback_reason,
            validation_failure_reason=validation_reason,
        )

    @staticmethod
    def _lock_budget(db: Any) -> None:
        if db.bind and db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 842091733})

    @staticmethod
    def _spend(db: Any, since: datetime) -> Decimal:
        return _decimal(db.scalar(
            select(func.coalesce(func.sum(func.coalesce(
                AIRequestTelemetry.actual_cost_usd,
                AIRequestTelemetry.estimated_cost_usd,
            )), 0.0)).where(
                AIRequestTelemetry.created_at >= since,
                AIRequestTelemetry.provider == "openai",
                AIRequestTelemetry.request_type == "provider_attempt",
            )
        ) or 0)

    def _paid_preflight(self, db: Any, settings: Any, estimate: Decimal | None) -> None:
        if estimate is None:
            raise AICreativeGenerationError("Paid generation is blocked because model pricing is unknown.")
        estimated = estimate or Decimal("0")
        if settings.per_request_cost_usd <= 0 or estimated > settings.per_request_cost_usd:
            raise AICreativeGenerationError("Paid generation would exceed the per-request cost ceiling.")
        self._lock_budget(db)
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)
        if self._spend(db, day_start) + estimated > settings.daily_budget_usd:
            raise AICreativeGenerationError("Daily hosted AI budget is exhausted.")
        if self._spend(db, month_start) + estimated > settings.monthly_budget_usd:
            raise AICreativeGenerationError("Monthly hosted AI budget is exhausted.")

    @staticmethod
    def _image_cost(settings: Any) -> Decimal | None:
        configured = (settings.pricing_metadata or {}).get(settings.image_model, {})
        value = configured.get("per_image") if isinstance(configured, dict) else None
        if value is None:
            value = DEFAULT_IMAGE_COSTS.get(settings.image_model)
        classifier_cost = _cost(1000, 120, _pricing(settings, settings.hosted_model))
        if value is None or classifier_cost is None:
            return None
        return _decimal(value) + classifier_cost

    @staticmethod
    def _text_estimate(settings: Any, model: str, prompt: str, max_tokens: int = 1400) -> Decimal | None:
        prompt_tokens, _ = _estimated_tokens(prompt)
        return _cost(prompt_tokens, min(max_tokens, 600), _pricing(settings, model))

    def _base(self, db: Any, draft: Any, rationale: dict[str, Any], image: Any, original: Any) -> tuple[dict[str, Any], Any]:
        parent = self.regeneration._parent(db, draft.id)
        if parent:
            return {
                "headline": parent.headline, "title": parent.title, "description": parent.description,
                "alt_text": parent.alt_text, "cta": parent.cta, "content_angle": parent.content_angle,
                "content_angle_key": parent.content_angle_key, "creative_template": parent.creative_template,
                "creative_template_key": parent.creative_template_key, "destination_url": parent.destination_url,
                "utm_url": parent.utm_url, "keywords": parent.keywords, "facts_used": parent.facts_used,
                "warnings": parent.warnings, "missing_facts": parent.missing_facts,
                "unsupported_claims": parent.unsupported_claims, "text_fingerprint": parent.text_fingerprint,
            }, parent
        return _snapshot(
            draft=draft, rationale=rationale, facts=rationale.get("facts_used", {}),
            image=image, creative=original,
        ), None

    def generate_background(self, draft_id: str, style_key: str, channel: str = "pinterest") -> dict[str, Any]:
        if style_key not in BACKGROUND_STYLES:
            raise AICreativeGenerationError("Unsupported background style.")
        if channel not in CHANNELS:
            raise AICreativeGenerationError("Unsupported intended channel.")
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self.regeneration._source(db, draft_id)
            settings = self.regeneration._settings(db)
            provider = self.image_provider_factory(settings)
            estimate = self._image_cost(settings)
            if (
                not settings.enabled
                or settings.provider_mode != "hosted_paid"
                or not settings.decorative_backgrounds_enabled
                or provider is None
            ):
                telemetry = self._telemetry(
                    draft.id, "image_background", "openai", settings.image_model,
                    status="failed", failure_code="provider_disabled",
                    request_type="preflight_blocked",
                )
                db.add(telemetry)
                db.commit()
                raise AICreativeGenerationError("OpenAI image generation is disabled; no background variant was created.")
            try:
                self._paid_preflight(db, settings, estimate)
            except AICreativeGenerationError as exc:
                db.add(self._telemetry(
                    draft.id, "image_background", "openai", settings.image_model,
                    status="failed", estimated_cost=estimate, failure_code="budget_blocked",
                    validation_reason=str(exc),
                    request_type="preflight_blocked",
                ))
                db.commit()
                raise
            started = time.monotonic()
            try:
                result = provider.generate_background(BACKGROUND_STYLES[style_key])
                png, width, height = _normalize_png(result.image_bytes)
                safety_decision = provider.validate_background(png)
            except (ProviderUnavailable, CreativeRenderError, AICreativeGenerationError) as exc:
                code = exc.code if isinstance(exc, ProviderUnavailable) else "image_validation_failed"
                reason = None if isinstance(exc, ProviderUnavailable) else str(exc)
                db.add(self._telemetry(
                    draft.id, "image_background", "openai", settings.image_model,
                    status="failed", latency_ms=int((time.monotonic() - started) * 1000),
                    estimated_cost=estimate, failure_code=code, validation_reason=reason,
                ))
                db.commit()
                raise AICreativeGenerationError(str(exc)) from exc

            sha = hashlib.sha256(png).hexdigest()
            prompt_hash = hashlib.sha256(BACKGROUND_STYLES[style_key].encode()).hexdigest()
            asset = AIGeneratedAsset(
                draft_id=draft.id, asset_type="image_background", provider="openai",
                model=result.model, status="REVIEW", mime_type="image/png", width=width,
                height=height, size_bytes=len(png), sha256=sha, prompt_hash=prompt_hash,
                provenance={
                    "background_only": True, "style_key": style_key, "inline_provider_output": True,
                    "arbitrary_image_fetching": False, "product_image_generated": False,
                    "safety_gate": safety_decision,
                },
            )
            db.add(asset)
            db.flush()
            path, url = self.asset_storage.write_png(asset.id, png)
            asset.storage_path = path
            base, parent = self._base(db, draft, rationale, image, original)
            render_result = self.renderer.render_variant(
                draft.id, base["creative_template_key"], snapshot=base,
                background_bytes=png,
                background_metadata={
                    "asset_id": asset.id, "asset_url": url, "sha256": sha,
                    "provider": "openai", "model": result.model, "style_key": style_key,
                    "background_only": True, "product_image_generated": False,
                },
                db=db,
            )
            creative = db.get(PinCreative, render_result["creative_id"])
            telemetry = self._telemetry(
                draft.id, "image_background", "openai", result.model, status="success",
                latency_ms=int((time.monotonic() - started) * 1000),
                estimated_cost=estimate,
            )
            db.add(telemetry)
            db.flush()
            revision = ContentRevision(
                draft_id=draft.id, parent_revision_id=parent.id if parent else None,
                version=self.regeneration._next_version(db, draft.id), revision_kind="IMAGE_BACKGROUND",
                status="REVIEW", headline=base["headline"], title=base["title"],
                description=base["description"], alt_text=base["alt_text"], cta=base["cta"],
                content_angle=base["content_angle"], content_angle_key=base["content_angle_key"],
                creative_template=base["creative_template"], creative_template_key=base["creative_template_key"],
                destination_url=base["destination_url"], utm_url=base["utm_url"], keywords=base["keywords"],
                facts_used=base["facts_used"], warnings=base["warnings"], missing_facts=base["missing_facts"],
                unsupported_claims=[], text_fingerprint=base["text_fingerprint"],
                creative_fingerprint=creative.creative_fingerprint, creative_id=creative.id,
                source_image_id=image.id, background_asset_id=asset.id,
                provenance={
                    "product_image": _verified_image(image, product.id, rationale),
                    "generated_background": asset.provenance | {"asset_id": asset.id, "sha256": sha},
                    "authentic_product_image_composited_unchanged": True,
                },
                provider_mode="hosted_paid", generation_mode="provider_generated_background",
                generation_type="image_background", intended_channel=channel,
                reason="background_only_visual_variant", ai_telemetry_id=telemetry.id,
                estimated_cost_usd=estimate, actual_cost_usd=None,
            )
            db.add(revision)
            db.commit()
            return self.regeneration.revision_payload(revision, creative, None)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _deterministic_video_spec(product_title: str, headline: str, cta: str, channel: str) -> dict[str, Any]:
        return {
            "format": "reviewable_video_specification", "rendered_video": False,
            "channel": channel, "concept": f"Editorial catalog feature for {product_title}",
            "hook": headline,
            "script": f"Discover {product_title}. Review the authentic catalog image and product details.",
            "caption": f"{headline} {cta}".strip(),
            "overlay_text": [product_title, cta],
            "cta": cta,
            "scenes": [
                {"index": 1, "duration_seconds": 3, "visual": "Authentic Shopify product image on a neutral background.", "voiceover": headline, "overlay": product_title},
                {"index": 2, "duration_seconds": 3, "visual": "Detail crop from the same authentic Shopify product image.", "voiceover": f"Discover {product_title}.", "overlay": cta},
            ],
            "asset_policy": {"authentic_shopify_image_only": True, "generated_product_imagery": False},
        }

    def generate_structured(
        self,
        draft_id: str,
        generation_type: str,
        channel: str = "pinterest",
    ) -> dict[str, Any]:
        if generation_type not in {"content_variant", "video_script", "storyboard"}:
            raise AICreativeGenerationError("Unsupported generation type.")
        if channel not in CHANNELS:
            raise AICreativeGenerationError("Unsupported intended channel.")
        db = self.session_factory()
        try:
            draft, concept, product, intelligence, image, rationale, original = self.regeneration._source(db, draft_id)
            settings = self.regeneration._settings(db)
            base, parent = self._base(db, draft, rationale, image, original)
            facts = _facts(product, intelligence, image)
            is_video = generation_type in {"video_script", "storyboard"}
            provider = self.video_provider_factory(settings) if is_video else self.text_provider_factory(settings)
            schema = (
                '{"concept":"...","hook":"...","script":"...","caption":"...","overlay_text":["..."],'
                '"cta":"...","scenes":[{"index":1,"duration_seconds":3,"visual":"...",'
                '"voiceover":"...","overlay":"..."}]}'
                if is_video else
                '{"headline":"...","title":"...","description":"...","cta":"...",'
                '"board_description":"...","social_post":"...","hooks":["..."],"keywords":["..."]}'
            )
            prompt = (
                f"Return JSON only using this exact shape: {schema}. Intended channel: {channel}. "
                f"Generation type: {generation_type}. Product title must remain exactly {product.title!r}. "
                "Use only persisted facts in the following JSON. Do not invent discounts, scarcity, ratings, "
                "ingredients, performance, certifications, audiences, or product visuals. Video scenes may use "
                "only the authentic Shopify source image; do not request generated or reconstructed product imagery. "
                f"Persisted facts: {json.dumps(facts, sort_keys=True, default=str)}"
            )
            provider_mode = self.regeneration._provider_mode(settings)
            model = (
                settings.video_model if is_video and provider_mode == "hosted_paid"
                else settings.hosted_model if provider_mode == "hosted_paid"
                else settings.local_model
            )
            estimate = self._text_estimate(settings, model, prompt)
            fallback_reason = None
            payload: dict[str, Any]
            telemetry: AIRequestTelemetry | None = None
            if provider is None:
                fallback_reason = "provider_disabled"
            else:
                if provider_mode == "hosted_paid":
                    try:
                        self._paid_preflight(db, settings, estimate)
                    except AICreativeGenerationError as exc:
                        db.add(self._telemetry(
                            draft.id, generation_type, "openai", model, status="failed",
                            estimated_cost=estimate, failure_code="budget_blocked", validation_reason=str(exc),
                            request_type="preflight_blocked",
                        ))
                        db.commit()
                        raise
                started = time.monotonic()
                try:
                    result = provider.generate(prompt)
                    payload = _json_object(result.text)
                    _safe_structured_text(payload, facts, product)
                    telemetry = self._telemetry(
                        draft.id, generation_type, provider.name, result.model, status="success",
                        latency_ms=int((time.monotonic() - started) * 1000),
                        estimated_cost=estimate,
                    )
                except (ProviderUnavailable, AICreativeGenerationError) as exc:
                    fallback_reason = exc.code if isinstance(exc, ProviderUnavailable) else "fact_safety_rejection"
                    telemetry = self._telemetry(
                        draft.id, generation_type, getattr(provider, "name", "unknown"), model,
                        status="fallback", latency_ms=int((time.monotonic() - started) * 1000),
                        estimated_cost=estimate,
                        failure_code=fallback_reason,
                        fallback_reason=fallback_reason,
                        validation_reason=str(exc) if isinstance(exc, AICreativeGenerationError) else None,
                    )
            if fallback_reason:
                if is_video:
                    payload = self._deterministic_video_spec(product.title, base["headline"], base["cta"], channel)
                else:
                    payload = {
                        "headline": base["headline"], "title": base["title"], "description": base["description"],
                        "cta": base["cta"], "board_description": base["description"],
                        "social_post": f"{base['headline']} {base['description']}",
                        "hooks": [base["headline"]], "keywords": list(base["keywords"]),
                    }
                if telemetry is None:
                    telemetry = self._telemetry(
                        draft.id, generation_type, "deterministic", "none", status="fallback",
                        fallback_reason=fallback_reason, failure_code=fallback_reason,
                    )
            db.add(telemetry)
            db.flush()
            if is_video:
                content = None
                video = payload | {
                    "format": "reviewable_video_specification", "rendered_video": False,
                    "channel": channel,
                    "asset_policy": {"authentic_shopify_image_only": True, "generated_product_imagery": False},
                }
                headline = str(payload.get("hook") or base["headline"])
                title = f"{product.title} — {'Storyboard' if generation_type == 'storyboard' else 'Video script'}"
                description = str(payload.get("caption") or base["description"])
                cta = str(payload.get("cta") or base["cta"])
            else:
                content, video = payload, None
                required = ("headline", "title", "description", "cta")
                if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
                    raise AICreativeGenerationError("Generated content variant is missing required copy fields.")
                _validate_provider_copy(
                    {
                        "headline": payload["headline"], "title": payload["title"],
                        "description": payload["description"],
                        "alt_text": f"{product.title} authentic Shopify product image",
                    },
                    product, intelligence, rationale,
                )
                headline, title = payload["headline"], payload["title"]
                description, cta = payload["description"], payload["cta"]
            revision = ContentRevision(
                draft_id=draft.id, parent_revision_id=parent.id if parent else None,
                version=self.regeneration._next_version(db, draft.id),
                revision_kind="VIDEO_SPEC" if is_video else "CONTENT",
                status="REVIEW", headline=headline, title=title, description=description,
                alt_text=base["alt_text"], cta=cta, content_angle=base["content_angle"],
                content_angle_key=base["content_angle_key"], creative_template=base["creative_template"],
                creative_template_key=base["creative_template_key"], destination_url=base["destination_url"],
                utm_url=base["utm_url"], keywords=payload.get("keywords", base["keywords"]),
                facts_used=facts, warnings=base["warnings"], missing_facts=base["missing_facts"],
                unsupported_claims=[], provenance={
                    "product_image": _verified_image(image, product.id, rationale),
                    "authentic_product_image_only": True, "rendered_video": False if is_video else None,
                },
                text_fingerprint=text_fingerprint(title=title, description=description, alt_text=base["alt_text"]),
                creative_fingerprint=base.get("creative_fingerprint"), source_image_id=image.id,
                provider_mode=provider_mode, generation_mode="deterministic_fallback" if fallback_reason else "provider_generated",
                generation_type=generation_type, intended_channel=channel,
                content_payload=content, video_spec=video,
                reason=f"{generation_type}_variant", ai_telemetry_id=telemetry.id,
                estimated_cost_usd=estimate, actual_cost_usd=telemetry.actual_cost_usd,
            )
            db.add(revision)
            db.commit()
            return self.regeneration.revision_payload(revision, None, None)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
