"""No live SDK/client construction: S3 operations are process-local doubles."""
import hashlib
from io import BytesIO
import json
import sys
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import media_storage, s3_media_storage as s3
from app.services.media_storage import PNGMediaStorage, StorageCorrupt, StorageMissing, StorageUnavailable


PNG = b"\x89PNG\r\n\x1a\nisolated-s3-pixels"
SECRET = "PRIVATE_STORAGE_CANARY"
CONFIG = dict(object_storage_endpoint="https://s3.example.invalid",
              object_storage_bucket="isolated-media",
              object_storage_access_key=SECRET, object_storage_secret_key=SECRET)


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(SECRET)


class FakeClient:
    def __init__(self):
        self.objects, self.calls = {}, []
        self.error = None
        self.corrupt = False

    def _check(self):
        if self.error:
            raise S3Error(self.error)

    def put_object(self, **kwargs):
        self._check()
        self.calls.append(("put", kwargs["Key"]))
        assert kwargs["ContentType"] == "image/png"
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        self._check()
        self.calls.append(("get", kwargs["Key"]))
        if kwargs["Key"] not in self.objects:
            raise S3Error("NoSuchKey")
        value = self.objects[kwargs["Key"]]
        return {"Body": BytesIO(value + b"bad" if self.corrupt else value)}

    def head_object(self, **kwargs):
        self._check()
        self.calls.append(("head", kwargs["Key"]))
        if kwargs["Key"] not in self.objects:
            raise S3Error("404")


def backend(client=None):
    client = client or FakeClient()
    return s3.S3CompatibleStorage(s3.S3Config.from_settings(SimpleNamespace(**CONFIG)),
                                  client_factory=lambda _: client), client


def test_explicit_selection_is_lazy_in_development_and_exposed(monkeypatch, tmp_path):
    monkeypatch.setattr(s3, "_boto_client", lambda *_: pytest.fail("Unexpected client construction"))
    for exposed in (False, True):
        selected = media_storage.default_storage(
            kind="creative", root=tmp_path, settings=SimpleNamespace(is_exposed=exposed, **CONFIG))
        assert isinstance(selected, s3.S3CompatibleStorage)
        assert selected._client_instance is None


@pytest.mark.parametrize("fields", [
    {}, {"object_storage_endpoint": ""}, {"object_storage_bucket": "isolated-media"},
    {"object_storage_region": "auto"}, {"object_storage_path_style": False},
    *[{k: v} for k, v in CONFIG.items()],
])
def test_default_and_partial_settings_never_replit_fallback(fields, monkeypatch, tmp_path):
    monkeypatch.setattr(media_storage, "ReplitObjectStorage",
                        lambda *_: pytest.fail("Replit fallback forbidden"))
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:", **fields)
    if not fields:
        assert isinstance(media_storage.default_storage(kind="creative", root=tmp_path,
                                                         settings=settings), media_storage.LocalStorage)
    else:
        with pytest.raises(StorageUnavailable):
            media_storage.default_storage(kind="creative", settings=settings)


def test_missing_explicit_bucket_does_not_use_legacy_default():
    fields = {k: v for k, v in CONFIG.items() if k != "object_storage_bucket"}
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:", **fields)
    with pytest.raises(StorageUnavailable):
        media_storage.default_storage(kind="creative", settings=settings)


@pytest.mark.parametrize("region,path_style", [("us-east-1", True), ("auto", True), ("eu-west-2", False)])
def test_railway_style_client_parameters_no_network(monkeypatch, region, path_style):
    made = []
    fake = FakeClient()
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(
        client=lambda *a, **kw: (made.append((a, kw)) or fake)))
    try:
        from botocore.config import Config
    except ModuleNotFoundError:
        # Locally the optional dependency need not be installed. CI installs
        # requirements and validates these arguments against real Config.
        monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(
            Config=lambda **kw: SimpleNamespace(**kw)))
    settings = SimpleNamespace(**CONFIG, object_storage_region=region,
                               object_storage_path_style=path_style, is_exposed=True)
    selected = media_storage.default_storage(kind="creative", settings=settings)
    assert made == []
    assert selected.exists("missing") is False
    args, kwargs = made[0]
    assert args == ("s3",) and kwargs["region_name"] == region
    assert kwargs["endpoint_url"] == CONFIG["object_storage_endpoint"]
    assert kwargs["aws_access_key_id"] == kwargs["aws_secret_access_key"] == SECRET
    assert kwargs["config"].s3 == {"addressing_style": "path" if path_style else "virtual"}
    assert kwargs["config"].retries["total_max_attempts"] == 1
    assert kwargs["config"].signature_version == "s3v4"
    assert kwargs["config"].request_checksum_calculation == "when_required"
    assert kwargs["config"].response_checksum_validation == "when_required"
    assert selected.exists("missing") is False and len(made) == 1


