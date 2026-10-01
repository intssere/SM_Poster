"""Offline-rendered creative media and recoverable canary promotion.

This module deliberately accepts verified source bytes from its caller instead
of resolving URLs. It stages locally before promoting into the configured
digest-addressed storage backend. It never publishes or exposes staged PNGs.
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
from app.services.media_storage import (
    PNG_SIGNATURE,
    LocalStorage,
    PNGMediaStorage,
    StorageCorrupt,
    StorageMissing,
    StorageUnavailable,
    media_key,
)


class LocalCanaryMediaError(RuntimeError):
    """A local-only fixture artifact failed provenance or filesystem checks."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_STAGING_DIRECTORY = ".task61-6-staging"
LOCAL_CANARY_PROTOCOL = "TASK61_6A_LOCAL_MEDIA_STAGED_V1"
DURABLE_CANARY_PROTOCOL = "TASK61_14_DURABLE_PNG_V1"


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
            "protocol": LOCAL_CANARY_PROTOCOL,
            "creative_id": self.provenance["creative_id"],
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
        "creative_id": creative_id,
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
    if receipt.get("protocol") != LOCAL_CANARY_PROTOCOL:
        raise LocalCanaryMediaError("STAGED_MEDIA_RECEIPT_INVALID")
    if _safe_root(receipt.get("local_root")) != root:
        raise LocalCanaryMediaError("STAGED_MEDIA_ROOT_MISMATCH")
    digest = _safe_digest(receipt.get("artifact_sha256"), "STAGED_MEDIA_RECEIPT_INVALID")
    if receipt.get("creative_id") != creative_id:
        raise LocalCanaryMediaError("STAGED_MEDIA_PROVENANCE_INVALID")
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
        or provenance.get("creative_id") != creative_id
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
    creative: Any = None,
    storage=None,
    settings=None,
) -> Path | dict[str, Any]:
    """Idempotently atomically promote a committed receipt's artifact."""
    if durable_media_required(settings):
        return promote_staged_creative_durable(
            root,
            creative_id=creative_id,
            receipt=receipt,
            creative=creative,
            storage=storage,
            settings=settings,
        )
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


def cleanup_committed_staged_creative(
    root: str | Path,
    *,
    creative_id: str,
    receipt: dict[str, Any],
) -> bool:
    """Remove a local stage only after the caller commits its durable receipt."""
    root_path = _safe_root(root)
    creative_id = _safe_creative_id(creative_id)
    receipt_root = _safe_root(receipt.get("local_root") if isinstance(receipt, dict) else None)
    stage, _ = _receipt_paths(receipt_root, receipt, creative_id)
    if root_path != receipt_root or not stage.is_file():
        return False
    try:
        _verified_artifact(stage, receipt)
    except LocalCanaryMediaError:
        return False
    try:
        stage.unlink()
        if stage.parent.exists():
            _fsync_directory(stage.parent)
        return True
    except OSError as exc:
        raise LocalCanaryMediaError("STAGED_MEDIA_CLEANUP_FAILED") from exc


def durable_media_required(settings=None) -> bool:
    """Whether this runtime must use the shared durable creative store."""
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    return getattr(settings, "is_exposed", False) is True


def _media_storage(storage=None, *, settings=None):
    if storage is not None and hasattr(storage, "media"):
        storage = storage.media
    if storage is None:
        try:
            storage = PNGMediaStorage("creative", settings=settings)
        except Exception as exc:
            raise LocalCanaryMediaError("DURABLE_MEDIA_STORAGE_UNAVAILABLE") from exc
    if not isinstance(storage, PNGMediaStorage) or storage.kind != "creative":
        raise LocalCanaryMediaError("DURABLE_MEDIA_STORAGE_UNAVAILABLE")
    if durable_media_required(settings) and isinstance(
        getattr(storage, "backend", None), LocalStorage
    ):
        raise LocalCanaryMediaError("DURABLE_MEDIA_LOCAL_BACKEND_FORBIDDEN")
    return storage


def immutable_render_spec_fingerprint(spec: Any) -> str:
    if not isinstance(spec, dict):
        raise LocalCanaryMediaError("DURABLE_MEDIA_RENDER_SPEC_INVALID")
    immutable = {
        key: value for key, value in spec.items()
        if key not in {"local_canary_stage", "durable_media_protocol"}
    }
    return _canonical_fingerprint(immutable)


