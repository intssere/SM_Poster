"""Offline 404 envelopes, bounded reads, secrecy and full migration contracts."""
import io
import json
import os
import subprocess

import pytest

from app.services.media_storage import StorageMissing, StorageUnavailable
from app.state_transfer import curl_migration_storage as c, media_migration as m
from tests.test_curl_migration_storage import (
    CaptureFactory, FakeProcess, make_target, KEY, ACCESS, SECRET, PNG,
)
from tests.test_media_migration import setup, NAMES, Memory

MISSING = b"<Error><Code>NoSuchKey</Code></Error>"
DIAGNOSTIC = b"PRIVATE_PROVIDER_DIAGNOSTIC"
BAD_BODIES = [
    b"", b" ", b"<Error><Code>NoSuchBucket</Code></Error>",
    b"<Error><Code>AccessDenied</Code></Error>",
    b"<Error><Code>PermanentRedirect</Code></Error>",
    b"<Error><Code>AuthorizationHeaderMalformed</Code></Error>",
    b"<Error><Code>NotFound</Code></Error>", b"<Error><Code>404</Code></Error>",
    b"<Error><Code>nosuchkey</Code></Error>",
    b"<Error><Code>NoSuchKey </Code></Error>",
    b"<Error><Code>NoSuchKey</Code>", b"\xff\xfeinvalid",
    b"<html><Code>NoSuchKey</Code></html>", b"<Code>NoSuchKey</Code>",
    b'{"Code":"NoSuchKey"}', b"<Error><Message>NoSuchKey</Message></Error>",
    b"<Error><Code>NoSuchKey</Code><Code>NoSuchBucket</Code></Error>",
    b"<Error><Message><Code>NoSuchKey</Code></Message></Error>",
    b"<Error><Code><Name>NoSuchKey</Name></Code></Error>",
    b"<Error><Code>NoSuchKey</Code></Error>" * 2,
    b'<Error xmlns="https://unrecognized.invalid"><Code>NoSuchKey</Code></Error>',
    b'<Error><Code value="NoSuchKey">NoSuchKey</Code></Error>',
    b'<Error><Code>NoSuchKey</Code><Key>another-key</Key></Error>',
    b'<!DOCTYPE Error [<!ENTITY missing "NoSuchKey">]><Error><Code>&missing;</Code></Error>',
    b'<!DOCTYPE Error SYSTEM "https://no-network.invalid/secret"><Error><Code>NoSuchKey</Code></Error>',
    b'<!DOCTYPE Error [<!ENTITY secret SYSTEM "file:///never-read">]><Error><Code>&secret;</Code></Error>',
    b'<?provider diagnostic?><Error><Code>NoSuchKey</Code></Error>',
    MISSING + b"x" * (c.MAX_ERROR_BYTES + 1),
]


@pytest.mark.parametrize("body", BAD_BODIES)
@pytest.mark.parametrize("expected_size", [8, 10000000])
def test_ambiguous_404_unavailable_for_tiny_and_large_expected_images(body, expected_size, capsys, caplog):
    factory = CaptureFactory(body=body, status=404)
    with pytest.raises(StorageUnavailable) as error:
        make_target(factory).get(KEY, expected_size)
    assert str(error.value) == "Exact target read refused."
    assert len(factory.calls) == 1
    assert all(not os.path.exists(p) for p in factory.config_paths + factory.header_paths)
    output = capsys.readouterr()
    assert output.out == output.err == caplog.text == ""
    assert all(process.stdout.closed and process.stderr.closed for process in factory.processes)


@pytest.mark.parametrize("body", [
    MISSING,
    b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>NoSuchKey</Code></Error>',
    b'<Error xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Code>NoSuchKey</Code></Error>',
    b"<Error><Code>NoSuchKey</Code><Key>" + KEY.encode() + b"</Key></Error>",
    MISSING + b" " * (c.MAX_ERROR_BYTES - len(MISSING)),
])
@pytest.mark.parametrize("expected_size", [8, 10000000])
def test_only_bounded_s3_no_such_key_is_missing(body, expected_size):
    factory = CaptureFactory(body=body, status=404)
    with pytest.raises(StorageMissing) as error:
        make_target(factory).get(KEY, expected_size)
    assert str(error.value) == "Exact object absent."
    assert len(factory.calls) == 1
    assert all(not os.path.exists(p) for p in factory.config_paths + factory.header_paths)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 400, 500, 502, 503])