@pytest.mark.parametrize("overrides", [
    {"object_storage_endpoint": "not-an-endpoint"},
    {"object_storage_endpoint": "https://user:password@example.invalid"},
    {"object_storage_bucket": ""}, {"object_storage_secret_key": ""},
    {"object_storage_region": ""}, {"object_storage_path_style": "false"},
])
def test_invalid_config_is_safe(overrides):
    with pytest.raises(StorageUnavailable) as error:
        s3.S3Config.from_settings(SimpleNamespace(**{**CONFIG, **overrides}))
    assert SECRET not in str(error.value)


def test_verified_png_roundtrip_idempotent_key_and_corruption():
    adapter, client = backend()
    storage = PNGMediaStorage("creative", backend=adapter)
    sha = hashlib.sha256(PNG).hexdigest()
    expected = media_storage.media_key("creative", "creative-1", sha)
    assert storage.write("creative-1", PNG) == expected
    assert storage.write("creative-1", PNG) == expected
    assert list(client.objects) == [expected]
    assert storage.read("creative-1", sha) == PNG
    assert adapter.exists(expected) is True
    client.corrupt = True
    with pytest.raises(StorageCorrupt):
        storage.read("creative-1", sha)
    with pytest.raises(StorageCorrupt):
        storage.write("creative-1", b"invalid png")


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "404"])
def test_missing_objects_are_distinct(code):
    adapter, client = backend()
    client.error = code
    assert adapter.exists("missing") is False
    with pytest.raises(StorageMissing):
        adapter.get("missing")


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "InternalError"])
def test_provider_errors_do_not_become_missing_or_leak(code):
    adapter, client = backend()
    client.error = code
    for operation in (lambda: adapter.get("key"), lambda: adapter.exists("key"),
                      lambda: adapter.put("key", PNG)):
        with pytest.raises(StorageUnavailable) as error:
            operation()
        assert SECRET not in str(error.value) and error.value.__cause__ is None


def test_lazy_client_failure_is_safe_no_fallback():
    def unavailable(_):
        raise RuntimeError(SECRET)
    adapter = s3.S3CompatibleStorage(
        s3.S3Config.from_settings(SimpleNamespace(**CONFIG)), client_factory=unavailable)
    with pytest.raises(StorageUnavailable) as error:
        adapter.get("key")
    assert SECRET not in str(error.value)
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:", **CONFIG)
    assert SECRET not in repr(settings) and SECRET not in json.dumps(settings.model_dump())
    assert SECRET not in repr(adapter.config)


def test_existing_durable_canary_receipt_and_public_admission_accept_s3(tmp_path, monkeypatch):
    from tests.test_task61_14_media_consumers import (
        _durable_creative, _install_isolated_storage, _AdmissionDB)
    from app.services import pinterest_local_canary_media as canary, public_creative_media
    from app.services import local_canary_admission
    creative, settings, _, _, root = _durable_creative(tmp_path, exposed=False)
    settings.is_exposed = True
    adapter, client = backend()
    storage = PNGMediaStorage("creative", backend=adapter)
    # Use the real staged/provenance object from the existing consumer fixture.
    creative.render_status = "STAGED"
    receipt = canary.promote_staged_creative_durable(
        root, creative_id=creative.id, receipt=creative.render_spec["local_canary_stage"],
        creative=creative, storage=storage, settings=settings)
    assert receipt == canary.promote_staged_creative_durable(
        root, creative_id=creative.id, receipt=receipt, creative=creative,
        storage=storage, settings=settings)
    assert sum(name == "put" for name, _ in client.calls) == 1
    creative.render_spec["local_canary_stage"] = receipt
    creative.render_status = "RENDERED"
    _install_isolated_storage(monkeypatch, storage)
    assert canary.read_verified_promoted(creative, settings=settings, storage=storage)
    assert public_creative_media.verified_png(creative, creative.sha256, settings=settings)
    from app.models.domain import PinPublication
    assert not local_canary_admission.publication_has_pending_local_canary_media(
        _AdmissionDB(creative), PinPublication(id="p", creative_id=creative.id),
        settings=settings, storage=storage)
    # Existing consumers must still reject corruption, never admit/cache fallback.
    client.corrupt = True
    with pytest.raises(canary.LocalCanaryMediaError):
        canary.read_verified_promoted(creative, settings=settings, storage=storage)
    assert local_canary_admission.publication_has_pending_local_canary_media(
        _AdmissionDB(creative), PinPublication(id="p", creative_id=creative.id),
        settings=settings, storage=storage)