"""Deterministic, local rendering of authentic product creative previews."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import socket
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import (
    ContentRevision,
    CreativeTemplate,
    DraftStatus,
    PinConcept,
    PinCreative,
    PinDraft,
    Product,
    ProductImage,
)
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
PREVIEW_CACHE_LIMIT = 16
_PREVIEW_CACHE: OrderedDict[str, bytes] = OrderedDict()
_PREVIEW_CACHE_LOCK = threading.Lock()
_PREVIEW_RENDER_SLOTS = threading.BoundedSemaphore(2)


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


def _is_near_white(pixel: tuple[int, int, int, int]) -> bool:
    """Return true only for deliberately conservative studio-background pixels."""
    red, green, blue, alpha = pixel
    return alpha > 245 and min(red, green, blue) >= 248 and max(red, green, blue) - min(red, green, blue) <= 8


def edge_connected_near_white_cutout(
    source: Image.Image,
    *,
    trusted_opaque_background: bool = False,
) -> tuple[Image.Image, dict[str, Any]]:
    """Remove only near-white pixels proven connected to the source image edge.

    This is intentionally not an object detector.  A pixel can only be removed
    if it passes the conservative colour test *and* is reachable from an edge
    pixel through pixels that pass that same test.  All retained RGBA samples
    are copied verbatim, so product pixels are never recoloured.
    """
    image = source.convert("RGBA")
    if not trusted_opaque_background:
        # RGB values and full opacity are not independent evidence of a
        # background.  A white product can be indistinguishable from a white
        # studio sweep, so production Shopify inputs fail closed by default.
        return image, {
            "applied": False,
            "mask_method": "edge_connected_near_white_v1",
            "confidence": 0.0,
            "retained_foreground_ratio": 1.0,
            "retained_foreground_bounds_ratio": {"width": 1.0, "height": 1.0},
            "retained_foreground_centrally_bounded": False,
            "safety_validated": False,
            "safety_rejection_reason": "opaque_background_not_independently_verified",
            "independent_opaque_background_evidence": False,
            "fallback_treatment": "premium_source_card_v1",
            "alpha_mask_composited": False,
            "retained_pixels_recolored": False,
        }
    width, height = image.size
    pixels = image.load()
    edge: deque[tuple[int, int]] = deque()
    visited: set[tuple[int, int]] = set()
    for x in range(width):
        edge.extend(((x, 0), (x, height - 1)))
    for y in range(1, max(1, height - 1)):
        edge.extend(((0, y), (width - 1, y)))
    while edge:
        x, y = edge.popleft()
        if (x, y) in visited or not _is_near_white(pixels[x, y]):
            continue
        visited.add((x, y))
        if x:
            edge.append((x - 1, y))
        if x + 1 < width:
            edge.append((x + 1, y))
        if y:
            edge.append((x, y - 1))
        if y + 1 < height:
            edge.append((x, y + 1))

    total = width * height
    removed = len(visited)
    retained = total - removed
    # A non-background sample on an image edge is a likely cropped/edge-touching
    # product.  We cannot safely infer its silhouette locally, so fail closed.
    edge_foreground = any(
        not _is_near_white(pixels[x, y])
        for x in range(width)
        for y in (0, height - 1)
    ) or any(
        not _is_near_white(pixels[x, y])
        for y in range(1, max(1, height - 1))
        for x in (0, width - 1)
    )
    foreground_count = 0
    left, right, top, bottom = width, -1, height, -1
    for y in range(height):
        for x in range(width):
            if not _is_near_white(pixels[x, y]):
                foreground_count += 1
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
    foreground_ratio = foreground_count / total
    if foreground_count:
        foreground_width_ratio = (right - left + 1) / width
        foreground_height_ratio = (bottom - top + 1) / height
    else:
        foreground_width_ratio = foreground_height_ratio = 0.0
    margin_x, margin_y = max(1, int(width * 0.02)), max(1, int(height * 0.02))
    centrally_bounded = bool(
        foreground_count
        and left >= margin_x
        and right < width - margin_x
        and top >= margin_y
        and bottom < height - margin_y
    )
    substantial_foreground = (
        foreground_ratio >= 0.10
        and foreground_width_ratio >= 0.18
        and foreground_height_ratio >= 0.18
        and centrally_bounded
    )
    # Reject ambiguous all-white/near-empty sources and tiny, low-confidence
    # regions.  Fallback preserves the original image unchanged in a card.
    coverage = removed / total
    retained_ratio = retained / total
    applied = (
        not edge_foreground
        and substantial_foreground
        and 0.03 <= coverage <= 0.94
        and retained_ratio >= 0.04
    )
    safety_reason = (
        "edge_touching_foreground"
        if edge_foreground
        else "insufficient_retained_foreground"
        if not substantial_foreground
        else "insufficient_confident_background"
        if not applied
        else None
    )
    metadata = {
        "applied": applied,
        "mask_method": "edge_connected_near_white_v1",
        "confidence": round(coverage, 6),
        "retained_foreground_ratio": round(foreground_ratio, 6),
        "retained_foreground_bounds_ratio": {
            "width": round(foreground_width_ratio, 6),
            "height": round(foreground_height_ratio, 6),
        },
        "retained_foreground_centrally_bounded": centrally_bounded,
        "safety_validated": applied,
        "safety_rejection_reason": safety_reason,
        "independent_opaque_background_evidence": True,
        "fallback_treatment": None if applied else "premium_source_card_v1",
        "alpha_mask_composited": applied,
        "retained_pixels_recolored": False,
    }
    if not applied:
        return image, metadata
    result = image.copy()
    alpha = result.getchannel("A")
    alpha_pixels = alpha.load()
    for x, y in visited:
        alpha_pixels[x, y] = 0
    result.putalpha(alpha)
    return result, metadata


def _luminance(color: tuple[int, int, int, int]) -> float:
    red, green, blue, _ = color
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255


def _local_text_tokens(canvas: Image.Image) -> dict[str, str]:
    """Choose text tokens from the reserved copy area, not global image colour."""
    sample = canvas.convert("RGBA").crop((60, 1010, 940, 1430)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    dark_surface = _luminance(sample) < 0.48
    return {
        "surface": "#1D2024" if dark_surface else "#FCFAF6",
        "ink": "#F9F7F2" if dark_surface else "#17191C",
        "muted": "#D9D5CC" if dark_surface else "#5D5A54",
        "accent": "#D7A567" if dark_surface else "#9A5F2C",
    }


def _with_alpha(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    return tuple(int(hex_color[index:index + 2], 16) for index in (1, 3, 5)) + (alpha,)


def _responsive_lines(
    draw: ImageDraw.ImageDraw, text: str, *, bold: bool, max_width: int, max_lines: int, maximum: int, minimum: int
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    for size in range(maximum, minimum - 1, -2):
        font = _font(bold, size)
        try:
            return _lines(draw, text, font, max_width, max_lines), font
        except CreativeRenderError:
            continue
    raise CreativeRenderError("Creative text cannot fit the selected template.")


def render_png(
    spec: dict[str, Any],
    source: Image.Image,
    background: Image.Image | None = None,
    *,
    prepared_product: Image.Image | None = None,
    cutout: dict[str, Any] | None = None,
) -> bytes:
    template = spec["template_key"]
    tokens = TEMPLATES.get(template)
    if not tokens:
        raise CreativeRenderError("Unsupported creative template.")
    canvas = (
        ImageOps.fit(background.convert("RGBA"), CANVAS, method=Image.Resampling.LANCZOS)
        if background is not None
        else Image.new("RGBA", CANVAS, tokens["background"])
    )
    # The upper 60% is a product stage; the lower region is a deterministic,
    # protected text zone.  Decorative AI imagery never supplies product pixels.
    draw = ImageDraw.Draw(canvas, "RGBA")
    product, resolved_cutout = (prepared_product, cutout) if prepared_product is not None and cutout is not None else edge_connected_near_white_cutout(source)
    image_box = (90, 145 if template != "gift_guide_gift_set" else 185, 910, 935)
    if not resolved_cutout["applied"]:
        shadow = (image_box[0] + 10, image_box[1] + 14, image_box[2] + 10, image_box[3] + 14)
        draw.rounded_rectangle(shadow, radius=34, fill=(20, 22, 24, 24))
        draw.rounded_rectangle(image_box, radius=34, fill=(255, 253, 249, 218), outline=(154, 95, 44, 80), width=2)
    fitted = ImageOps.contain(product, (image_box[2] - image_box[0] - 40, image_box[3] - image_box[1] - 36), Image.Resampling.LANCZOS)
    x = image_box[0] + ((image_box[2] - image_box[0]) - fitted.width) // 2
    # Wide catalog crops read as an editorial banner; taller objects sit on a
    # consistent visual baseline to retain generous negative space.
    y = image_box[1] if fitted.height < 180 else image_box[3] - fitted.height
    canvas.alpha_composite(fitted, (x, y))
    local = _local_text_tokens(canvas)
    draw.rounded_rectangle((50, 1000, 950, 1450), radius=28, fill=_with_alpha(local["surface"], 240))
    draw.rectangle((80, 1050, 220, 1058), fill=local["accent"])
    # A small, high-contrast masthead establishes the brand without competing
    # with the catalog product or editorial headline.
    draw.text((80, 1082), "DIAMOND SHELF  /  EDIT", font=_font(True, 19), fill=local["accent"])
    headline, headline_font = _responsive_lines(
        draw, spec["headline"], bold=True, max_width=840, max_lines=3, maximum=58, minimum=38
    )
    sub, sub_font = _responsive_lines(
        draw, spec.get("supporting_text", spec.get("subheadline", "")), bold=False, max_width=840, max_lines=2, maximum=31, minimum=22
    )
    y = 1130
    for line in headline:
        draw.text((80, y), line, font=headline_font, fill=local["ink"])
        y += headline_font.size + 13
    y += 12
    for line in sub:
        draw.text((80, y), line, font=sub_font, fill=local["muted"])
        y += sub_font.size + 11
    if y > 1405:
        raise CreativeRenderError("Creative text overflows the canvas.")
    draw.text((80, 1415), "CURATED OBJECTS • CONSIDERED LIVING", font=_font(True, 16), fill=local["accent"])
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=False)
    return output.getvalue()


class CreativeRenderService:
    def __init__(self, session_factory: Callable = SessionLocal, downloader: Callable[[str], bytes] | None = None, storage: CreativeStorage | None = None):
        self.session_factory = session_factory
        self.downloader = downloader or SecureImageDownloader()
        self.storage = storage

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

    def render_variant(
        self,
        draft_id: str,
        template_key: str,
        *,
        snapshot: dict[str, Any] | None = None,
        background_bytes: bytes | None = None,
        background_metadata: dict[str, Any] | None = None,
        db: Any = None,
    ) -> dict[str, Any]:
        """Render one additive variant without changing the proposal or its original creative."""
        if template_key not in TEMPLATES:
            raise CreativeRenderError("Unsupported creative template.")
        owns_session = db is None
        db = db or self.session_factory()
        try:
            row = db.execute(
                select(PinDraft, PinConcept, Product)
                .join(PinConcept, PinConcept.id == PinDraft.concept_id)
                .join(Product, Product.id == PinConcept.product_id)
                .where(PinDraft.id == draft_id)
            ).first()
            if not row:
                raise CreativeRenderError("Proposal was not found.")
            draft, concept, product = row
            if draft.status != DraftStatus.READY_FOR_REVIEW:
                raise CreativeRenderError("Only proposals in REVIEW can receive a creative variant.")
            template = db.scalar(
                select(CreativeTemplate).where(
                    CreativeTemplate.key == template_key,
                    CreativeTemplate.version == 1,
                )
            )
            if not template:
                db.add(CreativeTemplate(
                    key=template_key,
                    version=1,
                    name=template_key.replace("_", " ").title(),
                    renderer="pillow",
                    definition={"renderer_active": True, "authentic_product_image_required": True},
                    active=True,
                ))
                db.flush()
            background = _decode_source(background_bytes) if background_bytes else None
            result = self._render_one(
                db,
                draft,
                concept,
                product,
                template_key_override=template_key,
                copy_snapshot=snapshot,
                background=background,
                background_metadata=background_metadata,
            )
            if result["status"] not in {"RENDERED", "EXISTING"}:
                raise CreativeRenderError(result.get("error") or "Creative variant could not be rendered.")
            if owns_session:
                db.commit()
            return result
        except Exception:
            if owns_session:
                db.rollback()
            raise
        finally:
            if owns_session:
                db.close()

    def preview_version_png(self, draft_id: str, version_id: str) -> bytes:
        """Render a persisted copy version in memory without creating or updating records."""
        db = self.session_factory()
        try:
            row = db.execute(
                select(PinDraft, PinConcept, Product)
                .join(PinConcept, PinConcept.id == PinDraft.concept_id)
                .join(Product, Product.id == PinConcept.product_id)
                .where(PinDraft.id == draft_id)
            ).first()
            if not row:
                raise CreativeRenderError("Proposal was not found.")
            draft, concept, product = row
            if draft.status != DraftStatus.READY_FOR_REVIEW:
                raise CreativeRenderError("Only proposals in REVIEW can be previewed.")

            rationale = concept.rationale or {}
            image_data = rationale.get("authentic_image") or {}
            image = db.get(ProductImage, image_data.get("id"))
            revision = None
            if version_id != "original":
                revision = db.get(ContentRevision, version_id)
                if not revision or revision.draft_id != draft_id:
                    raise CreativeRenderError("Revision was not found for this proposal.")
                if revision.status != "REVIEW":
                    raise CreativeRenderError("Only revisions in REVIEW can be previewed.")

            template_key = revision.creative_template_key if revision else rationale.get("creative_template_key")
            template_version = rationale.get("template_version", 1)
            template = db.scalar(
                select(CreativeTemplate).where(
                    CreativeTemplate.key == template_key,
                    CreativeTemplate.version == template_version,
                )
            )
            parsed = urlparse(image.source_url) if image else None
            if (
                not image
                or image.product_id != concept.product_id
                or not image.shopify_media_id
                or not image.editorial_eligible
                or image.source_url != image_data.get("url")
                or not parsed
                or parsed.scheme != "https"
                or parsed.hostname != "cdn.shopify.com"
                or not template
            ):
                raise CreativeRenderError("Persisted authentic image or template validation failed.")

            prior = db.scalar(
                select(PinCreative)
                .where(PinCreative.draft_id == draft.id, PinCreative.render_status == "RENDERED")
                .order_by(PinCreative.rendered_at.desc())
            )
            prior_checksum = ((prior.render_spec or {}).get("image") or {}).get("checksum_sha256") if prior else None
            expected_checksum = image_data.get("source_sha256") or image.source_sha256 or prior_checksum
            headline = revision.headline if revision else rationale["headline"]
            supporting_text = revision.title if revision else draft.title
            text_fingerprint = revision.text_fingerprint if revision else draft.text_fingerprint
            cache_key = hashlib.sha256(json.dumps({
                "draft_id": draft.id,
                "version_id": version_id,
                "text_fingerprint": text_fingerprint,
                "template_key": template_key,
                "template_version": template.version,
                "source_url": image.source_url,
                "expected_checksum": expected_checksum,
            }, sort_keys=True).encode()).hexdigest()
            with _PREVIEW_CACHE_LOCK:
                cached = _PREVIEW_CACHE.get(cache_key)
                if cached is not None:
                    _PREVIEW_CACHE.move_to_end(cache_key)
                    return cached
            if not _PREVIEW_RENDER_SLOTS.acquire(blocking=False):
                raise CreativeRenderError("Preview renderer is busy; try again shortly.")
            try:
                with _PREVIEW_CACHE_LOCK:
                    cached = _PREVIEW_CACHE.get(cache_key)
                    if cached is not None:
                        _PREVIEW_CACHE.move_to_end(cache_key)
                        return cached
                raw = self.downloader(image.source_url)
                source = _decode_source(raw)
                source_sha = hashlib.sha256(raw).hexdigest()
                if expected_checksum and expected_checksum != source_sha:
                    raise CreativeRenderError("Downloaded image does not match the persisted source checksum.")
                prepared_product, cutout = edge_connected_near_white_cutout(source)

                spec = {
                    "version": 1,
                    "design_token_version": 1,
                    "draft_id": draft.id,
                    "proposal_id": draft.id,
                    "concept_id": concept.id,
                    "product_id": product.id,
                    "brand": rationale.get("facts_used", {}).get("brand") or product.vendor,
                    "image": {
                        "id": image.id,
                        "shopify_media_id": image.shopify_media_id,
                        "provenance_url": image.source_url,
                        "checksum_sha256": source_sha,
                        "checksum_basis": "persisted_read_only_preview",
                        "source_bytes_unchanged": True,
                        "alpha_mask_composited": cutout["applied"],
                        "width": source.width,
                        "height": source.height,
                        "cutout": cutout,
                    },
                    "canvas": {"width": 1000, "height": 1500},
                    "template_key": template_key,
                    "template_version": template.version,
                    "headline": headline,
                    "supporting_text": supporting_text,
                    "content_angle": revision.content_angle if revision else rationale.get("content_angle"),
                    "board": rationale.get("board_mapping"),
                    "tokens": TEMPLATES.get(template_key),
                }
                png = render_png(spec, source, prepared_product=prepared_product, cutout=cutout)
                with _PREVIEW_CACHE_LOCK:
                    _PREVIEW_CACHE[cache_key] = png
                    _PREVIEW_CACHE.move_to_end(cache_key)
                    while len(_PREVIEW_CACHE) > PREVIEW_CACHE_LIMIT:
                        _PREVIEW_CACHE.popitem(last=False)
                return png
            finally:
                _PREVIEW_RENDER_SLOTS.release()
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

    def _render_one(
        self,
        db: Any,
        draft: PinDraft,
        concept: PinConcept,
        product: Product,
        *,
        template_key_override: str | None = None,
        copy_snapshot: dict[str, Any] | None = None,
        background: Image.Image | None = None,
        background_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rationale = concept.rationale or {}
        image_data = rationale.get("authentic_image") or {}
        image = db.get(ProductImage, image_data.get("id"))
        template_key = template_key_override or rationale.get("creative_template_key")
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
            prepared_product, cutout = edge_connected_near_white_cutout(source)
            copy = copy_snapshot or {}
            headline = copy.get("headline") or rationale["headline"]
            supporting_text = copy.get("title") or draft.title
            text_hash = copy.get("text_fingerprint") or draft.text_fingerprint
            spec = {
                "version": 1, "design_token_version": 1,
                "draft_id": draft.id, "proposal_id": draft.id, "concept_id": concept.id,
                "product_id": product.id, "brand": rationale.get("facts_used", {}).get("brand") or product.vendor,
                "image": {
                    "id": image.id, "shopify_media_id": image.shopify_media_id,
                    "provenance_url": image.source_url, "checksum_sha256": source_sha,
                    "checksum_basis": "persisted" if image_data.get("source_sha256") or image.source_sha256 else "first_verified_render",
                    "source_bytes_unchanged": True,
                    "alpha_mask_composited": cutout["applied"],
                    "width": source.width, "height": source.height,
                    "cutout": cutout,
                },
                "canvas": {"width": 1000, "height": 1500}, "template_key": template_key,
                "template_version": template.version, "headline": rationale["headline"],
                "supporting_text": supporting_text, "content_angle": rationale.get("content_angle"),
                "board": rationale.get("board_mapping"), "tokens": TEMPLATES.get(template_key),
            }
            if background_metadata:
                spec["background"] = dict(background_metadata)
            spec["headline"] = headline
            fingerprint = creative_fingerprint(source_image_sha256=source_sha, template_key=template_key, template_version=template.version, text_hash=text_hash, layout_parameters=spec)
            existing = db.scalar(select(PinCreative).where(PinCreative.creative_fingerprint == fingerprint))
            if existing and existing.render_status == "RENDERED":
                return {"draft_id": draft.id, "creative_id": existing.id, "status": "EXISTING", "image_url": existing.rendered_url}
            creative = existing or PinCreative(draft_id=draft.id, template_id=template.id, source_image_id=image.id, creative_fingerprint=fingerprint, width=1000, height=1500)
            if not existing: db.add(creative); db.flush()
            started = time.monotonic()
            png = render_png(
                spec, source, background=background, prepared_product=prepared_product, cutout=cutout
            )
            if self.storage is None:
                self.storage = CreativeStorage()
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