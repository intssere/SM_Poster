"""Offline exact-key, secrecy, bounds, lifecycle and curl process contracts."""
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.services.media_storage import StorageCorrupt, StorageMissing, StorageUnavailable
from app.services.s3_media_storage import S3Config
from app.state_transfer import curl_migration_storage as c
from tests.curl_migration_fixture import CurlMemory

PNG = b"\x89PNG\r\n\x1a\n" + b"fixture" * 32
SHA = hashlib.sha256(PNG).hexdigest()
KEY = "creative/fixture/" + SHA + ".png"
BINDING = {"creative_id": "fixture", "sha256": SHA, "size_bytes": len(PNG), "key": KEY}
CONFIG = S3Config("https://objects.fixture.invalid", "fixture-bucket",
                  'FIXTURE_ACCESS_"\\', 'FIXTURE_SECRET_"\\', "auto", False)
BINARY = SimpleNamespace(path="/fixture/curl", verify=lambda: None)


@pytest.fixture
def wire(monkeypatch):
    backend = CurlMemory({KEY: PNG})
    monkeypatch.setattr(c.subprocess, "Popen", backend)
    return backend


def adapter(wire, config=CONFIG, binding=BINDING):
    return c.CurlExactTarget(config, [binding], binary=BINARY)


def test_exact_argv_config_secret_redaction_and_immediate_private_cleanup(wire):
    assert adapter(wire).get(KEY, len(PNG)) == PNG
    argv, config, raw, path, environment = wire.records[0]
    assert argv[:4] == ["/fixture/curl", "-q", "--config", str(path)]
    assert "--user" not in argv and "--aws-sigv4" not in argv and "--url" not in argv
    assert CONFIG.access_key not in repr(argv) and CONFIG.secret_key not in repr(argv)
    assert CONFIG.access_key not in repr(environment) and CONFIG.secret_key not in repr(environment)
    assert config["user"] == [CONFIG.access_key + ":" + CONFIG.secret_key]
    assert '\\"' in raw and "\\\\" in raw
    assert config["aws-sigv4"] == ["aws:amz:auto:s3"]
    assert config["range"] == [f"0-{len(PNG)}"]
    assert argv[argv.index("--retry") + 1] == "0"
    assert argv[argv.index("--max-redirs") + 1] == "0" and "--no-location" in argv
    assert "--http1.1" in argv
    assert argv[argv.index("--proto") + 1] == "=https"
    assert argv[argv.index("--proto-redir") + 1] == "=https"
    assert argv[argv.index("--noproxy") + 1] == "*"
    assert argv[argv.index("--max-filesize") + 1] == str(len(PNG) + 1)
    assert argv[argv.index("--connect-timeout") + 1] == "10"
    assert argv[argv.index("--max-time") + 1] == "30"
    assert not path.exists() and not path.parent.exists()
    assert wire.calls == [("GET", KEY)]


@pytest.mark.parametrize("style,endpoint,url", [
    (False, "https://objects.fixture.invalid", "https://fixture-bucket.objects.fixture.invalid/" + KEY),
    (True, "https://objects.fixture.invalid", "https://objects.fixture.invalid/fixture-bucket/" + KEY),
    (False, "https://objects.fixture.invalid:443/base", "https://fixture-bucket.objects.fixture.invalid:443/base/" + KEY),
    (True, "https://objects.fixture.invalid:8443/base/", "https://objects.fixture.invalid:8443/base/fixture-bucket/" + KEY),
])
def test_virtual_and_path_style_exact_address(wire, style, endpoint, url):
    config = replace(CONFIG, endpoint=endpoint, path_style=style)
    assert adapter(wire, config).get(KEY, len(PNG)) == PNG
    assert wire.records[0][1]["url"] == [url]


@pytest.mark.parametrize("key", [
    "unknown", KEY + "?acl", KEY + "/../other", KEY.replace("creative/", "ai-asset/"),
    "creative/fixture/" + "0" * 64 + ".png", "../" + KEY, KEY + "?list-type=2",
])
def test_unknown_keys_never_spawn(wire, key):
    target = adapter(wire)
    with pytest.raises(Exception):
        target.get(key, len(PNG))
    with pytest.raises(Exception):
        target.put_missing(key, PNG)
    assert wire.calls == []