def test_missing_code_cannot_override_redirect_auth_routing_or_server_status(status):
    factory = CaptureFactory(body=MISSING, status=status)
    with pytest.raises(StorageUnavailable):
        make_target(factory).get(KEY, 10000)
    assert len(factory.calls) == 1 and factory.processes[0].killed


@pytest.mark.parametrize("returncode", [6, 7, 22, 28, 35, 63])
def test_failed_curl_even_with_no_such_key_is_unavailable(returncode):
    factory = CaptureFactory(body=MISSING, status=404, returncode=returncode)
    with pytest.raises(StorageUnavailable):
        make_target(factory).get(KEY, 10000)


@pytest.mark.parametrize("code,expected", [(b"NoSuchKey", StorageMissing),
                                         (b"AccessDenied", StorageUnavailable)])
def test_secret_bearing_diagnostics_are_never_exposed(code, expected, capsys, caplog):
    secret = ACCESS + SECRET + KEY + "https://private.invalid/token" + DIAGNOSTIC.decode()
    body = b"<Error><Code>" + code + b"</Code><Message>" + secret.encode() + b"</Message></Error>"
    factory = CaptureFactory(body=body, status=404)
    with pytest.raises(expected) as error:
        make_target(factory).get(KEY, 10000)
    assert not any(value in str(error.value) for value in [ACCESS, SECRET, KEY, "private.invalid", DIAGNOSTIC.decode()])
    assert capsys.readouterr() == ("", "") and caplog.text == ""


def test_error_read_and_parser_bound_are_independent_of_expected_image_size(monkeypatch):
    observed = []
    original = c._no_such_key
    monkeypatch.setattr(c, "_no_such_key", lambda body, key: observed.append(len(body)) or original(body, key))
    class Counting(io.BytesIO):
        total = 0
        def read(self, count=-1):
            assert 0 <= count <= c.MAX_ERROR_BYTES + 1
            data = super().read(count)
            self.total += len(data)
            return data
    factory = CaptureFactory(body=MISSING + b" " * 100000, status=404)
    pipes = []
    def spawn(argv, **kwargs):
        process = factory(argv, **kwargs)
        process.stdout = Counting(factory.body)
        pipes.append(process.stdout)
        return process
    with pytest.raises(StorageUnavailable):
        make_target(spawn).get(KEY, 10000000)
    assert observed == [] and pipes[0].total == c.MAX_ERROR_BYTES + 1


