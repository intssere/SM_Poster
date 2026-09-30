"""Offline, filesystem-staged creative media for the local canary fixture.

This module deliberately accepts verified source bytes from its caller instead
of resolving URLs or selecting an application storage backend.  It never
constructs an Object Storage client and does not publish or expose staged PNGs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from app.services.creative_rendering import (
    CreativeRenderError,
    _decode_source,
    edge_connected_near_white_cutout,
    render_png,
)
from app.services.media_storage import PNG_SIGNATURE


class LocalCanaryMediaError(RuntimeError):
    """A local-only fixture artifact failed provenance or filesystem checks."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_STAGING_DIRECTORY = ".task61-6-staging"


@dataclass(frozen=True)
class StagedCreativeArtifact:
    """Durable receipt written into the pending creative's existing render_spec."""

    stage_path: str
    final_path: str
    artifact_sha256: str
    artifact_size: int
    local_root: str
    source_image_id: str
    source_sha256: str
    input_fingerprint: str
    provenance_fingerprint: str
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": "TASK61_6A_LOCAL_MEDIA_STAGED_V1",
            "stage_path": self.stage_path,
            "final_path": self.final_path,
            "artifact_sha256": self.artifact_sha256,
            "artifact_size": self.artifact_size,
            "local_root": self.local_root,
            "source_image_id": self.source_image_id,
            "source_sha256": self.source_sha256,
            "input_fingerprint": self.input_fingerprint,
            "provenance_fingerprint": self.provenance_fingerprint,
            "provenance": self.provenance,
        }


def _canonical_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_root(root: str | Path | None) -> Path:
    if root is None:
        raise LocalCanaryMediaError("LOCAL_MEDIA_ROOT_REQUIRED")
    path = Path(root).expanduser().resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        not path.is_absolute()
        or path == temporary_root
        or temporary_root not in path.parents
    ):
        raise LocalCanaryMediaError("LOCAL_MEDIA_ROOT_REQUIRED")
    return path


def _safe_creative_id(creative_id: str) -> str:
    if not isinstance(creative_id, str) or not _IDENTIFIER.fullmatch(creative_id):
        raise LocalCanaryMediaError("INVALID_CREATIVE_ID")
    return creative_id


