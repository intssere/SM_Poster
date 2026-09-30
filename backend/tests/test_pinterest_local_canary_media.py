"""Focused offline tests for the local Pinterest canary media helper."""
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from app.services import pinterest_local_canary_media as local_media


def source_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (48, 32), (45, 90, 135)).save(output, format="PNG")
    return output.getvalue()


def stage_artifact(root, creative_id="creative_1", png=None):
    png = png or source_png()
    return local_media.stage_creative_png(
        root,
        creative_id=creative_id,
        png=png,
        source_image_id="source_1",
        source_sha256="a" * 64,
        input_fingerprint="b" * 64,
        provenance={"source_bytes_basis": "verified_local_input"},
    )


def test_render_verified_bytes_is_offline_and_reports_rendered_hash(monkeypatch):
    import socket
    import urllib.request

    def network_forbidden(*args, **kwargs):
        raise AssertionError("offline media rendering attempted network access")

    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", network_forbidden)
    source = source_png()
    digest = sha256(source).hexdigest()
    spec = {
        "image": {"id": "source_1", "checksum_sha256": digest},
        "template_key": "editorial_product_pick",
        "headline": "A clear product headline",
        "subheadline": "Supporting product detail",
    }

    rendered, provenance = local_media.render_verified_local_png(
        spec,
        source,
        source_image_id="source_1",
        expected_source_sha256=digest,
    )

    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert Image.open(BytesIO(rendered)).size == (1000, 1500)
    assert provenance["source_sha256"] == digest
    assert provenance["source_bytes_basis"] == "verified_local_input"
    assert provenance["source_bytes_unchanged"] is True
    assert provenance["artifact_sha256"] == sha256(rendered).hexdigest()
    assert provenance["artifact_size"] == len(rendered)


def test_render_rejects_unverified_bytes_and_mismatched_source():
    data = source_png()
    spec = {"image": {"id": "source_1"}, "template_key": "editorial_product_pick"}

    with pytest.raises(local_media.LocalCanaryMediaError, match="LOCAL_SOURCE_SHA256_MISMATCH"):
        local_media.render_verified_local_png(
            spec, data, source_image_id="source_1", expected_source_sha256="a" * 64
        )
    with pytest.raises(local_media.LocalCanaryMediaError, match="CREATIVE_SOURCE_ID_MISMATCH"):
        local_media.render_verified_local_png(
            spec,
            data,
            source_image_id="source_2",
            expected_source_sha256=sha256(data).hexdigest(),
        )


def test_staging_isolated_and_receipt_binds_artifact_hash(tmp_path):
    assert str(tmp_path).startswith("/tmp/")
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    png = source_png()
    first = stage_artifact(root_one, png=png)
    second = stage_artifact(root_two, png=png)
    receipt = first.as_dict()

    assert first.artifact_sha256 == sha256(png).hexdigest()
    assert first.artifact_size == len(png)
    assert receipt["provenance"]["artifact_sha256"] == first.artifact_sha256
    assert receipt["provenance_fingerprint"]
    assert (root_one / first.stage_path).read_bytes() == png
    assert (root_one / first.stage_path).parent.name == ".task61-6-staging"
    assert not (root_one / first.final_path).exists()
    assert (root_two / second.stage_path).exists()
    assert first.stage_path != second.stage_path
    assert not (root_one / second.stage_path).exists()


def test_promotion_is_atomic_and_idempotent(tmp_path):
    png = source_png()
    staged = stage_artifact(tmp_path, png=png)
    receipt = staged.as_dict()
    stage_path = tmp_path / staged.stage_path

    final = local_media.promote_staged_creative(
        tmp_path, creative_id="creative_1", receipt=receipt
    )

    assert final == tmp_path / staged.final_path
    assert final.read_bytes() == png
    assert not stage_path.exists()
    assert local_media.promote_staged_creative(
        tmp_path, creative_id="creative_1", receipt=receipt
    ) == final
    assert final.read_bytes() == png


def test_promotion_rejects_missing_and_corrupt_staged_artifacts(tmp_path):
    staged = stage_artifact(tmp_path)
    receipt = staged.as_dict()
    (tmp_path / staged.stage_path).unlink()
    with pytest.raises(local_media.LocalCanaryMediaError, match="STAGED_MEDIA_ARTIFACT_MISSING"):
        local_media.promote_staged_creative(
            tmp_path, creative_id="creative_1", receipt=receipt
        )

    corrupted = stage_artifact(tmp_path / "corrupt")
    (tmp_path / "corrupt" / corrupted.stage_path).write_bytes(b"not a png")
    with pytest.raises(local_media.LocalCanaryMediaError, match="STAGED_MEDIA_ARTIFACT_MISMATCH"):
        local_media.promote_staged_creative(
            tmp_path / "corrupt",
            creative_id="creative_1",
            receipt=corrupted.as_dict(),
        )


def test_receipt_validation_rejects_invalid_provenance_and_paths(tmp_path):
    staged = stage_artifact(tmp_path)
    receipt = staged.as_dict()

    bad_protocol = {**receipt, "protocol": "unknown"}
    with pytest.raises(local_media.LocalCanaryMediaError, match="STAGED_MEDIA_RECEIPT_INVALID"):
        local_media.promote_staged_creative(
            tmp_path, creative_id="creative_1", receipt=bad_protocol
        )

    bad_provenance = {**receipt, "provenance": {**receipt["provenance"], "source_sha256": "c" * 64}}
    with pytest.raises(local_media.LocalCanaryMediaError, match="STAGED_MEDIA_PROVENANCE_INVALID"):
        local_media.promote_staged_creative(
            tmp_path, creative_id="creative_1", receipt=bad_provenance
        )

    for path in ("../outside.stage", ".task61-6-staging/../../outside.stage"):
        bad_path = {**receipt, "stage_path": path}
        with pytest.raises(local_media.LocalCanaryMediaError, match="STAGED_MEDIA_PATH_INVALID"):
            local_media.promote_staged_creative(
                tmp_path, creative_id="creative_1", receipt=bad_path
            )
    with pytest.raises(local_media.LocalCanaryMediaError, match="INVALID_CREATIVE_ID"):
        local_media.promote_staged_creative(
            tmp_path, creative_id="../creative", receipt=receipt
        )


def test_orphan_cleanup_preserves_referenced_receipts_and_removes_uncommitted(tmp_path):
    referenced = stage_artifact(tmp_path, "creative_1")
    orphan = stage_artifact(tmp_path, "creative_2")
    referenced_path = tmp_path / referenced.stage_path
    orphan_path = tmp_path / orphan.stage_path

    removed = local_media.cleanup_orphan_staging(
        tmp_path,
        committed_receipts=[("creative_1", referenced.as_dict())],
    )
    assert removed == [orphan.stage_path]
    assert referenced_path.exists()
    assert not orphan_path.exists()

    # Before a receipt is committed, its stage is an orphan and may be removed.
    uncommitted = stage_artifact(tmp_path, "creative_3")
    removed_before_commit = local_media.cleanup_orphan_staging(
        tmp_path, committed_receipts=[]
    )
    assert uncommitted.stage_path in removed_before_commit
    assert not (tmp_path / uncommitted.stage_path).exists()
    assert referenced.stage_path in removed_before_commit