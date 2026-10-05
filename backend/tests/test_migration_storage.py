"""Adapter contract tests: patched HTTP and SDK factories, no live clients."""
import io
import json
from types import SimpleNamespace

import pytest

from app.services.media_storage import StorageUnavailable, StorageMissing
from app.services.s3_media_storage import S3Config
from app.state_transfer.migration_storage import ReplitExactReader, S3ExactTarget
from tests.test_media_migration import PNG

KEY = "creative/fixture/" + "a" * 64 + ".png"
CONFIG = S3Config("https://objects.railway.example", "fixture-bucket", "test", "test")
BINDINGS = [{"key": KEY}]


class FakeS3:
    def __init__(self):
        self.values, self.calls, self.callbacks = {}, [], []
        self.meta = SimpleNamespace(events=SimpleNamespace(register=lambda event, callback:
                                                           self.callbacks.append(callback)))
        self.repeat_send = False
        self.error = None

    def send(self, method):
        request = SimpleNamespace(url=CONFIG.endpoint + "/" + CONFIG.bucket + "/" + KEY,
                                  method=method)
        for cb in self.callbacks:
            cb(request=request)
        if self.repeat_send:
            for cb in self.callbacks:
                cb(request=request)

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        self.send("GET")
        if self.error:
            error = RuntimeError("SECRET_ERROR")
            error.response = {"Error": {"Code": self.error}}
            raise error
        return {"Body": io.BytesIO(self.values.get(kwargs["Key"], PNG))}

    def put_object(self, **kwargs):
        self.send("PUT")
        self.calls.append(("put", kwargs))
        assert kwargs["IfNoneMatch"] == "*"
        assert kwargs["Key"] not in self.values
        self.values[kwargs["Key"]] = kwargs["Body"]

    def close(self):
        pass


def target(client=None):
    client = client or FakeS3()
    return S3ExactTarget(CONFIG, BINDINGS, client_factory=lambda _: client), client


def test_bounded_exact_get_conditional_put_and_one_send():
    adapter, client = target()
    assert adapter.get(KEY, len(PNG)) == PNG
    assert client.calls[0][1] == {"Bucket": CONFIG.bucket, "Key": KEY, "Range": f"bytes=0-{len(PNG)}"}
    adapter.put_missing(KEY, PNG)
    assert client.calls[-1][1]["IfNoneMatch"] == "*"
    assert client.calls[-1][1]["ContentType"] == "image/png"


def test_sdk_retry_or_redirect_cannot_make_second_send():
    adapter, client = target()
    client.repeat_send = True
    with pytest.raises(StorageUnavailable):
        adapter.get(KEY, len(PNG))
    assert len(client.calls) == 1


@pytest.mark.parametrize("code,kind", [("NoSuchKey", StorageMissing), ("404", StorageMissing),
                                     ("NoSuchBucket", StorageUnavailable),
                                     ("AccessDenied", StorageUnavailable)])
def test_exact_missing_vs_provider_error(code, kind):
    adapter, client = target()
    client.error = code
    with pytest.raises(kind) as exc:
        adapter.get(KEY, len(PNG))
    assert "SECRET_ERROR" not in str(exc.value)
    assert len(client.calls) == 1


def test_target_rejects_unknown_key_and_endpoint_redirect():
    adapter, client = target()
    with pytest.raises(Exception):
        adapter.get("unknown", 8)
    with pytest.raises(Exception):
        adapter._before_send(SimpleNamespace(url="https://other.example/" + KEY, method="GET"))
    assert client.calls == []


@pytest.fixture
def replit_fake(monkeypatch):
    import requests
    import replit.object_storage as sdk
    from google.auth.credentials import AnonymousCredentials
    calls, downloads = [], []

    def send(session, request, **kwargs):
        calls.append((request.method, request.url, kwargs["allow_redirects"]))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"bucketId": "fixture-bucket"}).encode()
        response.raw = io.BytesIO(response._content)
        return response

    class GCS:
        _credentials = AnonymousCredentials()

        def bucket(self, bucket):
            assert bucket == "fixture-bucket"
            return SimpleNamespace(blob=self.blob)

        def blob(self, key):
            def read(output, **kwargs):
                downloads.append((key, kwargs))
                self._http_internal.get("https://storage.googleapis.com/download/storage/v1/b/"
                                        "fixture-bucket/o/" + key, timeout=1)
                output.write(PNG)
            return SimpleNamespace(download_to_file=read)

        def close(self):
            pass

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(sdk, "Client", lambda bucket_id: SimpleNamespace(_Client__gcs_client=GCS()))
    return calls, downloads


def test_replit_exact_bounded_get_no_retry_no_head_or_list(replit_fake):
    reader = ReplitExactReader(BINDINGS)
    assert reader.get(KEY, len(PNG)) == PNG
    calls, downloads = replit_fake
    assert downloads == [(KEY, {"raw_download": True, "end": len(PNG), "checksum": None,
                               "retry": None, "timeout": 20})]
    assert len(calls) == 2 and all(c[0] == "GET" and c[2] is False for c in calls)
    with pytest.raises(Exception):
        reader.get(KEY, len(PNG))
    with pytest.raises(Exception):
        reader.get("unrelated", len(PNG))
    assert len(calls) == 2
    reader.close()


def test_replit_sdk_failure_never_falls_back_to_s3(replit_fake, monkeypatch):
    import replit.object_storage as sdk
    monkeypatch.setattr(sdk, "Client", lambda **kw: (_ for _ in ()).throw(RuntimeError("SECRET")))
    with pytest.raises(StorageUnavailable) as exc:
        ReplitExactReader(BINDINGS)
    assert "SECRET" not in str(exc.value)
    assert len(replit_fake[0]) == 1


def test_replit_bounded_sink_rejects_oversized_response(replit_fake):
    reader = ReplitExactReader(BINDINGS)
    with pytest.raises(StorageUnavailable):
        reader.get(KEY, 1)
    assert len(replit_fake[1]) == 1
    reader.close()