@pytest.mark.parametrize("change", [
    {"endpoint": "http://objects.fixture.invalid"},
    {"endpoint": "https://objects.fixture.invalid/../base"},
    {"endpoint": "https://objects.fixture.invalid/%2fbase"},
    {"access_key": "name:other"}, {"access_key": "line\nsecret"},
    {"secret_key": "secret\rheader"}, {"secret_key": "secret\x00"},
])
def test_unsafe_config_fails_before_request(wire, change):
    with pytest.raises(Exception):
        adapter(wire, replace(CONFIG, **change))
    assert wire.calls == []


@pytest.mark.parametrize("change", [
    {"key": KEY + "?acl"}, {"size_bytes": 0}, {"sha256": "a" * 64},
    {"creative_id": "../fixture"},
])
def test_only_complete_immutable_bindings(wire, change):
    with pytest.raises(Exception):
        adapter(wire, binding=BINDING | change)
    assert wire.calls == []


def test_size_and_payload_cannot_override_binding(wire):
    target = adapter(wire)
    with pytest.raises(Exception):
        target.get(KEY, len(PNG) + 1)
    with pytest.raises(Exception):
        target.put_missing(KEY, PNG + b"extra")
    assert wire.calls == []


@pytest.mark.parametrize("status,body,expected", [
    (404, b"", StorageMissing),
    (404, b"<Error><Code>NoSuchKey</Code></Error>", StorageMissing),
    (404, b"<Error><Code>NoSuchBucket</Code></Error>", StorageUnavailable),
    (404, b"<Error><Code>AccessDenied</Code></Error>", StorageUnavailable),
    (404, b"<html>unknown</html>", StorageUnavailable),
    (404, b"<!DOCTYPE x><Error><Code>NoSuchKey</Code></Error>", StorageUnavailable),
    (301, b"", Exception), (307, b"", Exception), (403, b"", Exception), (500, b"", Exception),
])
def test_missing_is_distinct_from_bucket_auth_redirect_and_server_failures(wire, status, body, expected):
    wire.response = (0, status, body, b"")
    with pytest.raises(expected) as error:
        adapter(wire).get(KEY, len(PNG))
    assert CONFIG.access_key not in str(error.value) and CONFIG.secret_key not in str(error.value)
    assert len(wire.calls) == 1
    assert all(not r[3].parent.exists() for r in wire.records)


def test_correct_range_and_full_object_size(wire):
    wire.range_response = True
    assert adapter(wire).get(KEY, len(PNG)) == PNG


@pytest.mark.parametrize("range_header", [
    b"Content-Range: bytes 0-231/233\r\n",
    b"Content-Range: bytes 1-232/232\r\n",
    b"Content-Range: bytes 0-230/232\r\n", b"",
])
def test_wrong_or_missing_range_proves_conflict(wire, range_header):
    wire.response = (0, 206, PNG, range_header)
    with pytest.raises(StorageCorrupt):
        adapter(wire).get(KEY, len(PNG))
    assert len(wire.calls) == 1


@pytest.mark.parametrize("code,status,body", [
    (63, 200, b""), (63, 206, b""), (0, 416, b""),
    (0, 200, PNG + b"x"), (0, 200, PNG + b"x" * 100000),
])
def test_native_and_independent_response_caps(wire, code, status, body):
    wire.response = (code, status, body, b"")
    with pytest.raises(StorageCorrupt):
        adapter(wire).get(KEY, len(PNG))
    assert len(wire.calls) == 1 and not wire.records[0][3].parent.exists()


