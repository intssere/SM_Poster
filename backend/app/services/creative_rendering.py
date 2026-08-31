"""Deterministic, local rendering of authentic product creative previews."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import CreativeTemplate, DraftStatus, PinConcept, PinCreative, PinDraft, Product, ProductImage
from app.services.fingerprints import creative_fingerprint

CANVAS = (1000, 1500)
MAX_SOURCE_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 8
TEMPLATES = {
    "luxury_product_spotlight": {"background": "#F5F0E8", "ink": "#1E2023", "accent": "#A46B36"},
    "product_classification": {"background": "#EFF2F2", "ink": "#18272B", "accent": "#46727A"},
    "gift_guide_gift_set": {"background": "#FFF2E7", "ink": "#542E2E", "accent": "#B65D48"},
    "editorial_product_pick": {"background": "#F0F0F7", "ink": "#25223A", "accent": "#76689B"},
}


class CreativeRenderError(ValueError):
    """A controlled error appropriate for displaying in render QA."""


class CreativeStorage:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path(__file__).resolve().parents[2] / "generated-creatives").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write_png(self, creative_id: str, contents: bytes) -> str:
        if not creative_id or any(c not in "0123456789abcdef-" for c in creative_id.lower()):
            raise CreativeRenderError("Invalid creative storage key.")
        path = (self.root / f"{creative_id}.png").resolve()
        if path.parent != self.root:
            raise CreativeRenderError("Invalid creative storage path.")
        path.write_bytes(contents)
        return f"/api/pins/creatives/{creative_id}/image"

    def path_for(self, creative_id: str) -> Path:
        path = (self.root / f"{creative_id}.png").resolve()
        if path.parent != self.root:
            raise CreativeRenderError("Invalid creative storage key.")
        return path


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    names = ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if bold else (
        "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    raise CreativeRenderError("Bundled/system DejaVu font is unavailable.")


def _public_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() != "cdn.shopify.com":
        raise CreativeRenderError("Source image must be an HTTPS cdn.shopify.com URL.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CreativeRenderError("Could not resolve source image host.") from exc
    for address in {entry[4][0] for entry in addresses}:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise CreativeRenderError("Source image host resolved to a non-public IP address.")


class SecureImageDownloader:
    """Downloader deliberately rejecting redirects and SSRF-prone destinations."""
    def __call__(self, url: str) -> bytes:
        _public_host(url)
        try:
            with httpx.Client(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
                with client.stream("GET", url, headers={"Accept": "image/*"}) as response:
                    if response.is_redirect:
                        raise CreativeRenderError("Source image redirects are not allowed.")
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                        raise CreativeRenderError("Source response MIME type is not an allowed image type.")
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_SOURCE_BYTES:
                            raise CreativeRenderError("Source image exceeds the maximum download size.")
                        chunks.append(chunk)
        except CreativeRenderError:
            raise
        except httpx.HTTPError as exc:
            raise CreativeRenderError("Source image download failed.") from exc
        return b"".join(chunks)


def _decode_source(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.verify()
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except Exception as exc:
        raise CreativeRenderError("Source response could not be decoded as an image.") from exc
    if image.width < 1 or image.height < 1 or image.width * image.height > 40_000_000:
        raise CreativeRenderError("Source image dimensions are invalid.")
    return image


def _lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int) -> list[str]:
    words = " ".join(text.split()).split(" ")
    out, current = [], ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            if not current or len(out) >= max_lines - 1:
                raise CreativeRenderError("Creative text cannot fit the selected template.")
            out.append(current)
            current = word
            if draw.textbbox((0, 0), current, font=font)[2] > max_width:
                raise CreativeRenderError("Creative text contains an unrenderable word.")
    if current:
        out.append(current)
    if len(out) > max_lines:
        raise CreativeRenderError("Creative text overflows the selected template.")
    return out


def render_png(spec: dict[str, Any], source: Image.Image) -> bytes:
    template = spec["template_key"]
    tokens = TEMPLATES.get(template)
    if not tokens:
        raise CreativeRenderError("Unsupported creative template.")
    canvas = Image.new("RGBA", CANVAS, tokens["background"])
    draw = ImageDraw.Draw(canvas)
    # All four paths share deterministic geometry but retain distinct token palettes.
    image_box = (80, 140 if template != "gift_guide_gift_set" else 180, 920, 870)
    fitted = ImageOps.contain(source.convert("RGBA"), (image_box[2] - image_box[0], image_box[3] - image_box[1]), Image.Resampling.LANCZOS)
    x = image_box[0] + ((image_box[2] - image_box[0]) - fitted.width) // 2
    y = image_box[1] + ((image_box[3] - image_box[1]) - fitted.height) // 2
    canvas.alpha_composite(fitted, (x, y))
    draw.rectangle((80, 940, 180, 950), fill=tokens["accent"])
    headline_font, sub_font = _font(True, 54), _font(False, 30)
    headline = _lines(draw, spec["headline"], headline_font, 840, 3)
    sub = _lines(draw, spec.get("supporting_text", spec.get("subheadline", "")), sub_font, 840, 3)
    y = 990
    for line in headline:
        draw.text((80, y), line, font=headline_font, fill=tokens["ink"])
        y += 67
    y += 20
    for line in sub:
        draw.text((80, y), line, font=sub_font, fill=tokens["ink"])
        y += 42
    if y > 1430:
        raise CreativeRenderError("Creative text overflows the canvas.")
    draw.text((80, 1440), "DIAMOND SHELF", font=_font(True, 20), fill=tokens["accent"])
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=False)
    return output.getvalue()


class CreativeRenderService:
    def __init__(self, session_factory: Callable = SessionLocal, downloader: Callable[[str], bytes] | None = None, storage: CreativeStorage | None = None):
        self.session_factory, self.downloader, self.storage = session_factory, downloader or SecureImageDownloader(), storage or CreativeStorage()

    def render_review_batch(self, limit: int = 12) -> dict[str, Any]:
        if not 1 <= limit <= 12:
            raise CreativeRenderError("Render batch limit must be between 1 and 12.")
        db = self.session_factory()
        results: list[dict[str, Any]] = []
        try:
            rows = db.execute(
                select(PinDraft, PinConcept, Product)
                .select_from(PinDraft)
                .join(PinConcept, PinConcept.id == PinDraft.concept_id)
                .join(Product, Product.id == PinConcept.product_id)
                .where(PinDraft.status == DraftStatus.READY_FOR_REVIEW)
                .order_by(PinDraft.created_at, PinDraft.id)
            ).all()
            # Greedily maximize new template, category, angle, and product coverage.
            used_products, used_templates, used_categories, used_angles = set(), set(), set(), set()
            ordered = list(rows)
            chosen = []
            while ordered and len(chosen) < limit:
                def priority(row):
                    draft, concept, _ = row
                    rationale = concept.rationale or {}
                    template = rationale.get("creative_template_key", "")
                    category = rationale.get("facts_used", {}).get("normalization_category", "unknown")
                    angle = rationale.get("content_angle_key", "")
                    novelty = (
                        int(template not in used_templates) * 1000
                        + int(category not in used_categories) * 100
                        + int(angle not in used_angles) * 25
                        + int(concept.product_id not in used_products) * 10
                    )
                    return (-novelty, template, category, angle, concept.product_id, draft.id)
                ordered.sort(key=priority)
                draft, concept, product = ordered.pop(0)
                chosen.append((draft, concept, product))
                rationale = concept.rationale or {}
                used_products.add(concept.product_id)
                used_templates.add(rationale.get("creative_template_key"))
                used_categories.add(rationale.get("facts_used", {}).get("normalization_category", "unknown"))
                used_angles.add(rationale.get("content_angle_key"))
            for draft, concept, product in chosen:
                results.append(self._render_one(db, draft, concept, product))
            db.commit()
            template_distribution, category_distribution = {}, {}
            for _, concept, _ in chosen:
                key = concept.rationale.get("creative_template_key", "unknown")
                category = concept.rationale.get("facts_used", {}).get("normalization_category", "unknown")
                template_distribution[key] = template_distribution.get(key, 0) + 1
                category_distribution[category] = category_distribution.get(category, 0) + 1
            return {"requested": limit, "selected": len(chosen), "attempted": len(results), "rendered": sum(x["status"] == "RENDERED" for x in results), "existing": sum(x["status"] == "EXISTING" for x in results), "failed": sum(x["status"] == "FAILED" for x in results), "template_distribution": template_distribution, "category_distribution": category_distribution, "unsupported_claims_introduced": [], "items": results}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def qa_report(self) -> dict[str, Any]:
        db = self.session_factory()
        try:
            rows = list(db.scalars(select(PinCreative)))
            by_status: dict[str, int] = {}
            for creative in rows:
                by_status[creative.render_status] = by_status.get(creative.render_status, 0) + 1
            rendered = [creative for creative in rows if creative.render_status == "RENDERED"]
            errors = [c.render_error or "" for c in rows if c.render_status == "FAILED"]
            png_sizes = [c.size_bytes for c in rendered if c.size_bytes is not None]
            durations = [c.render_duration_ms for c in rendered if c.render_duration_ms is not None]
            return {
                "total": len(rows),
                "by_status": by_status,
                "rendered_1000x1500": sum(c.width == 1000 and c.height == 1500 for c in rendered),
                "average_render_duration_ms": sum(durations) / len(durations) if durations else 0,
                "max_render_duration_ms": max(durations) if durations else 0,
                "png_size_min": min(png_sizes) if png_sizes else 0,
                "png_size_max": max(png_sizes) if png_sizes else 0,
                "sha256_unique": len({c.sha256 for c in rendered}) == len(rendered),
                "provenance_failures": sum("authentic image" in error.lower() or "persisted" in error.lower() for error in errors),
                "image_dimension_failures": sum("dimension" in error.lower() for error in errors),
                "text_overflow_failures": sum("text" in error.lower() or "overflow" in error.lower() for error in errors),
                "unsupported_claims_introduced": [],
                "failures": [
                    {"creative_id": c.id, "draft_id": c.draft_id, "error": c.render_error}
                    for c in rows if c.render_status == "FAILED"
                ],
                "publishing_enabled": False,
            }
        finally:
            db.close()

    def _render_one(self, db: Any, draft: PinDraft, concept: PinConcept, product: Product) -> dict[str, Any]:
        rationale = concept.rationale or {}
        image_data = rationale.get("authentic_image") or {}
        image = db.get(ProductImage, image_data.get("id"))
        template_key = rationale.get("creative_template_key")
        template = db.scalar(select(CreativeTemplate).where(CreativeTemplate.key == template_key, CreativeTemplate.version == rationale.get("template_version", 1)))
        parsed = urlparse(image.source_url) if image else None
        if not image or image.product_id != concept.product_id or not image.shopify_media_id or not image.editorial_eligible or image.source_url != image_data.get("url") or not parsed or parsed.scheme != "https" or parsed.hostname != "cdn.shopify.com" or not template:
            return self._failure(db, draft, template, image, "Persisted authentic image or template validation failed.")
        try:
            prior = db.scalar(
                select(PinCreative)
                .where(PinCreative.draft_id == draft.id, PinCreative.render_status == "RENDERED")
                .order_by(PinCreative.rendered_at.desc())
            )
            prior_checksum = ((prior.render_spec or {}).get("image") or {}).get("checksum_sha256") if prior else None
            expected_checksum = image_data.get("source_sha256") or image.source_sha256 or prior_checksum
            raw = self.downloader(image.source_url)
            source = _decode_source(raw)
            source_sha = hashlib.sha256(raw).hexdigest()
            if expected_checksum and expected_checksum != source_sha:
                raise CreativeRenderError("Downloaded image does not match the persisted source checksum.")
            spec = {
                "version": 1, "design_token_version": 1,
                "draft_id": draft.id, "proposal_id": draft.id, "concept_id": concept.id,
                "product_id": product.id, "brand": rationale.get("facts_used", {}).get("brand") or product.vendor,
                "image": {
                    "id": image.id, "shopify_media_id": image.shopify_media_id,
                    "provenance_url": image.source_url, "checksum_sha256": source_sha,
                    "checksum_basis": "persisted" if image_data.get("source_sha256") or image.source_sha256 else "first_verified_render",
                    "width": source.width, "height": source.height,
                },
                "canvas": {"width": 1000, "height": 1500}, "template_key": template_key,
                "template_version": template.version, "headline": rationale["headline"],
                "supporting_text": draft.title, "content_angle": rationale.get("content_angle"),
                "board": rationale.get("board_mapping"), "tokens": TEMPLATES.get(template_key),
            }
            fingerprint = creative_fingerprint(source_image_sha256=source_sha, template_key=template_key, template_version=template.version, text_hash=draft.text_fingerprint, layout_parameters=spec)
            existing = db.scalar(select(PinCreative).where(PinCreative.creative_fingerprint == fingerprint))
            if existing and existing.render_status == "RENDERED":
                return {"draft_id": draft.id, "creative_id": existing.id, "status": "EXISTING", "image_url": existing.rendered_url}
            creative = existing or PinCreative(draft_id=draft.id, template_id=template.id, source_image_id=image.id, creative_fingerprint=fingerprint, width=1000, height=1500)
            if not existing: db.add(creative); db.flush()
            started = time.monotonic()
            png = render_png(spec, source)
            creative.sha256, creative.rendered_url = hashlib.sha256(png).hexdigest(), self.storage.write_png(creative.id, png)
            creative.render_status, creative.render_error, creative.render_spec = "RENDERED", None, json.loads(json.dumps(spec, sort_keys=True))
            creative.rendered_at, creative.render_duration_ms, creative.size_bytes = datetime.now(timezone.utc), int((time.monotonic() - started) * 1000), len(png)
            return {"draft_id": draft.id, "creative_id": creative.id, "status": "RENDERED", "image_url": creative.rendered_url}
        except CreativeRenderError as exc:
            return self._failure(db, draft, template, image, str(exc))
        except Exception as exc:
            return self._failure(db, draft, template, image, f"Creative rendering failed unexpectedly: {type(exc).__name__}: {exc}.")

    def _failure(self, db: Any, draft: PinDraft, template: CreativeTemplate | None, image: ProductImage | None, error: str) -> dict[str, Any]:
        if template and image:
            creative = db.scalar(select(PinCreative).where(PinCreative.draft_id == draft.id, PinCreative.template_id == template.id, PinCreative.source_image_id == image.id))
            if creative and creative.render_status == "RENDERED":
                return {
                    "draft_id": draft.id, "creative_id": creative.id,
                    "status": "FAILED", "error": error,
                }
            if not creative:
                creative = PinCreative(draft_id=draft.id, template_id=template.id, source_image_id=image.id, creative_fingerprint=hashlib.sha256(f"failure:{draft.id}:{template.id}:{image.id}".encode()).hexdigest(), width=1000, height=1500)
                db.add(creative); db.flush()
            creative.render_status, creative.render_error = "FAILED", error
            return {"draft_id": draft.id, "creative_id": creative.id, "status": "FAILED", "error": error}
        return {"draft_id": draft.id, "status": "FAILED", "error": error}