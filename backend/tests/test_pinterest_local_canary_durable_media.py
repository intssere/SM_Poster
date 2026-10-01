"""Isolated Task #61.14 durable-media protocol regressions."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.orm import Session

from app.services.media_storage import (
    LocalStorage,
    PNGMediaStorage,
    StorageMissing,
    StorageUnavailable,
)
from app.services import pinterest_local_canary_media as canary_media


def source_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (48, 32), (45, 90, 135)).save(output, format="PNG")
    return output.getvalue()


class MemoryBackend:
    """No-network, process-independent stand-in for the storage protocol."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.put_count = 0
        self.unavailable = False

    def put(self, key: str, data: bytes) -> None:
        if self.unavailable:
            raise StorageUnavailable("offline test backend unavailable")
        self.put_count += 1
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        if self.unavailable:
            raise StorageUnavailable("offline test backend unavailable")
        if key not in self.objects:
            raise StorageMissing("object missing")
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


def context(tmp_path: Path):
    source = source_png()
    source_digest = sha256(source).hexdigest()
    settings = SimpleNamespace(is_exposed=True)
    backend = MemoryBackend()
    storage = PNGMediaStorage("creative", backend=backend, settings=settings)
    render_spec = {
        "image": {"id": "source_1", "checksum_sha256": source_digest},
        "template_key": "editorial_product_pick",
        "headline": "A grounded product headline",
        "local_canary_protocol": canary_media.LOCAL_CANARY_PROTOCOL,
    }
    render_spec_fingerprint = canary_media.immutable_render_spec_fingerprint(render_spec)
    staged = canary_media.stage_creative_png(
        tmp_path / "media-root",
        creative_id="creative_1",
        png=source,
        source_image_id="source_1",
        source_sha256=source_digest,
        input_fingerprint="b" * 64,
        provenance={
            "source_bytes_basis": "verified_local_input",
            "source_bytes_unchanged": True,
            "generation_input_fingerprint": "b" * 64,
            "creative_fingerprint": "c" * 64,
            "render_spec_fingerprint": render_spec_fingerprint,
        },
    )
    receipt = staged.as_dict()
    creative = SimpleNamespace(
        id="creative_1",
        source_image_id="source_1",
        sha256=staged.artifact_sha256,
        size_bytes=staged.artifact_size,
        render_status="STAGED",
        creative_fingerprint="c" * 64,
        render_spec={
            **render_spec,
            "local_canary_stage": receipt,
        },
    )
    return source, sha256(source).hexdigest(), settings, backend, storage, staged, receipt, creative


def promote(root, creative, receipt, storage, settings):
    result = canary_media.promote_staged_creative_durable(
        root,
        creative_id=creative.id,
        receipt=receipt,
        creative=creative,
        storage=storage,
        settings=settings,
    )
    creative.render_spec = {
        **creative.render_spec,
        "local_canary_stage": result,
        "durable_media_protocol": canary_media.DURABLE_CANARY_PROTOCOL,
    }
    return result


def test_local_stage_durable_promotion_and_verified_readback(tmp_path):
    _, _, settings, backend, storage, staged, receipt, creative = context(tmp_path)

    durable = promote(staged_root := Path(staged.local_root), creative, receipt, storage, settings)

    assert durable["durable_protocol"] == canary_media.DURABLE_CANARY_PROTOCOL
    assert durable["storage_key"] == storage.key(creative.id, creative.sha256)
    assert durable["durable_receipt_fingerprint"]
    assert backend.put_count == 1
    assert (staged_root / staged.stage_path).exists()
    assert canary_media.cleanup_committed_staged_creative(
        staged_root,
        creative_id=creative.id,
        receipt=durable,
    )
    assert not (staged_root / staged.stage_path).exists()
    creative.render_status = "RENDERED"
    assert canary_media.read_verified_canary_creative(
        creative, storage=storage, settings=settings
    ) == backend.get(durable["storage_key"])