def test_conditional_create_and_no_retry_or_overwrite(wire):
    wire.values.clear()
    target = adapter(wire)
    target.put_missing(KEY, PNG)
    assert "Expect:" in wire.records[-1][1]["header"]
    assert wire.values == {KEY: PNG}
    assert target.get(KEY, len(PNG)) == PNG
    with pytest.raises(Exception):
        target.put_missing(KEY, PNG)
    assert wire.values == {KEY: PNG}
    assert wire.calls == [("PUT", KEY), ("GET", KEY), ("PUT", KEY)]
    assert all(not r[3].parent.exists() for r in wire.records)


def test_racing_create_is_refused(wire):
    wire.values.clear()
    wire.race = True
    with pytest.raises(Exception):
        adapter(wire).put_missing(KEY, PNG)
    assert wire.values == {} and wire.calls == [("PUT", KEY)]


def test_timeout_kills_reaps_and_cleans_without_retry(wire):
    wire.timeout = True
    with pytest.raises(StorageUnavailable) as error:
        adapter(wire).get(KEY, len(PNG))
    assert len(wire.calls) == 1 and wire.kills >= 1
    assert not wire.records[0][3].parent.exists()
    assert "FIXTURE_SECRET" not in str(error.value)


def test_spawn_exception_cleanup_and_no_secret_output(wire, monkeypatch):
    directories = []
    def refused(argv, **kwargs):
        directories.append(Path(argv[argv.index("--config") + 1]).parent)
        raise RuntimeError(CONFIG.secret_key)
    monkeypatch.setattr(c.subprocess, "Popen", refused)
    with pytest.raises(StorageUnavailable) as error:
        adapter(wire).get(KEY, len(PNG))
    assert CONFIG.secret_key not in str(error.value)
    assert directories and not directories[0].exists()


@pytest.mark.parametrize("version,help_text,accepted", [
    (b"curl 8.4.0\nProtocols: http https\n", b"--aws-sigv4 --max-filesize --proto --retry --max-time", True),
    (b"curl 8.22.0\nProtocols: http https\n", b"--aws-sigv4 --max-filesize --proto --retry --max-time", True),
    (b"curl 8.3.0\nProtocols: http https\n", b"--aws-sigv4 --max-filesize --proto --retry --max-time", False),
    (b"curl 8.4.0\nProtocols: http\n", b"--aws-sigv4 --max-filesize --proto --retry --max-time", False),
    (b"curl 8.4.0\nProtocols: https\n", b"--max-filesize --proto --retry --max-time", False),
])
def test_capability_probes_are_local_only(tmp_path, monkeypatch, version, help_text, accepted):
    binary = tmp_path / "curl"
    binary.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(c.shutil, "which", lambda name: str(binary))
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"] == c._environment()
        assert kwargs["timeout"] == 5 and kwargs["stderr"] == subprocess.DEVNULL
        return SimpleNamespace(stdout=version if "--version" in argv else help_text)
    monkeypatch.setattr(c.subprocess, "run", run)
    if accepted:
        c.check_curl_capability().verify()
    else:
        with pytest.raises(StorageUnavailable):
            c.check_curl_capability()
    assert calls[0] == [str(binary), "-q", "--version"]
    assert all(cmd[1:] in [["-q", "--version"], ["-q", "--help", "all"]] for cmd in calls)


def test_missing_binary_capability_is_safe(monkeypatch):
    monkeypatch.setattr(c.shutil, "which", lambda name: None)
    with pytest.raises(StorageUnavailable):
        c.check_curl_capability()


def test_binary_identity_change_is_refused_before_spawn(wire, tmp_path):
    binary = tmp_path / "curl"
    binary.write_bytes(b"old")
    info = binary.stat()
    checked = c.CurlBinary(str(binary), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    target = c.CurlExactTarget(CONFIG, [BINDING], binary=checked)
    binary.write_bytes(b"changed")
    with pytest.raises(Exception):
        target.get(KEY, len(PNG))
    assert wire.calls == []


def test_native_process_fence_rejects_curl_network_execution():
    # Do not invoke a real binary unless the isolation runner installed its fence.
    assert getattr(subprocess.Popen, "__name__", "") == "fenced_popen"
    with pytest.raises(AssertionError):
        subprocess.Popen(["/fixture/curl", "-q", "--config", "/never-opened"])
