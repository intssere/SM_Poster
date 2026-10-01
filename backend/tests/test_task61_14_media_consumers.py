"""Task #61.14 public-media and admission consumers, with isolated storage."""

from contextlib import nullcontext
import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.models.domain import PinPublication
from app.services import (
    local_canary_admission,
    media_storage,
    pinterest_local_canary_media as canary_media,
    public_creative_media,
)
from app.api.routes import proposals


SOURCE_PNG = b"\x89PNG\r\n\x1a\nverified-local-source"
ARTIFACT_PNG = b"\x89PNG\r\n\x1a\nrendered-canary-pixels"
SOURCE_IMAGE_ID = "source_61_14"
INPUT_FINGERPRINT = "b" * 64
CREATIVE_FINGERPRINT = "c" * 64


class MemoryBackend:
    """An explicitly injected, no-network stand-in for PNGMediaStorage."""

    def __init__(self):
        self.objects = {}
        self.unavailable = False

    def put(self, key, data):
        if self.unavailable:
            raise media_storage.StorageUnavailable("isolated backend unavailable")
        self.objects[key] = data

    def get(self, key):
        if self.unavailable:
            raise media_storage.StorageUnavailable("isolated backend unavailable")
        if key not in self.objects:
            raise media_storage.StorageMissing("isolated object missing")
        return self.objects[key]

    def exists(self, key):
        return key in self.objects


def _settings(*, exposed=True):
    return SimpleNamespace(
        is_exposed=exposed,
        public_media_base_url="https://media.example.com",
    )


def _durable_creative(tmp_path, *, exposed=True):
    settings = _settings(exposed=exposed)
    backend = MemoryBackend()
    storage = media_storage.PNGMediaStorage(
        "creative", backend=backend, settings=settings
    )
    root = tmp_path / "canary-media"
    source_sha256 = hashlib.sha256(SOURCE_PNG).hexdigest()
    spec = {
        "local_canary_protocol": canary_media.LOCAL_CANARY_PROTOCOL,
        "image": {
            "id": SOURCE_IMAGE_ID,
            "checksum_sha256": source_sha256,
            "source_bytes_unchanged": True,
        },
    }
    staged = canary_media.stage_creative_png(
        root,
        creative_id="creative_61_14",
        png=ARTIFACT_PNG,
        source_image_id=SOURCE_IMAGE_ID,
        source_sha256=source_sha256,
        input_fingerprint=INPUT_FINGERPRINT,
        provenance={
            "source_bytes_basis": "verified_local_input",
            "source_bytes_unchanged": True,
            "generation_input_fingerprint": INPUT_FINGERPRINT,
            "creative_fingerprint": CREATIVE_FINGERPRINT,
            "render_spec_fingerprint": (
                canary_media.immutable_render_spec_fingerprint(spec)
            ),
        },
    )
    receipt = staged.as_dict()
    creative = SimpleNamespace(
        id="creative_61_14",
        source_image_id=SOURCE_IMAGE_ID,
        sha256=staged.artifact_sha256,
        size_bytes=staged.artifact_size,
        rendered_size=staged.artifact_size,
        render_status="STAGED",
        creative_fingerprint=CREATIVE_FINGERPRINT,
        render_spec={**spec, "local_canary_stage": receipt},
    )
    if exposed:
        durable_receipt = canary_media.promote_staged_creative_durable(
            root,
            creative_id=creative.id,
            receipt=receipt,
            creative=creative,
            storage=storage,
            settings=settings,
        )
        creative.render_spec = {
            **creative.render_spec,
            "local_canary_stage": durable_receipt,
            "durable_media_protocol": canary_media.DURABLE_CANARY_PROTOCOL,
        }
    else:
        canary_media.promote_staged_creative(
            root, creative_id=creative.id, receipt=receipt
        )
    creative.render_status = "RENDERED"
    return creative, settings, storage, backend, root


class _RouteDB:
    no_autoflush = nullcontext()

    def __init__(self, creative):
        self.creative = creative

    def get(self, model, identity):
        return self.creative if identity == self.creative.id else None


class _AdmissionDB:
    def __init__(self, creative):
        self.creative = creative

    def scalar(self, statement):
        return self.creative.id

    def execute(self, statement):
        projected = SimpleNamespace(
            id=self.creative.id,
            render_status=self.creative.render_status,
            render_spec=self.creative.render_spec,
            sha256=self.creative.sha256,
            source_image_id=self.creative.source_image_id,
            creative_fingerprint=self.creative.creative_fingerprint,
            rendered_size=self.creative.size_bytes,
        )
        return SimpleNamespace(one_or_none=lambda: projected)


def _install_isolated_storage(monkeypatch, storage):
    # Inject at the storage boundary while retaining the real receipt, key,
    # PNG-signature and digest verifier. No SDK or provider is constructed.
    monkeypatch.setattr(
        canary_media,
        "_media_storage",
        lambda configured=None, *, settings=None: storage,
    )


def _public_response(creative, digest, *, settings, db=None):
    request = SimpleNamespace(method="GET")
    return proposals.public_creative_image(
        creative.id,
        digest,
        request,
        db or _RouteDB(creative),
    )