def test_no_entity_expansion_or_file_network_resolution(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: pytest.fail("DNS attempted"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("Network attempted"))
    assert not c._no_such_key(BAD_BODIES[24], KEY)
    assert not c._no_such_key(BAD_BODIES[25], KEY)


@pytest.mark.parametrize("headers", [
    b"", b"not HTTP", b"HTTP/1.1 404 no terminator",
    b"HTTP/1.1 302 redirect\r\n\r\nHTTP/1.1 404 missing\r\n\r\n",
    b"HTTP/1.1 404 fixture\r\nX: " + b"x" * c.MAX_HEADER_BYTES + b"\r\n\r\n",
])
def test_untrustworthy_status_headers_refuse_and_cleanup(headers):
    factory = CaptureFactory(body=MISSING, status=404)
    def spawn(argv, **kwargs):
        process = factory(argv, **kwargs)
        with open(factory.header_paths[-1], "wb") as stream:
            stream.write(headers)
        return process
    with pytest.raises(StorageUnavailable):
        make_target(spawn).get(KEY, 10000)
    assert all(not os.path.exists(p) for p in factory.config_paths + factory.header_paths)


def test_status_writeout_mismatch_is_unavailable():
    factory = CaptureFactory(body=MISSING, status=404)
    def spawn(argv, **kwargs):
        process = factory(argv, **kwargs)
        process.stderr = io.BytesIO(b"200")
        return process
    with pytest.raises(StorageUnavailable):
        make_target(spawn).get(KEY, 10000)


@pytest.mark.parametrize("status", [200, 206])
@pytest.mark.parametrize("size", [72, 10000])
def test_successful_payload_and_oversize_behavior_unchanged(status, size):
    payload = PNG + b"x" * (size - len(PNG))
    factory = CaptureFactory(body=payload, status=status)
    assert make_target(factory).get(KEY, size) == payload
    oversized = CaptureFactory(body=payload + b"extra", status=status)
    assert len(make_target(oversized).get(KEY, size)) == size + 1
    assert oversized.processes[0].killed


def test_default_config_and_native_reissue_paths_disabled():
    factory = CaptureFactory()
    make_target(factory).get(KEY, len(PNG))
    argv = factory.calls[0][0]
    assert argv[:2] == ["/usr/bin/curl", "-q"]
    assert "--no-location" in argv and "--http1.1" in argv
    assert argv[argv.index("--max-redirs") + 1] == "0"
    assert argv[argv.index("--retry") + 1] == "0"
    put = CaptureFactory(body=b"", status=200)
    make_target(put).put_missing(KEY, PNG)
    assert "If-None-Match: *" in put.calls[0][0] and "Expect:" in put.calls[0][0]


@pytest.mark.parametrize("body,success", [(MISSING, True), (b"", False),
                                        (BAD_BODIES[2], False), (BAD_BODIES[3], False)])
def test_complete_dry_run_is_zero_write_and_ambiguous_first_target_stops(setup, body, success, monkeypatch):
    factory = CaptureFactory(body=body, status=404)
    monkeypatch.setattr(c, "verify_curl_sigv4_capability", lambda: "/fixture/curl")
    result = m.run(database_env="FIXTURE_SOURCE_DB", roots=[setup[0]], target_envs=NAMES,
                   target_transport="curl", dry_run=True,
                   target_factory=lambda cfg, bindings: c.CurlS3ExactTarget(
                       cfg, bindings, curl_path="/fixture/curl", popen_factory=factory))
    assert result["success"] is success
    assert result["source_counts"] == {"LOCAL_VERIFIED": 17}
    assert result["target_reads"] == (17 if success else 1)
    assert result["target_put_attempts"] == result["database_writes"] == 0
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert all(argv[argv.index("--request") + 1] == "GET" for argv, kwargs in factory.calls)
    assert DIAGNOSTIC.decode() not in json.dumps(result)


def test_full_curl_conditional_put_readback_and_idempotent_rerun_unchanged(setup, monkeypatch):
    values, calls = {}, []
    factory = CaptureFactory()
    def spawn(argv, **kwargs):
        method = argv[argv.index("--request") + 1]
        key = "creative/" + argv[-1].split("/creative/", 1)[1]
        calls.append(method)
        factory.body = values.get(key, MISSING) if method == "GET" else b""
        factory.status = 200 if method == "PUT" or key in values else 404
        process = factory(argv, **kwargs)
        if method == "PUT":
            assert key not in values
            assert "If-None-Match: *" in argv
            def communicate(input=None, timeout=None):
                values[key] = input
                return b"", b"200"
            process.communicate = communicate
        return process
    monkeypatch.setattr(c, "verify_curl_sigv4_capability", lambda: "/fixture/curl")
    def invoke():
        return m.run(database_env="FIXTURE_SOURCE_DB", roots=[setup[0]], target_envs=NAMES,
                     target_transport="curl", execute=True,
                     target_factory=lambda cfg, bindings: c.CurlS3ExactTarget(
                         cfg, bindings, curl_path="/fixture/curl", popen_factory=spawn))
    first = invoke()
    assert first["success"] and first["media_certification"] == "PASS"
    assert first["target_put_attempts"] == 17 and first["target_reads"] == 34
    assert calls == ["GET"] * 17 + ["PUT", "GET"] * 17
    assert values == setup[2] and first["publishing_admission"] == "NOT_GRANTED"
    calls.clear()
    second = invoke()
    assert second["success"] and second["target_put_attempts"] == 0
    assert second["target_counts"] == {"VERIFIED_EXISTING": 17}
    assert calls == ["GET"] * 17
    assert first["transfer_fingerprint"] == second["transfer_fingerprint"]
    assert all(not os.path.exists(p) for p in factory.config_paths + factory.header_paths)


def test_native_curl_network_fence_is_installed_before_collection():
    assert subprocess.Popen.__name__ == "fenced_popen"
    with pytest.raises(AssertionError):
        subprocess.Popen(["/fixture/curl", "--config", "/never-opened"])