def _validate_durable_provenance(
    creative: Any,
    receipt: dict[str, Any],
    *,
    expected_input_fingerprint: str | None = None,
) -> str:
    creative_id = _safe_creative_id(getattr(creative, "id", None))
    provenance = receipt.get("provenance")
    spec = getattr(creative, "render_spec", None)
    if not isinstance(provenance, dict) or not isinstance(spec, dict):
        raise LocalCanaryMediaError("DURABLE_MEDIA_PROVENANCE_MISMATCH")
    image = spec.get("image")
    if not isinstance(image, dict):
        raise LocalCanaryMediaError("DURABLE_MEDIA_PROVENANCE_MISMATCH")
    source_image_id = getattr(creative, "source_image_id", None)
    source_sha256 = image.get("checksum_sha256")
    input_fingerprint = receipt.get("input_fingerprint")
    creative_fingerprint = getattr(creative, "creative_fingerprint", None)
    size = getattr(creative, "size_bytes", None)
    if size is None:
        size = getattr(creative, "rendered_size", None)
    if (
        receipt.get("creative_id") != creative_id
        or not source_image_id
        or image.get("id") != source_image_id
        or not _SHA256.fullmatch(str(source_sha256 or ""))
        or receipt.get("source_image_id") != source_image_id
        or receipt.get("source_sha256") != source_sha256
        or not _SHA256.fullmatch(str(input_fingerprint or ""))
        or provenance.get("generation_input_fingerprint") != input_fingerprint
        or (
            expected_input_fingerprint is not None
            and input_fingerprint != expected_input_fingerprint
        )
        or not _SHA256.fullmatch(str(creative_fingerprint or ""))
        or provenance.get("creative_fingerprint") != creative_fingerprint
        or provenance.get("render_spec_fingerprint")
        != immutable_render_spec_fingerprint(spec)
        or getattr(creative, "sha256", None) != receipt.get("artifact_sha256")
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size != receipt.get("artifact_size")
        or provenance.get("source_bytes_unchanged") is not True
        or provenance.get("source_bytes_basis") != "verified_local_input"
    ):
        raise LocalCanaryMediaError("DURABLE_MEDIA_PROVENANCE_MISMATCH")
    _safe_digest(source_sha256, "DURABLE_MEDIA_PROVENANCE_MISMATCH")
    _safe_digest(creative_fingerprint, "DURABLE_MEDIA_PROVENANCE_MISMATCH")
    return creative_id


def _durable_receipt_fingerprint(creative_id: str, receipt: dict[str, Any]) -> str:
    return _canonical_fingerprint({
        "protocol": receipt.get("durable_protocol"),
        "creative_id": creative_id,
        "artifact_sha256": receipt.get("artifact_sha256"),
        "artifact_size": receipt.get("artifact_size"),
        "source_image_id": receipt.get("source_image_id"),
        "source_sha256": receipt.get("source_sha256"),
        "input_fingerprint": receipt.get("input_fingerprint"),
        "provenance_fingerprint": receipt.get("provenance_fingerprint"),
        "storage_key": receipt.get("storage_key"),
    })


def _verified_local_stage_or_final(
    root: Path,
    creative_id: str,
    receipt: dict[str, Any],
) -> bytes:
    stage, final = _receipt_paths(root, receipt, creative_id)
    candidate = stage if stage.is_file() else final
    if not candidate.is_file():
        raise LocalCanaryMediaError("STAGED_MEDIA_ARTIFACT_MISSING")
    return _verified_artifact(candidate, receipt)