def _assert_not_found(call):
    with pytest.raises(HTTPException) as response:
        call()
    assert response.value.status_code == 404


def test_public_media_route_reads_verified_durable_object_from_fake_storage(
    tmp_path, monkeypatch
):
    creative, settings, storage, _, _ = _durable_creative(tmp_path)
    _install_isolated_storage(monkeypatch, storage)
    monkeypatch.setattr(proposals, "get_settings", lambda: settings)

    response = _public_response(creative, creative.sha256, settings=settings)

    assert response.status_code == 200
    assert response.media_type == "image/png"
    assert response.body == ARTIFACT_PNG
    assert hashlib.sha256(response.body).hexdigest() == creative.sha256


def test_public_media_storage_unavailable_is_503(tmp_path, monkeypatch):
    creative, settings, storage, backend, _ = _durable_creative(tmp_path)
    backend.unavailable = True
    _install_isolated_storage(monkeypatch, storage)
    monkeypatch.setattr(proposals, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as response:
        _public_response(creative, creative.sha256, settings=settings)

    assert response.value.status_code == 503


def test_public_media_missing_durable_object_is_404(tmp_path, monkeypatch):
    creative, settings, storage, backend, _ = _durable_creative(tmp_path)
    backend.objects.clear()
    _install_isolated_storage(monkeypatch, storage)
    monkeypatch.setattr(proposals, "get_settings", lambda: settings)

    _assert_not_found(
        lambda: _public_response(creative, creative.sha256, settings=settings)
    )


def test_public_media_corrupt_durable_object_is_404_not_storage_unavailable(
    tmp_path, monkeypatch
):
    creative, settings, storage, backend, _ = _durable_creative(tmp_path)
    key = storage.key(creative.id, creative.sha256)
    backend.objects[key] = ARTIFACT_PNG + b"corrupt"
    _install_isolated_storage(monkeypatch, storage)
    monkeypatch.setattr(proposals, "get_settings", lambda: settings)

    _assert_not_found(
        lambda: _public_response(creative, creative.sha256, settings=settings)
    )


@pytest.mark.parametrize("mismatch", ["wrong_key", "wrong_source", "render_spec"])
def test_real_public_verifier_rejects_receipt_and_provenance_mismatches(
    tmp_path, monkeypatch, mismatch
):
    creative, settings, storage, _, _ = _durable_creative(tmp_path)
    _install_isolated_storage(monkeypatch, storage)
    if mismatch == "wrong_key":
        monkeypatch.setattr(storage, "key", lambda object_id, digest: "wrong/key.png")
    elif mismatch == "wrong_source":
        creative.source_image_id = "different_source"
    else:
        creative.render_spec["unexpected_immutable_change"] = True

    assert public_creative_media.verified_png(
        creative, creative.sha256, settings=settings, storage=storage
    ) is None


def test_public_route_rejects_wrong_creative_and_digest_before_storage_read(
    tmp_path, monkeypatch
):
    creative, settings, storage, backend, _ = _durable_creative(tmp_path)
    _install_isolated_storage(monkeypatch, storage)
    monkeypatch.setattr(proposals, "get_settings", lambda: settings)
    key = storage.key(creative.id, creative.sha256)

    _assert_not_found(
        lambda: proposals.public_creative_image(
            "another_creative",
            creative.sha256,
            SimpleNamespace(method="GET"),
            _RouteDB(creative),
        )
    )
    _assert_not_found(
        lambda: _public_response(creative, "0" * 64, settings=settings)
    )
    assert backend.objects[key] == ARTIFACT_PNG


def test_local_only_receipt_is_rejected_in_exposed_runtime_before_storage(
    tmp_path, monkeypatch
):
    creative, _, storage, _, root = _durable_creative(
        tmp_path, exposed=False
    )
    settings = _settings(exposed=True)
    monkeypatch.setattr(
        canary_media,
        "_media_storage",
        lambda *args, **kwargs: pytest.fail(
            "local-only exposed receipt must fail before storage initialization"
        ),
    )

    assert public_creative_media.verified_png(
        creative, creative.sha256, root=root, settings=settings
    ) is None


def test_development_public_media_keeps_local_filesystem_protocol(tmp_path):
    creative, settings, _, _, root = _durable_creative(
        tmp_path, exposed=False
    )

    assert public_creative_media.verified_png(
        creative, creative.sha256, root=root, settings=settings
    ) == ARTIFACT_PNG


def test_exposed_admission_reads_real_durable_receipt_and_projects_provenance(
    tmp_path,
):
    creative, settings, storage, _, _ = _durable_creative(tmp_path)
    db = _AdmissionDB(creative)

    assert not local_canary_admission.publication_has_pending_local_canary_media(
        db,
        PinPublication(id="publication_61_14", creative_id=creative.id),
        settings=settings,
        storage=storage,
    )


def test_admission_remains_blocked_when_durable_object_is_missing(
    tmp_path,
):
    creative, settings, storage, backend, _ = _durable_creative(tmp_path)
    backend.objects.clear()

    assert local_canary_admission.publication_has_pending_local_canary_media(
        _AdmissionDB(creative),
        PinPublication(id="publication_61_14", creative_id=creative.id),
        settings=settings,
        storage=storage,
    )