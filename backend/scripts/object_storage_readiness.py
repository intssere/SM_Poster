"""Opt-in, one-object SDK probe. Never invoked by application startup or health.

PASS certifies an SDK round trip, not production provenance or public-media
delivery. The caller must independently certify the execution environment.
Imports are stdlib-only until the explicit gate passes; tests inject an SDK.
"""
from __future__ import annotations

import hashlib
from contextlib import redirect_stderr, redirect_stdout
import json
import os
import re
import secrets
import signal
import sys
from datetime import datetime, timezone

PROBE_VERSION = "1"
GATE = "OBJECT_STORAGE_READINESS_PROBE_ENABLED"
PREFIX = "task61-readiness/"
EXPECTED_SHA256 = "dc5687b50f70cb95379bfced5a0eae768dd4382cd6b393ee77d65bbdd6373fbf"
EXPECTED_SIZE = 70
# Fixed, valid 1x1 RGBA PNG. No Pillow, external image, file, or business input.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63606060f80f000104010080bbd15b"
    "0000000049454e44ae426082"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
STAGES = (
    "gate", "key", "payload", "initialize", "preflight", "write", "read",
    "verify", "fresh_read", "fresh_verify", "delete", "absence",
)
SAFE_ERRORS = frozenset({
    "GATE_DISABLED", "INVALID_GATE", "INVALID_NAMESPACE", "INVALID_BYTES",
    "SIZE_MISMATCH", "PNG_SIGNATURE_MISMATCH", "DIGEST_MISMATCH",
    "BYTES_MISMATCH", "FRESH_CLIENT_NOT_DISTINCT", "SDK_CAPABILITY_MISSING",
    "ABSENCE_NOT_CONFIRMED", "KEY_NOT_CONFIRMED_UNUSED", "INTERRUPTED",
})