def test_read_after_restart_needs_no_local_final_or_stage(tmp_path):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)
    Path(staged.local_root).joinpath(staged.final_path).unlink(missing_ok=True)
    import shutil

    shutil.rmtree(staged.local_root)
    restarted = SimpleNamespace(
        **{
            **creative.__dict__,
            "render_status": "RENDERED",
            "render_spec": {
                **creative.render_spec,
                "local_canary_stage": durable,
            },
        }
    )

    assert canary_media.read_verified_durable_creative(
        restarted, storage=storage, settings=settings
    ) == storage.read(creative.id, creative.sha256)


def test_verified_object_already_present_recovers_without_second_write(tmp_path):
    _, _, settings, backend, storage, staged, receipt, creative = context(tmp_path)
    storage.write(creative.id, (Path(staged.local_root) / staged.stage_path).read_bytes())
    assert backend.put_count == 1

    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)

    assert backend.put_count == 1
    assert durable["storage_key"] == storage.key(creative.id, creative.sha256)


def test_old_pending_receipt_recovers_existing_object_without_local_stage_on_new_root(
    tmp_path,
):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    storage.write(creative.id, Path(staged.local_root, staged.stage_path).read_bytes())
    Path(staged.local_root, staged.stage_path).unlink()

    recovered = canary_media.promote_staged_creative_durable(
        tmp_path / "other-instance-root",
        creative_id=creative.id,
        receipt=receipt,
        creative=creative,
        storage=storage,
        settings=settings,
    )

    assert recovered["durable_protocol"] == canary_media.DURABLE_CANARY_PROTOCOL
    assert storage.backend.put_count == 1


def test_storage_unavailable_fails_closed_without_local_fallback(tmp_path):
    _, _, settings, backend, storage, staged, receipt, creative = context(tmp_path)
    backend.unavailable = True

    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_STORAGE_UNAVAILABLE"):
        promote(Path(staged.local_root), creative, receipt, storage, settings)


def test_exposed_runtime_rejects_wrong_kind_local_backend_and_wrong_key(tmp_path):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    wrong_kind = PNGMediaStorage("ai-asset", backend=MemoryBackend(), settings=settings)
    local_backend = PNGMediaStorage(
        "creative",
        backend=LocalStorage(tmp_path / "isolated-local-backend"),
        settings=settings,
    )
    wrong_key = PNGMediaStorage("creative", backend=MemoryBackend(), settings=settings)
    wrong_key.key = lambda *_args: "creative/wrong/" + creative.sha256 + ".png"
    wrong_write_result = PNGMediaStorage(
        "creative", backend=MemoryBackend(), settings=settings
    )
    wrong_write_result.write = lambda *_args: "creative/wrong/" + creative.sha256 + ".png"

    for invalid_storage in (wrong_kind, local_backend, wrong_key, wrong_write_result):
        with pytest.raises(canary_media.LocalCanaryMediaError):
            canary_media.promote_staged_creative_durable(
                staged.local_root,
                creative_id=creative.id,
                receipt=receipt,
                creative=creative,
                storage=invalid_storage,
                settings=settings,
            )


def test_finalized_receipt_with_missing_object_is_not_rewritten(tmp_path):
    _, _, settings, backend, storage, staged, receipt, creative = context(tmp_path)
    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)
    before_put_count = backend.put_count
    del backend.objects[durable["storage_key"]]

    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_OBJECT_MISSING"):
        canary_media.promote_staged_creative_durable(
            staged.local_root,
            creative_id=creative.id,
            receipt=durable,
            creative=creative,
            storage=storage,
            settings=settings,
        )
    assert backend.put_count == before_put_count