def _safe_digest(value: str, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise LocalCanaryMediaError(code)
    return value


def render_verified_local_png(
    spec: dict[str, Any],
    source_bytes: bytes,
    *,
    source_image_id: str,
    expected_source_sha256: str,
) -> tuple[bytes, dict[str, Any]]:
    """Render from explicitly supplied, checksum-verified bytes; never fetch."""
    _safe_creative_id(source_image_id)
    expected = _safe_digest(expected_source_sha256, "SOURCE_SHA256_REQUIRED")
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise LocalCanaryMediaError("VERIFIED_LOCAL_SOURCE_BYTES_REQUIRED")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != expected:
        raise LocalCanaryMediaError("LOCAL_SOURCE_SHA256_MISMATCH")
    if not isinstance(spec, dict) or not isinstance(spec.get("image"), dict):
        raise LocalCanaryMediaError("CREATIVE_PROVENANCE_REQUIRED")
    image_spec = spec["image"]
    if image_spec.get("id") != source_image_id:
        raise LocalCanaryMediaError("CREATIVE_SOURCE_ID_MISMATCH")
    if image_spec.get("checksum_sha256") not in (None, expected):
        raise LocalCanaryMediaError("CREATIVE_SOURCE_SHA256_MISMATCH")

    try:
        source = _decode_source(source_bytes)
        prepared_product, cutout = edge_connected_near_white_cutout(source)
        png = render_png(
            spec,
            source,
            prepared_product=prepared_product,
            cutout=cutout,
        )
    except CreativeRenderError as exc:
        raise LocalCanaryMediaError("LOCAL_CREATIVE_RENDER_FAILED") from exc
    if not png.startswith(PNG_SIGNATURE):
        raise LocalCanaryMediaError("RENDERED_ARTIFACT_NOT_PNG")
    return png, {
        "source_image_id": source_image_id,
        "source_sha256": source_sha256,
        "source_bytes_basis": "verified_local_input",
        "source_bytes_unchanged": True,
        "source_width": source.width,
        "source_height": source.height,
        "artifact_sha256": hashlib.sha256(png).hexdigest(),
        "artifact_size": len(png),
        "cutout": cutout,
    }


def stage_creative_png(
    root: str | Path,
    *,
    creative_id: str,
    png: bytes,
    source_image_id: str,
    source_sha256: str,
    input_fingerprint: str,
    provenance: dict[str, Any],
) -> StagedCreativeArtifact:
    """Fsync an artifact in a private stage directory and return its receipt."""
    root_path = _safe_root(root)
    creative_id = _safe_creative_id(creative_id)
    source_image_id = _safe_creative_id(source_image_id)
    source_sha256 = _safe_digest(source_sha256, "SOURCE_SHA256_REQUIRED")
    input_fingerprint = _safe_digest(input_fingerprint, "INPUT_FINGERPRINT_REQUIRED")
    if not isinstance(provenance, dict) or not provenance:
        raise LocalCanaryMediaError("CREATIVE_PROVENANCE_REQUIRED")
    if not isinstance(png, bytes) or not png.startswith(PNG_SIGNATURE):
        raise LocalCanaryMediaError("RENDERED_ARTIFACT_NOT_PNG")

    artifact_sha256 = hashlib.sha256(png).hexdigest()
    bound_provenance = {
        **provenance,
        "source_image_id": source_image_id,
        "source_sha256": source_sha256,
        "input_fingerprint": input_fingerprint,
        "artifact_sha256": artifact_sha256,
    }
    stage_dir = root_path / _STAGING_DIRECTORY
    stage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stage_dir.is_symlink() or root_path not in stage_dir.resolve().parents:
        raise LocalCanaryMediaError("STAGED_MEDIA_PATH_INVALID")
    final = root_path / "creative" / creative_id / f"{artifact_sha256}.png"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{creative_id}-{artifact_sha256}-",
        suffix=".stage",
        dir=stage_dir,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(png)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(stage_dir)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return StagedCreativeArtifact(
        stage_path=str(temporary.relative_to(root_path)),
        final_path=str(final.relative_to(root_path)),
        artifact_sha256=artifact_sha256,
        artifact_size=len(png),
        local_root=str(root_path),
        source_image_id=source_image_id,
        source_sha256=source_sha256,
        input_fingerprint=input_fingerprint,
        provenance_fingerprint=_canonical_fingerprint(bound_provenance),
        provenance=bound_provenance,
    )


def _receipt_paths(
    root: Path, receipt: dict[str, Any], creative_id: str
) -> tuple[Path, Path]:
    if receipt.get("protocol") != "TASK61_6A_LOCAL_MEDIA_STAGED_V1":
        raise LocalCanaryMediaError("STAGED_MEDIA_RECEIPT_INVALID")
    if _safe_root(receipt.get("local_root")) != root:
        raise LocalCanaryMediaError("STAGED_MEDIA_ROOT_MISMATCH")
    digest = _safe_digest(receipt.get("artifact_sha256"), "STAGED_MEDIA_RECEIPT_INVALID")
    if receipt.get("provenance_fingerprint") != _canonical_fingerprint(
        receipt.get("provenance")
    ):
        raise LocalCanaryMediaError("STAGED_MEDIA_PROVENANCE_INVALID")
    if not receipt.get("source_image_id") or not receipt.get("source_sha256"):
        raise LocalCanaryMediaError("STAGED_MEDIA_PROVENANCE_INVALID")
    _safe_creative_id(receipt["source_image_id"])
    _safe_digest(receipt["source_sha256"], "STAGED_MEDIA_PROVENANCE_INVALID")
    if not receipt.get("input_fingerprint"):
        raise LocalCanaryMediaError("STAGED_MEDIA_PROVENANCE_INVALID")
    _safe_digest(receipt["input_fingerprint"], "STAGED_MEDIA_PROVENANCE_INVALID")
    provenance = receipt["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("source_image_id") != receipt["source_image_id"]
        or provenance.get("source_sha256") != receipt["source_sha256"]
        or provenance.get("input_fingerprint") != receipt["input_fingerprint"]
        or provenance.get("artifact_sha256") != digest
    ):
        raise LocalCanaryMediaError("STAGED_MEDIA_PROVENANCE_INVALID")

    expected_stage_prefix = f"{_STAGING_DIRECTORY}/"
    expected_final = Path("creative") / creative_id / f"{digest}.png"
    stage_rel = Path(str(receipt.get("stage_path") or ""))
    final_rel = Path(str(receipt.get("final_path") or ""))
    if (
        stage_rel.is_absolute()
        or ".." in stage_rel.parts
        or not str(stage_rel).startswith(expected_stage_prefix)
        or final_rel != expected_final
    ):
        raise LocalCanaryMediaError("STAGED_MEDIA_PATH_INVALID")
    stage = (root / stage_rel).resolve()
    final = (root / final_rel).resolve()
    if root not in stage.parents or root not in final.parents:
        raise LocalCanaryMediaError("STAGED_MEDIA_PATH_INVALID")
    stage_dir = root / _STAGING_DIRECTORY
    if stage_dir.is_symlink() or root not in stage_dir.resolve().parents:
        raise LocalCanaryMediaError("STAGED_MEDIA_PATH_INVALID")
    return stage, final


def _verified_artifact(path: Path, receipt: dict[str, Any]) -> bytes:
    if path.is_symlink():
        raise LocalCanaryMediaError("STAGED_MEDIA_PATH_INVALID")
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise LocalCanaryMediaError("LOCAL_MEDIA_READ_FAILED") from exc
    if (
        not contents.startswith(PNG_SIGNATURE)
        or hashlib.sha256(contents).hexdigest() != receipt.get("artifact_sha256")
        or isinstance(receipt.get("artifact_size"), bool)
        or not isinstance(receipt.get("artifact_size"), int)
        or len(contents) != receipt.get("artifact_size")
    ):
        raise LocalCanaryMediaError("STAGED_MEDIA_ARTIFACT_MISMATCH")
    return contents


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def promote_staged_creative(
    root: str | Path,
    *,
    creative_id: str,
    receipt: dict[str, Any],
) -> Path:
    """Idempotently atomically promote a committed receipt's artifact."""
    root_path = _safe_root(root)
    creative_id = _safe_creative_id(creative_id)
    stage, final = _receipt_paths(root_path, receipt, creative_id)
    try:
        final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if final.parent.is_symlink() or root_path not in final.parent.resolve().parents:
            raise LocalCanaryMediaError("STAGED_MEDIA_PATH_INVALID")
        if final.exists():
            _verified_artifact(final, receipt)
            stage.unlink(missing_ok=True)
            if stage.parent.exists():
                _fsync_directory(stage.parent)
            return final
        if not stage.is_file():
            raise LocalCanaryMediaError("STAGED_MEDIA_ARTIFACT_MISSING")
        _verified_artifact(stage, receipt)
        os.replace(stage, final)
        _fsync_directory(final.parent)
        if stage.parent.exists():
            _fsync_directory(stage.parent)
        _verified_artifact(final, receipt)
        return final
    except LocalCanaryMediaError:
        raise
    except OSError as exc:
        raise LocalCanaryMediaError("STAGED_MEDIA_PROMOTION_FAILED") from exc


def has_local_canary_media_marker(creative: Any) -> bool:
    """Return true for both pending and promoted Task #61.6A creatives."""
    if creative is None:
        return False
    if getattr(creative, "render_status", None) == "STAGED":
        return True
    raw_spec = getattr(creative, "render_spec", None)
    spec = raw_spec if isinstance(raw_spec, dict) else {}
    return (
        "local_canary_stage" in spec
        or spec.get("local_canary_protocol") == "TASK61_6A_LOCAL_MEDIA_STAGED_V1"
    )


def read_verified_promoted(
    creative: Any,
    *,
    digest: str | None = None,
) -> bytes:
    """Read only the promoted artifact from a valid RENDERED local receipt."""
    if creative is None or creative.render_status != "RENDERED":
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_NOT_PROMOTED")
    spec = creative.render_spec if isinstance(creative.render_spec, dict) else {}
    receipt = spec.get("local_canary_stage")
    if not isinstance(receipt, dict):
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_RECEIPT_INVALID")
    root = _safe_root(receipt.get("local_root"))
    stage, final = _receipt_paths(root, receipt, _safe_creative_id(creative.id))
    expected_digest = _safe_digest(
        digest if digest is not None else creative.sha256,
        "LOCAL_CANARY_MEDIA_RECEIPT_INVALID",
    )
    if (
        expected_digest != creative.sha256
        or expected_digest != receipt.get("artifact_sha256")
        or receipt.get("source_image_id") != creative.source_image_id
        or receipt.get("final_path")
        != str(final.relative_to(root))
    ):
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_DIGEST_MISMATCH")
    try:
        return _verified_artifact(final, receipt)
    except FileNotFoundError as exc:
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_NOT_PROMOTED") from exc


def verify_promoted_pending(creative: Any, receipt: dict[str, Any]) -> bytes:
    """Internal reconciliation verification; never use this in a media route."""
    if creative is None or creative.render_status != "STAGED":
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_NOT_PENDING")
    root = _safe_root(receipt.get("local_root"))
    _, final = _receipt_paths(root, receipt, _safe_creative_id(creative.id))
    if creative.sha256 != receipt.get("artifact_sha256"):
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_DIGEST_MISMATCH")
    try:
        return _verified_artifact(final, receipt)
    except FileNotFoundError as exc:
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_NOT_PROMOTED") from exc


def cleanup_orphan_staging(
    root: str | Path,
    *,
    committed_receipts: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    """Remove only stage files not referenced by a durable pending DB receipt."""
    root_path = _safe_root(root)
    stage_dir = root_path / _STAGING_DIRECTORY
    if not stage_dir.exists():
        return []
    if stage_dir.is_symlink() or root_path not in stage_dir.resolve().parents:
        raise LocalCanaryMediaError("STAGED_MEDIA_PATH_INVALID")
    referenced: set[Path] = set()
    for creative_id, receipt in committed_receipts:
        creative_id = _safe_creative_id(creative_id)
        stage, _ = _receipt_paths(root_path, receipt, creative_id)
        referenced.add(stage)

    removed: list[str] = []
    for candidate in sorted(stage_dir.iterdir()):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in referenced:
            continue
        candidate.unlink()
        removed.append(str(candidate.relative_to(root_path)))
    if removed:
        _fsync_directory(stage_dir)
    return removed