def promote_staged_creative_durable(
    root: str | Path,
    *,
    creative_id: str,
    receipt: dict[str, Any],
    creative: Any,
    storage=None,
    settings=None,
) -> dict[str, Any]:
    """Promote a verified local stage to digest-addressed storage, idempotently.

    The returned receipt is safe to persist in ``PinCreative.render_spec``.
    Local staging is retained until the caller commits that receipt; this lets
    a failed DB finalization retry safely. If the receipt commit is interrupted,
    the deterministic object key lets reconciliation rediscover the object.
    """
    if not durable_media_required(settings):
        raise LocalCanaryMediaError("DURABLE_MEDIA_EXPOSED_RUNTIME_REQUIRED")
    root_path = _safe_root(root)
    creative_id = _safe_creative_id(creative_id)
    if (
        not isinstance(receipt, dict)
        or getattr(creative, "render_status", None) != "STAGED"
    ):
        raise LocalCanaryMediaError("STAGED_MEDIA_RECEIPT_INVALID")
    receipt_root = _safe_root(receipt.get("local_root"))
    stage, final = _receipt_paths(receipt_root, receipt, creative_id)
    digest = receipt["artifact_sha256"]
    if _validate_durable_provenance(creative, receipt) != creative_id:
        raise LocalCanaryMediaError("DURABLE_MEDIA_PROVENANCE_MISMATCH")
    adapter = _media_storage(storage, settings=settings)
    try:
        expected_key = media_key("creative", creative_id, digest)
        if adapter.key(creative_id, digest) != expected_key:
            raise LocalCanaryMediaError("DURABLE_MEDIA_KEY_INVALID")
        if not isinstance(expected_key, str) or not expected_key:
            raise LocalCanaryMediaError("DURABLE_MEDIA_KEY_INVALID")
        if receipt.get("durable_protocol") is not None and (
            receipt.get("durable_protocol") != DURABLE_CANARY_PROTOCOL
            or receipt.get("storage_key") != expected_key
            or receipt.get("durable_receipt_fingerprint")
            != _durable_receipt_fingerprint(creative_id, receipt)
        ):
            raise LocalCanaryMediaError("DURABLE_MEDIA_RECEIPT_INVALID")
        # A pre-existing object is a recovery boundary, not permission to
        # overwrite it. Verify it first; corruption fails closed.
        try:
            stored = adapter.read(creative_id, digest)
        except StorageMissing:
            if receipt.get("durable_protocol") is not None:
                raise LocalCanaryMediaError("DURABLE_MEDIA_OBJECT_MISSING")
            stored = None
        if stored is not None:
            if (
                not isinstance(stored, bytes)
                or not stored.startswith(PNG_SIGNATURE)
                or hashlib.sha256(stored).hexdigest() != digest
                or len(stored) != receipt.get("artifact_size")
            ):
                raise LocalCanaryMediaError("DURABLE_MEDIA_OBJECT_MISMATCH")
        else:
            if root_path != receipt_root:
                raise LocalCanaryMediaError("STAGED_MEDIA_ROOT_MISMATCH")
            if stage.is_file() or final.is_file():
                local = _verified_local_stage_or_final(receipt_root, creative_id, receipt)
            else:
                raise LocalCanaryMediaError("STAGED_MEDIA_ARTIFACT_MISSING")
            written_key = adapter.write(creative_id, local)
            if written_key != expected_key:
                raise LocalCanaryMediaError("DURABLE_MEDIA_KEY_INVALID")
            stored = adapter.read(creative_id, digest)
            if (
                not isinstance(stored, bytes)
                or not stored.startswith(PNG_SIGNATURE)
                or hashlib.sha256(stored).hexdigest() != digest
                or len(stored) != receipt.get("artifact_size")
                or stored != local
            ):
                raise LocalCanaryMediaError("DURABLE_MEDIA_READBACK_MISMATCH")
        durable_receipt = {
            **receipt,
            "durable_protocol": DURABLE_CANARY_PROTOCOL,
            "storage_key": expected_key,
        }
        durable_receipt["durable_receipt_fingerprint"] = (
            _durable_receipt_fingerprint(creative_id, durable_receipt)
        )
        return durable_receipt
    except LocalCanaryMediaError:
        raise
    except (StorageCorrupt, StorageUnavailable) as exc:
        raise LocalCanaryMediaError("DURABLE_MEDIA_STORAGE_UNAVAILABLE") from exc
    except StorageMissing as exc:
        raise LocalCanaryMediaError("DURABLE_MEDIA_OBJECT_MISSING") from exc
    except Exception as exc:
        raise LocalCanaryMediaError("DURABLE_MEDIA_PROMOTION_FAILED") from exc


def _durable_receipt_for_creative(
    creative: Any,
    *,
    digest: str | None,
    expected_input_fingerprint: str | None,
) -> tuple[str, dict[str, Any]]:
    if creative is None:
        raise LocalCanaryMediaError("DURABLE_MEDIA_CREATIVE_REQUIRED")
    creative_id = _safe_creative_id(getattr(creative, "id", None))
    spec = getattr(creative, "render_spec", None)
    spec = spec if isinstance(spec, dict) else {}
    receipt = spec.get("local_canary_stage")
    if not isinstance(receipt, dict):
        raise LocalCanaryMediaError("DURABLE_MEDIA_RECEIPT_INVALID")
    _, _ = _receipt_paths(_safe_root(receipt.get("local_root")), receipt, creative_id)
    expected_digest = _safe_digest(
        digest if digest is not None else getattr(creative, "sha256", None),
        "DURABLE_MEDIA_RECEIPT_INVALID",
    )
    expected_source = (
        (spec.get("image") or {}).get("checksum_sha256")
        if isinstance(spec.get("image"), dict)
        else None
    )
    if (
        getattr(creative, "sha256", None) != expected_digest
        or receipt.get("artifact_sha256") != expected_digest
        or receipt.get("provenance_fingerprint")
        != _canonical_fingerprint(receipt.get("provenance"))
    ):
        raise LocalCanaryMediaError("DURABLE_MEDIA_PROVENANCE_MISMATCH")
    if expected_source and (
        not isinstance(spec.get("image"), dict)
        or spec["image"].get("checksum_sha256") != expected_source
    ):
        raise LocalCanaryMediaError("DURABLE_MEDIA_PROVENANCE_MISMATCH")
    _validate_durable_provenance(
        creative,
        receipt,
        expected_input_fingerprint=expected_input_fingerprint,
    )
    return creative_id, receipt