def test_missing_object_and_missing_stage_cannot_finalize(tmp_path):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    Path(staged.local_root, staged.stage_path).unlink()
    with pytest.raises(canary_media.LocalCanaryMediaError, match="STAGED_MEDIA_ARTIFACT_MISSING"):
        canary_media.promote_staged_creative_durable(
            staged.local_root,
            creative_id=creative.id,
            receipt=receipt,
            creative=creative,
            storage=storage,
            settings=settings,
        )
    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_OBJECT_MISSING"):
        canary_media.read_verified_durable_creative(
            SimpleNamespace(
                **{
                    **creative.__dict__,
                    "render_status": "RENDERED",
                    "render_spec": {
                        **creative.render_spec,
                        "local_canary_stage": {
                            **receipt,
                            "durable_protocol": canary_media.DURABLE_CANARY_PROTOCOL,
                            "storage_key": storage.key(creative.id, creative.sha256),
                            "durable_receipt_fingerprint": canary_media._durable_receipt_fingerprint(
                                creative.id,
                                {
                                    **receipt,
                                    "durable_protocol": canary_media.DURABLE_CANARY_PROTOCOL,
                                    "storage_key": storage.key(creative.id, creative.sha256),
                                },
                            ),
                        },
                    },
                }
            ),
            storage=storage,
            settings=settings,
        )


def test_corrupt_digest_object_is_rejected_without_overwrite(tmp_path):
    _, _, settings, backend, storage, staged, receipt, creative = context(tmp_path)
    key = storage.key(creative.id, creative.sha256)
    backend.objects[key] = b"\x89PNG\r\n\x1a\ncorrupt"

    with pytest.raises(canary_media.LocalCanaryMediaError):
        canary_media.promote_staged_creative_durable(
            staged.local_root,
            creative_id=creative.id,
            receipt=receipt,
            creative=creative,
            storage=storage,
            settings=settings,
        )
    assert backend.put_count == 0


def test_durable_receipt_rejects_wrong_protocol_key_and_fingerprint(tmp_path):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)
    creative.render_status = "RENDERED"
    cases = (
        {**durable, "durable_protocol": "OTHER_PROTOCOL"},
        {**durable, "storage_key": "creative/other/" + creative.sha256 + ".png"},
        {**durable, "durable_receipt_fingerprint": "d" * 64},
    )
    for invalid in cases:
        creative.render_spec = {**creative.render_spec, "local_canary_stage": invalid}
        with pytest.raises(canary_media.LocalCanaryMediaError):
            canary_media.read_verified_durable_creative(
                creative, storage=storage, settings=settings
            )


def test_stale_wrong_creative_source_and_input_fingerprints_are_rejected(tmp_path):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)
    creative.render_status = "RENDERED"

    creative.render_spec = {
        **creative.render_spec,
        "local_canary_stage": {**durable, "creative_id": "creative_2"},
    }
    with pytest.raises(canary_media.LocalCanaryMediaError):
        canary_media.read_verified_durable_creative(
            creative, storage=storage, settings=settings
        )

    creative.render_spec = {**creative.render_spec, "local_canary_stage": durable}
    creative.source_image_id = "source_2"
    with pytest.raises(canary_media.LocalCanaryMediaError):
        canary_media.read_verified_durable_creative(
            creative, storage=storage, settings=settings
        )

    creative.source_image_id = "source_1"
    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_PROVENANCE_MISMATCH"):
        canary_media.read_verified_durable_creative(
            creative,
            expected_input_fingerprint="e" * 64,
            storage=storage,
            settings=settings,
        )


def test_mutated_immutable_render_spec_and_size_are_rejected(tmp_path):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)
    creative.render_status = "RENDERED"
    creative.render_spec = {**creative.render_spec, "local_canary_stage": durable}
    creative.render_spec["headline"] = "Changed after render"
    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_PROVENANCE_MISMATCH"):
        canary_media.read_verified_durable_creative(
            creative, storage=storage, settings=settings
        )

    creative.render_spec = {
        **creative.render_spec,
        "headline": "A grounded product headline",
    }
    creative.size_bytes += 1
    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_PROVENANCE_MISMATCH"):
        canary_media.read_verified_durable_creative(
            creative, storage=storage, settings=settings
        )


def test_local_only_receipt_is_rejected_in_exposed_runtime(tmp_path):
    _, _, settings, _, storage, _, _, creative = context(tmp_path)
    creative.render_status = "RENDERED"

    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_RECEIPT_REQUIRED"):
        canary_media.read_verified_canary_creative(
            creative, storage=storage, settings=settings
        )