class ProbeFailure(Exception):
    """Safe, fixed error code; underlying SDK exceptions are never serialized."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_code(exc) -> str:
    if isinstance(exc, ProbeFailure) and exc.code in SAFE_ERRORS:
        return exc.code
    return "SDK_OPERATION_FAILED"


class _DiscardOutput:
    """Suppress SDK output without retaining potentially sensitive text."""

    def write(self, text):
        return len(text)

    def flush(self):
        pass


def _sdk_call(action, *args):
    with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
        return action(*args)


def validate_key(key: str) -> None:
    if not isinstance(key, str) or not re.fullmatch(
        r"task61-readiness/[0-9a-f]{32}\.png", key
    ):
        raise ProbeFailure("INVALID_NAMESPACE")


def validate_png(data: bytes) -> None:
    if not isinstance(data, bytes):
        raise ProbeFailure("INVALID_BYTES")
    if len(data) != EXPECTED_SIZE:
        raise ProbeFailure("SIZE_MISMATCH")
    if not data.startswith(PNG_SIGNATURE):
        raise ProbeFailure("PNG_SIGNATURE_MISMATCH")
    if hashlib.sha256(data).hexdigest() != EXPECTED_SHA256:
        raise ProbeFailure("DIGEST_MISMATCH")
    if data != PNG_BYTES:
        raise ProbeFailure("BYTES_MISMATCH")


def _load_sdk():
    # The only non-stdlib dependency; deliberately lazy and default-bucket-only.
    from replit.object_storage import Client
    from replit.object_storage.errors import ObjectNotFoundError

    return Client, ObjectNotFoundError


def _receipt(environ, injected: bool) -> dict:
    return {
        "probe_version": PROBE_VERSION,
        "object_key": None,
        "expected_digest": EXPECTED_SHA256,
        "byte_size": EXPECTED_SIZE,
        "started_at": _now(),
        "finished_at": None,
        "operations": {name: {"status": "NOT_RUN"} for name in STAGES},
        "client_evidence": {
            "backend": "replit.object_storage",
            "constructor": "Client()",
            "default_bucket_only": True,
            "mode": "injected_test" if injected else "official_sdk",
            "clients_created": 0,
            "fresh_read_distinct": False,
            "absence_client_distinct": False,
            "writer_bound_absence_verified": False,
            "default_bucket_identity_verified": False,
            "upload_acknowledged": False,
            "delete_acknowledged": False,
            "published_runtime_marker": environ.get("REPLIT_DEPLOYMENT") == "1",
            "production_runtime_certified": False,
        },
        "cleanup": {"status": "NOT_NEEDED", "attempted": False},
        "final_status": "BLOCKED",
    }


def run_probe(environ=None, sdk_loader=None) -> dict:
    """Execute at most one upload; accept no object/bucket/URL arguments.

    sdk_loader is an in-process testing seam, never a CLI parameter. SDK
    client instances must be distinct. Cleanup is armed before upload, since
    an exception does not imply that the server rejected the write.
    """
    environ = os.environ if environ is None else environ
    receipt = _receipt(environ, sdk_loader is not None)
    writer = None
    clients = []
    factory = not_found = None
    key = None
    cleanup_armed = False
    cleanup_confirmed = False
    upload_acknowledged = False

    def stage(name, action):
        result = {"status": "RUNNING", "started_at": _now()}
        receipt["operations"][name] = result
        try:
            value = action()
        except BaseException as exc:
            result.update(
                status="FAILED",
                error_code=_error_code(exc),
                finished_at=_now(),
            )
            raise
        result.update(status="PASS", finished_at=_now())
        return value

    def require_gate():
        value = environ.get(GATE, "false")
        if value == "false":
            raise ProbeFailure("GATE_DISABLED")
        if value != "true":
            raise ProbeFailure("INVALID_GATE")

    def new_client(required):
        client = _sdk_call(factory)
        receipt["client_evidence"]["clients_created"] += 1
        if any(client is previous for previous in clients):
            raise ProbeFailure("FRESH_CLIENT_NOT_DISTINCT")
        clients.append(client)
        if not all(callable(getattr(client, method, None)) for method in required):
            raise ProbeFailure("SDK_CAPABILITY_MISSING")
        return client

    def call(client, method, *args):
        validate_key(key)
        return _sdk_call(getattr(client, method), key, *args)

    def assert_absent(client):
        exists = call(client, "exists")
        if exists is not False:
            raise ProbeFailure("ABSENCE_NOT_CONFIRMED")
        try:
            call(client, "download_as_bytes")
        except not_found:
            return
        raise ProbeFailure("ABSENCE_NOT_CONFIRMED")

    def fresh_absence():
        client = new_client(("exists", "download_as_bytes"))
        receipt["client_evidence"]["absence_client_distinct"] = True
        assert_absent(client)
        # Fresh default-bucket resolution is not bucket-identity evidence.
        # Also check the original bound writer so drift cannot mask a residual.
        assert_absent(writer)
        receipt["client_evidence"]["writer_bound_absence_verified"] = True

    try:
        stage("gate", require_gate)

        def generate_key():
            generated = PREFIX + secrets.token_hex(16) + ".png"
            validate_key(generated)
            return generated

        key = stage("key", generate_key)
        receipt["object_key"] = key
        stage("payload", lambda: validate_png(PNG_BYTES))

        def initialize():
            nonlocal factory, not_found
            factory, not_found = _sdk_call(sdk_loader or _load_sdk)
            if (
                not callable(factory)
                or not isinstance(not_found, type)
                or not issubclass(not_found, Exception)
                or not_found.__module__ == "builtins"
                or not_found.__bases__ != (Exception,)
                or not not_found.__name__.endswith("ObjectNotFoundError")
            ):
                raise ProbeFailure("SDK_CAPABILITY_MISSING")
            return new_client(
                ("upload_from_bytes", "download_as_bytes", "exists", "delete")
            )

        writer = stage("initialize", initialize)

        def preflight():
            if call(writer, "exists") is not False:
                raise ProbeFailure("KEY_NOT_CONFIRMED_UNUSED")

        stage("preflight", preflight)
        cleanup_armed = True
        stage("write", lambda: call(writer, "upload_from_bytes", PNG_BYTES))
        upload_acknowledged = True
        receipt["client_evidence"]["upload_acknowledged"] = True
        data = stage("read", lambda: call(writer, "download_as_bytes"))
        stage("verify", lambda: validate_png(data))

        def fresh_read():
            reader = new_client(("download_as_bytes",))
            receipt["client_evidence"]["fresh_read_distinct"] = True
            return call(reader, "download_as_bytes")

        fresh_data = stage("fresh_read", fresh_read)
        stage("fresh_verify", lambda: validate_png(fresh_data))
        stage("delete", lambda: call(writer, "delete"))
        receipt["client_evidence"]["delete_acknowledged"] = True
        stage("absence", fresh_absence)
        cleanup_confirmed = True
        receipt["cleanup"] = {
            "status": "CONFIRMED", "attempted": True,
            "via": "normal_sequence", "finished_at": _now(),
        }
        receipt["final_status"] = "PASS"
    except BaseException as exc:
        code = _error_code(exc)
        receipt["error_code"] = code
        receipt["final_status"] = (
            "BLOCKED" if not cleanup_armed else "FAILED"
        )
    finally:
        if cleanup_armed and not cleanup_confirmed:
            cleanup = {
                "status": "FAILED", "attempted": True, "via": "finally",
                "started_at": _now(),
                "upload_acknowledged": upload_acknowledged,
            }
            receipt["cleanup"] = cleanup
            try:
                # Same writer's bound bucket, same generated key, never a scan.
                try:
                    call(writer, "delete")
                    cleanup["delete_result"] = "PASS"
                    receipt["client_evidence"]["delete_acknowledged"] = True
                except not_found:
                    cleanup["delete_result"] = "ALREADY_NOT_FOUND"
                fresh_absence()
                cleanup["absence_result"] = "PASS"
                if upload_acknowledged:
                    cleanup["status"] = "CONFIRMED"
                else:
                    # A timed-out upload could commit after this absence check.
                    cleanup["error_code"] = "UPLOAD_OUTCOME_UNCONFIRMED"
            except BaseException as exc:
                cleanup["error_code"] = _error_code(exc)
            cleanup["finished_at"] = _now()
            if cleanup["status"] != "CONFIRMED":
                receipt["final_status"] = "FAILED"
        receipt["finished_at"] = _now()
    return receipt


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        receipt = _receipt(os.environ, False)
        receipt.update(error_code="ARGUMENTS_PROHIBITED", finished_at=_now())
    else:
        receipt = run_probe()
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return {"PASS": 0, "FAILED": 1, "BLOCKED": 2}[receipt["final_status"]]


def _interrupt(signum, frame):
    raise ProbeFailure("INTERRUPTED")


if __name__ == "__main__":
    # SIGKILL cannot be handled; receipts never claim unconditional cleanup.
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    raise SystemExit(main())