def read_verified_durable_creative(
    creative: Any,
    *,
    digest: str | None = None,
    expected_input_fingerprint: str | None = None,
    storage=None,
    settings=None,
    require_rendered: bool = True,
) -> bytes:
    """Read and verify a durable canary PNG, independent of local final files."""
    if require_rendered and getattr(creative, "render_status", None) != "RENDERED":
        raise LocalCanaryMediaError("DURABLE_MEDIA_NOT_RENDERED")
    creative_id, receipt = _durable_receipt_for_creative(
        creative,
        digest=digest,
        expected_input_fingerprint=expected_input_fingerprint,
    )
    if (
        receipt.get("durable_protocol") != DURABLE_CANARY_PROTOCOL
        or not durable_media_required(settings)
    ):
        raise LocalCanaryMediaError("DURABLE_MEDIA_RECEIPT_REQUIRED")
    adapter = _media_storage(storage, settings=settings)
    expected_key = media_key("creative", creative_id, receipt["artifact_sha256"])
    if (
        adapter.key(creative_id, receipt["artifact_sha256"]) != expected_key
        or receipt.get("storage_key") != expected_key
        or receipt.get("durable_receipt_fingerprint")
        != _durable_receipt_fingerprint(creative_id, receipt)
    ):
        raise LocalCanaryMediaError("DURABLE_MEDIA_RECEIPT_INVALID")
    try:
        contents = adapter.read(creative_id, receipt["artifact_sha256"])
    except StorageMissing as exc:
        raise LocalCanaryMediaError("DURABLE_MEDIA_OBJECT_MISSING") from exc
    except (StorageCorrupt, StorageUnavailable) as exc:
        raise LocalCanaryMediaError("DURABLE_MEDIA_STORAGE_UNAVAILABLE") from exc
    except Exception as exc:
        raise LocalCanaryMediaError("DURABLE_MEDIA_STORAGE_UNAVAILABLE") from exc
    if (
        not isinstance(contents, bytes)
        or not contents.startswith(PNG_SIGNATURE)
        or hashlib.sha256(contents).hexdigest() != receipt.get("artifact_sha256")
        or len(contents) != receipt.get("artifact_size")
    ):
        raise LocalCanaryMediaError("DURABLE_MEDIA_OBJECT_MISMATCH")
    return contents


def read_verified_canary_creative(
    creative: Any,
    *,
    digest: str | None = None,
    expected_input_fingerprint: str | None = None,
    storage=None,
    settings=None,
) -> bytes:
    """Public-media/admission entry point for either runtime storage mode."""
    if durable_media_required(settings):
        return read_verified_durable_creative(
            creative,
            digest=digest,
            expected_input_fingerprint=expected_input_fingerprint,
            storage=storage,
            settings=settings,
        )
    return read_verified_promoted(
        creative,
        digest=digest,
        expected_input_fingerprint=expected_input_fingerprint,
        storage=storage,
        settings=settings,
    )


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
    expected_input_fingerprint: str | None = None,
    storage=None,
    settings=None,
) -> bytes:
    """Read only the promoted artifact from a valid RENDERED local receipt."""
    if creative is None or creative.render_status != "RENDERED":
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_NOT_PROMOTED")
    spec = creative.render_spec if isinstance(creative.render_spec, dict) else {}
    if durable_media_required(settings):
        return read_verified_durable_creative(
            creative,
            digest=digest,
            expected_input_fingerprint=expected_input_fingerprint,
            storage=storage,
            settings=settings,
            require_rendered=True,
        )
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


def verify_promoted_pending(
    creative: Any,
    receipt: dict[str, Any],
    *,
    storage=None,
    settings=None,
    expected_input_fingerprint: str | None = None,
) -> bytes:
    """Internal reconciliation verification; never use this in a media route."""
    if creative is None or creative.render_status != "STAGED":
        raise LocalCanaryMediaError("LOCAL_CANARY_MEDIA_NOT_PENDING")
    if durable_media_required(settings):
        if not isinstance(creative.render_spec, dict) or (
            creative.render_spec.get("local_canary_stage") != receipt
        ):
            raise LocalCanaryMediaError("DURABLE_MEDIA_PROVENANCE_MISMATCH")
        return read_verified_durable_creative(
            creative,
            digest=creative.sha256,
            storage=storage,
            settings=settings,
            expected_input_fingerprint=expected_input_fingerprint,
            require_rendered=False,
        )
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