def test_media_is_not_readable_until_rendered_and_durable_verification_passes(tmp_path):
    _, _, settings, _, storage, staged, receipt, creative = context(tmp_path)
    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)
    creative.render_spec = {**creative.render_spec, "local_canary_stage": durable}

    with pytest.raises(canary_media.LocalCanaryMediaError, match="DURABLE_MEDIA_NOT_RENDERED"):
        canary_media.read_verified_canary_creative(
            creative, storage=storage, settings=settings
        )
    creative.render_status = "RENDERED"
    assert canary_media.read_verified_canary_creative(
        creative, storage=storage, settings=settings
    )
    backend_key = durable["storage_key"]
    storage.backend.objects[backend_key] += b"tamper"
    with pytest.raises(canary_media.LocalCanaryMediaError):
        canary_media.read_verified_canary_creative(
            creative, storage=storage, settings=settings
        )


def test_interrupted_db_finalization_recovers_existing_object_without_duplicate_rows(tmp_path):
    _, _, settings, backend, storage, staged, receipt, creative = context(tmp_path)
    metadata = MetaData()
    rows = Table(
        "creative",
        metadata,
        Column("id", String, primary_key=True),
        Column("status", String, nullable=False),
        Column("digest", String, nullable=False),
        Column("render_spec", Text, nullable=False),
    )
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    import json

    with engine.begin() as connection:
        connection.execute(
            rows.insert().values(
                id=creative.id,
                status="STAGED",
                digest=creative.sha256,
                render_spec=json.dumps(creative.render_spec),
            )
        )

    durable = promote(Path(staged.local_root), creative, receipt, storage, settings)
    # Simulate process death before recording RENDERED/final schedule state.
    with Session(engine) as session:
        row = session.execute(select(rows).where(rows.c.id == creative.id)).one()
        assert row.status == "STAGED"
        session.rollback()

    recovered = canary_media.promote_staged_creative_durable(
        staged.local_root,
        creative_id=creative.id,
        receipt=receipt,
        creative=creative,
        storage=storage,
        settings=settings,
    )
    assert recovered == durable
    assert backend.put_count == 1
    with engine.begin() as connection:
        connection.execute(
            rows.update()
            .where(rows.c.id == creative.id)
            .values(status="RENDERED", render_spec=json.dumps({
                **creative.render_spec,
                "local_canary_stage": recovered,
            }))
        )
        assert connection.execute(select(rows.c.id)).all() == [(creative.id,)]


def test_orphan_staging_cleanup_keeps_committed_receipt_only(tmp_path):
    referenced = canary_media.stage_creative_png(
        tmp_path / "orphan-root",
        creative_id="creative_1",
        png=source_png(),
        source_image_id="source_1",
        source_sha256="a" * 64,
        input_fingerprint="b" * 64,
        provenance={"creative_fingerprint": "c" * 64},
    )
    orphan = canary_media.stage_creative_png(
        tmp_path / "orphan-root",
        creative_id="creative_2",
        png=source_png(),
        source_image_id="source_1",
        source_sha256="a" * 64,
        input_fingerprint="b" * 64,
        provenance={"creative_fingerprint": "c" * 64},
    )

    removed = canary_media.cleanup_orphan_staging(
        referenced.local_root,
        committed_receipts=[("creative_1", referenced.as_dict())],
    )

    assert removed == [orphan.stage_path]
    assert Path(referenced.local_root, referenced.stage_path).exists()
    assert not Path(referenced.local_root, orphan.stage_path).exists()


def test_development_filesystem_promotion_and_read_remain_supported(tmp_path):
    _, _, _, _, _, staged, receipt, creative = context(tmp_path)
    settings = SimpleNamespace(is_exposed=False)
    creative.render_spec["local_canary_stage"] = receipt

    final = canary_media.promote_staged_creative(
        staged.local_root,
        creative_id=creative.id,
        receipt=receipt,
        settings=settings,
    )

    assert isinstance(final, Path)
    assert final.read_bytes() == source_png()
    creative.render_status = "RENDERED"
    assert canary_media.read_verified_canary_creative(
        creative, digest=creative.sha256, settings=settings
    ) == source_png()