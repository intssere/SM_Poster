"""Isolated, offline tests for the Task 61.18B object-storage readiness probe."""

import builtins
import ast
from contextlib import contextmanager, ExitStack
import hashlib
from importlib.abc import MetaPathFinder
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import unittest
from unittest import mock
import urllib.request
import zlib


# These guards are installed only while exercising the injected fake SDK.
# Pytest also collects this module during broad backend runs, so importing it
# must never change process-wide imports, networking, or environment variables.
_SCRUB_PREFIXES = (
    "REPLIT_",
    "REPL_",
    "PINTEREST_",
    "BUFFER_",
    "OPENAI_",
    "ANTHROPIC_",
    "AWS_",
    "GOOGLE_",
    "OBJECT_STORAGE_",
    "SUPABASE_",
    "FIREBASE_",
    "CLOUDINARY_",
    "DATABASE_",
    "AZURE_",
    "CLOUDFLARE_",
    "FACEBOOK_",
    "INSTAGRAM_",
    "META_",
    "POSTGRES_",
    "PG",
    "R2_",
    "SHOPIFY_",
    "STRIPE_",
    "TIKTOK_",
)
_SCRUB_SUFFIXES = (
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_API_KEY",
    "_ACCESS_KEY",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_KEY",
)
_NETWORK_ATTEMPTS = []


def _deny_network(*args, **kwargs):
    _NETWORK_ATTEMPTS.append((args, kwargs))
    raise AssertionError("network access is forbidden in isolated readiness tests")


_BLOCKED_IMPORT_ROOTS = {
    "anthropic",
    "app",
    "azure",
    "backend",
    "boto3",
    "botocore",
    "buffer",
    "business",
    "cloudinary",
    "database",
    "db",
    "firebase_admin",
    "google",
    "httpx",
    "openai",
    "pinterest",
    "models",
    "minio",
    "psycopg",
    "psycopg2",
    "replit",
    "requests",
    "services",
    "repositories",
    "s3fs",
    "sqlalchemy",
    "sqlite3",
    "supabase",
}
_BLOCKED_IMPORT_ATTEMPTS = []
_original_import = builtins.__import__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if (
        root in _BLOCKED_IMPORT_ROOTS
        or (
            root not in sys.stdlib_module_names
            and root not in {"test_object_storage_readiness", "isolated_object_storage_readiness"}
        )
    ):
        _BLOCKED_IMPORT_ATTEMPTS.append(name)
        raise AssertionError("business, database, and provider imports are forbidden")
    return _original_import(name, globals, locals, fromlist, level)


class _BlockedImportFinder(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if (
            root in _BLOCKED_IMPORT_ROOTS
            or (
                root not in sys.stdlib_module_names
                and root not in {"test_object_storage_readiness", "isolated_object_storage_readiness"}
            )
        ):
            _BLOCKED_IMPORT_ATTEMPTS.append(fullname)
            raise AssertionError("business, database, and provider imports are forbidden")
        return None


@contextmanager
def isolated_fake_sdk():
    """Keep all deny hooks local to a fake-SDK operation and restore them."""
    safe_env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(_SCRUB_PREFIXES) and not name.endswith(_SCRUB_SUFFIXES)
    }
    _NETWORK_ATTEMPTS.clear()
    _BLOCKED_IMPORT_ATTEMPTS.clear()
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, safe_env, clear=True))
        for target, name in (
            (socket, "create_connection"), (socket, "getaddrinfo"),
            (socket.socket, "connect"), (socket.socket, "connect_ex"),
            (socket.socket, "send"), (socket.socket, "sendall"),
            (socket.socket, "sendto"), (socket.socket, "sendmsg"),
            (urllib.request, "urlopen"),
        ):
            stack.enter_context(mock.patch.object(target, name, _deny_network))
        stack.enter_context(mock.patch.object(builtins, "__import__", _guarded_import))
        stack.enter_context(mock.patch.object(sys, "meta_path", [_BlockedImportFinder(), *sys.meta_path]))
        yield

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "object_storage_readiness.py"
_SPEC = importlib.util.spec_from_file_location(
    "isolated_object_storage_readiness", _SCRIPT_PATH
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load readiness script at {_SCRIPT_PATH}")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


GATE = "OBJECT_STORAGE_READINESS_PROBE_ENABLED"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class FakeObjectNotFoundError(Exception):
    """Typed missing-object exception supplied by the injected fake SDK."""


class BroadSdkError(Exception):
    """Intentionally too broad to be treated as an exact not-found type."""


class BroadObjectNotFoundError(BroadSdkError):
    pass


class FakeStorage:
    """Shared in-memory objects plus per-client evidence; never touches disk."""

    def __init__(self):
        self.objects = {}
        self.bucket_objects = {"default": self.objects}
        self.client_bucket_ids = {}
        self.events = []
        self.clients = []
        self.reuse_client = False
        self._reused_client = None
        self.noop_delete = False
        self.emit_sdk_output = False
        self.object_not_found_type = FakeObjectNotFoundError
        self.upload_calls = []
        self.download_calls = []
        self.exists_calls = []
        self.delete_calls = []
        self.constructor_calls = []
        self.constructor_failures = {}
        self.operation_failures = {}
        self.one_shot_failures = set()
        self.read_overrides = {}
        self.exists_overrides = {}
        self.force_collision = False
        self.ambiguous_upload = False
        self.loader_calls = 0

    def _maybe_fail(self, operation, client_number):
        key = (operation, client_number)
        failure = self.operation_failures.get(key)
        if failure is not None:
            if key in self.one_shot_failures:
                self.one_shot_failures.remove(key)
                self.operation_failures.pop(key, None)
            raise failure

    def _emit_sdk_output(self):
        if self.emit_sdk_output:
            print("SDK_STDOUT_SECRET_SENTINEL")
            print("SDK_STDERR_SECRET_SENTINEL", file=sys.stderr)

    def _objects_for(self, bucket_id):
        return self.bucket_objects.setdefault(bucket_id, {})

    def make_client_factory(self):
        storage = self

        class Client:
            def __new__(cls, *args, **kwargs):
                if storage.reuse_client and storage._reused_client is not None:
                    return storage._reused_client
                instance = super().__new__(cls)
                if storage.reuse_client:
                    storage._reused_client = instance
                return instance

            def __init__(self, *args, **kwargs):
                storage._emit_sdk_output()
                client_number = len(storage.constructor_calls) + 1
                storage.constructor_calls.append((client_number, args, kwargs))
                storage.events.append(("construct", client_number, args, kwargs))
                failure = storage.constructor_failures.get(client_number)
                if failure is not None:
                    raise failure
                if hasattr(self, "client_number"):
                    return
                self.client_number = client_number
                self.bucket_id = storage.client_bucket_ids.get(client_number, "default")
                storage.clients.append(self)

            def upload_from_bytes(self, key, data):
                storage._emit_sdk_output()
                number = self.client_number
                storage.upload_calls.append((number, key, data))
                storage.events.append(("upload", number, key, data))
                storage._maybe_fail("upload", number)
                storage._objects_for(self.bucket_id)[key] = data
                if storage.ambiguous_upload:
                    raise RuntimeError("RAW_UPLOAD_EXCEPTION_SENTINEL")

            def download_as_bytes(self, key):
                storage._emit_sdk_output()
                number = self.client_number
                storage.download_calls.append((number, key))
                storage.events.append(("download", number, key))
                storage._maybe_fail("download", number)
                override = storage.read_overrides.get((number, key))
                if isinstance(override, BaseException):
                    raise override
                if override is not None:
                    return override
                bucket = storage._objects_for(self.bucket_id)
                if key not in bucket:
                    raise storage.object_not_found_type("typed missing object")
                return bucket[key]

            def exists(self, key):
                storage._emit_sdk_output()
                number = self.client_number
                storage.exists_calls.append((number, key))
                storage.events.append(("exists", number, key))
                storage._maybe_fail("exists", number)
                override = storage.exists_overrides.get((number, key))
                if override is not None:
                    return override
                return storage.force_collision or key in storage._objects_for(self.bucket_id)

            def delete(self, key):
                storage._emit_sdk_output()
                number = self.client_number
                storage.delete_calls.append((number, key))
                storage.events.append(("delete", number, key))
                storage._maybe_fail("delete", number)
                if not storage.noop_delete:
                    storage._objects_for(self.bucket_id).pop(key, None)

        return Client

    def loader(self):
        self._emit_sdk_output()
        self.loader_calls += 1
        return self.make_client_factory(), self.object_not_found_type


def enabled(storage, *, environ=None):
    environment = {GATE: "true"}
    if environ:
        environment.update(environ)
    with isolated_fake_sdk():
        return probe.run_probe(environ=environment, sdk_loader=storage.loader)


class ObjectStorageReadinessTests(unittest.TestCase):
    def test_collection_does_not_install_process_wide_guards(self):
        self.assertIs(builtins.__import__, _original_import)
        self.assertNotIn(_guarded_import, (builtins.__import__,))
        self.assertFalse(any(isinstance(finder, _BlockedImportFinder) for finder in sys.meta_path))
        self.assertIsNot(socket.create_connection, _deny_network)
        self.assertIsNot(urllib.request.urlopen, _deny_network)

    def test_constants_and_deterministic_png_are_valid(self):
        self.assertEqual(probe.PREFIX, "task61-readiness/")
        self.assertEqual(probe.EXPECTED_SIZE, 70)
        self.assertEqual(len(probe.PNG_BYTES), 70)
        self.assertEqual(hashlib.sha256(probe.PNG_BYTES).hexdigest(), probe.EXPECTED_SHA256)
        self.assertEqual(
            probe.EXPECTED_SHA256,
            "dc5687b50f70cb95379bfced5a0eae768dd4382cd6b393ee77d65bbdd6373fbf",
        )
        self.assertEqual(probe.EXPECTED_SIZE, len(probe.PNG_BYTES))
        self.assertTrue(probe.PNG_BYTES.startswith(PNG_SIGNATURE))

    def test_png_chunk_crcs_and_content_are_deterministic(self):
        image = probe.PNG_BYTES
        offset = len(PNG_SIGNATURE)
        chunks = []
        while offset < len(image):
            length = int.from_bytes(image[offset : offset + 4], "big")
            chunk_type = image[offset + 4 : offset + 8]
            chunk_data = image[offset + 8 : offset + 8 + length]
            crc_start = offset + 8 + length
            recorded_crc = int.from_bytes(image[crc_start : crc_start + 4], "big")
            self.assertEqual(recorded_crc, zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)
            chunks.append((chunk_type, chunk_data))
            offset = crc_start + 4
        self.assertEqual(offset, len(image))
        self.assertEqual([chunk[0] for chunk in chunks], [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(chunks[0][1], b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00")

    def test_validate_png_accepts_only_the_exact_fixture_bytes(self):
        self.assertIsNone(probe.validate_png(probe.PNG_BYTES))
        variants = (
            probe.PNG_BYTES[:-1],
            b"\x00" + probe.PNG_BYTES[1:],
            probe.PNG_BYTES[:-1] + bytes([probe.PNG_BYTES[-1] ^ 1]),
            probe.PNG_BYTES + b"\x00",
        )
        for value in variants:
            with self.subTest(size=len(value), digest=hashlib.sha256(value).hexdigest()):
                with self.assertRaises(probe.ProbeFailure):
                    probe.validate_png(value)

    def test_validate_key_accepts_only_the_scoped_random_png_shape(self):
        valid = probe.PREFIX + "a" * 32 + ".png"
        self.assertIsNone(probe.validate_key(valid))
        for invalid in (
            "../" + valid,
            "arbitrary/" + "a" * 32 + ".png",
            probe.PREFIX + "A" * 32 + ".png",
            probe.PREFIX + "a" * 31 + ".png",
            probe.PREFIX + "a" * 33 + ".png",
            probe.PREFIX + "a" * 32 + ".jpg",
            probe.PREFIX + "a" * 32 + ".png/extra",
            probe.PREFIX + "../" + "a" * 32 + ".png",
            probe.PREFIX + "a" * 32 + "%2f.png",
            probe.PREFIX + "é" * 32 + ".png",
        ):
            with self.subTest(key=invalid):
                with self.assertRaises(probe.ProbeFailure):
                    probe.validate_key(invalid)

    def test_absent_gate_is_blocked_without_key_or_sdk(self):
        storage = FakeStorage()
        receipt = probe.run_probe(environ={}, sdk_loader=storage.loader)
        self.assertEqual(receipt["final_status"], "BLOCKED")
        self.assertEqual(receipt["error_code"], "GATE_DISABLED")
        self.assertIsNone(receipt["object_key"])
        self.assertEqual(storage.loader_calls, 0)
        self.assertEqual(storage.constructor_calls, [])

    def test_false_gate_is_blocked_without_storage_access(self):
        storage = FakeStorage()
        receipt = probe.run_probe(environ={GATE: "false"}, sdk_loader=storage.loader)
        self.assertEqual(receipt["final_status"], "BLOCKED")
        self.assertEqual(receipt["error_code"], "GATE_DISABLED")
        self.assertIsNone(receipt["object_key"])
        self.assertEqual(storage.loader_calls, 0)
        self.assertEqual(storage.events, [])

    def test_noncanonical_gate_values_are_invalid_and_fail_closed(self):
        for value in ("", "TRUE", "True", "1", "yes", " true"):
            with self.subTest(value=value):
                storage = FakeStorage()
                receipt = probe.run_probe(environ={GATE: value}, sdk_loader=storage.loader)
                self.assertEqual(receipt["final_status"], "BLOCKED")
                self.assertEqual(receipt["error_code"], "INVALID_GATE")
                self.assertIsNone(receipt["object_key"])
                self.assertEqual(storage.loader_calls, 0)
                self.assertEqual(storage.events, [])

    def test_enabled_gate_generates_only_scoped_lowercase_random_key(self):
        storage = FakeStorage()
        receipt = enabled(storage)
        self.assertRegex(
            receipt["object_key"],
            r"\Atask61-readiness/[0-9a-f]{32}\.png\Z",
        )
        self.assertIsNone(probe.validate_key(receipt["object_key"]))

    def test_generated_invalid_key_is_blocked_before_loading_storage(self):
        storage = FakeStorage()
        with mock.patch.object(probe.secrets, "token_hex", return_value="../" + "A" * 32):
            receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "BLOCKED")
        self.assertEqual(receipt["error_code"], "INVALID_NAMESPACE")
        self.assertIsNone(receipt["object_key"])
        self.assertEqual(storage.loader_calls, 0)
        self.assertEqual(storage.constructor_calls, [])

    def test_collision_stops_before_upload_and_never_deletes(self):
        storage = FakeStorage()
        storage.force_collision = True
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "BLOCKED")
        self.assertEqual(len(storage.upload_calls), 0)
        self.assertEqual(storage.delete_calls, [])
        self.assertEqual(len(storage.constructor_calls), 1)
        self.assertEqual(storage.events[-1][0], "exists")
        self.assertEqual(receipt["cleanup"]["status"], "NOT_NEEDED")

    def test_pass_roundtrips_same_and_fresh_clients_then_deletes_once(self):
        storage = FakeStorage()
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "PASS")
        self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")
        self.assertEqual(len(storage.upload_calls), 1)
        key = receipt["object_key"]
        self.assertEqual(storage.upload_calls[0], (1, key, probe.PNG_BYTES))
        self.assertEqual(storage.download_calls[:2], [(1, key), (2, key)])
        self.assertEqual(
            storage.download_calls,
            [(1, key), (2, key), (3, key), (1, key)],
        )
        self.assertEqual(len(storage.delete_calls), 1)
        self.assertEqual(storage.delete_calls[0], (1, key))
        self.assertTrue(all(call_key == key for _, call_key in storage.exists_calls))
        self.assertTrue(all(call_key == key for _, call_key in storage.delete_calls))
        self.assertEqual(len({id(client) for client in storage.clients}), 3)
        self.assertEqual([call[0] for call in storage.exists_calls], [1, 3, 1])
        self.assertEqual([client.client_number for client in storage.clients], [1, 2, 3])
        self.assertEqual(storage.objects, {})
        self.assertEqual(storage.loader_calls, 1)
        self.assertEqual(receipt["operations"]["absence"]["status"], "PASS")
        evidence = receipt["client_evidence"]
        self.assertTrue(evidence["upload_acknowledged"])
        self.assertTrue(evidence["delete_acknowledged"])
        self.assertTrue(evidence["writer_bound_absence_verified"])
        self.assertFalse(evidence["default_bucket_identity_verified"])

    def test_pass_receipt_has_required_json_safe_fields_and_stage_order(self):
        receipt = enabled(FakeStorage())
        self.assertIsInstance(receipt["probe_version"], str)
        self.assertTrue(receipt["probe_version"])
        self.assertEqual(receipt["expected_digest"], probe.EXPECTED_SHA256)
        self.assertEqual(receipt["byte_size"], 70)
        self.assertTrue(receipt["started_at"])
        self.assertTrue(receipt["finished_at"])
        self.assertIsInstance(receipt["client_evidence"], dict)
        self.assertEqual(receipt["final_status"], "PASS")
        self.assertEqual(
            list(receipt["operations"]),
            [
                "gate",
                "key",
                "payload",
                "initialize",
                "preflight",
                "write",
                "read",
                "verify",
                "fresh_read",
                "fresh_verify",
                "delete",
                "absence",
            ],
        )
        for operation in receipt["operations"].values():
            self.assertIn("status", operation)
            self.assertIn("started_at", operation)
            self.assertIn("finished_at", operation)
        json.dumps(receipt, allow_nan=False)

    def test_sdk_clients_use_default_constructor_without_bucket_arguments(self):
        storage = FakeStorage()
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "PASS")
        self.assertEqual(storage.constructor_calls, [(1, (), {}), (2, (), {}), (3, (), {})])

    def test_primary_and_fresh_reads_are_both_content_verified(self):
        storage = FakeStorage()
        receipt = enabled(storage)
        key = receipt["object_key"]
        self.assertEqual(storage.read_overrides.get((1, key)), None)
        self.assertEqual(storage.read_overrides.get((2, key)), None)
        self.assertEqual(
            storage.download_calls,
            [(1, key), (2, key), (3, key), (1, key)],
        )

        corrupted = FakeStorage()
        # Corrupt the same-client round trip once; later writer-bound absence
        # reads must use the shared object state.
        corrupted.read_overrides = _AnyKeyReadOverride(1, b"not the fixture")
        bad_receipt = enabled(corrupted)
        self.assertEqual(bad_receipt["final_status"], "FAILED")
        self.assertEqual(bad_receipt["cleanup"]["status"], "CONFIRMED")

    def test_fresh_read_rejects_signature_size_and_digest_mismatches(self):
        invalid_payloads = (
            b"\x00" + probe.PNG_BYTES[1:],
            probe.PNG_BYTES[:-1],
            probe.PNG_BYTES[:-1] + bytes([probe.PNG_BYTES[-1] ^ 1]),
        )
        for invalid_payload in invalid_payloads:
            with self.subTest(size=len(invalid_payload)):
                storage = FakeStorage()
                storage.read_overrides = _AnyKeyReadOverride(2, invalid_payload)
                receipt = enabled(storage)
                self.assertEqual(receipt["final_status"], "FAILED")
                self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")
                self.assertEqual(
                    [number for number, _ in storage.download_calls],
                    [1, 2, 3, 1],
                )

    def test_preflight_exception_is_blocked_without_cleanup_or_upload(self):
        storage = FakeStorage()
        storage.operation_failures[("exists", 1)] = RuntimeError("preflight unavailable")
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "BLOCKED")
        self.assertEqual(receipt["cleanup"]["status"], "NOT_NEEDED")
        self.assertEqual(len(storage.upload_calls), 0)

    def test_initial_client_constructor_failure_is_reported(self):
        storage = FakeStorage()
        storage.constructor_failures[1] = RuntimeError("constructor sentinel")
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "BLOCKED")
        self.assertRegex(receipt["object_key"], r"\Atask61-readiness/[0-9a-f]{32}\.png\Z")
        self.assertEqual(len(storage.constructor_calls), 1)

    def test_builtin_and_broad_not_found_types_are_rejected_before_write(self):
        for invalid_type in (RuntimeError, ValueError, BroadObjectNotFoundError):
            with self.subTest(exception_type=invalid_type.__name__):
                storage = FakeStorage()
                storage.object_not_found_type = invalid_type
                receipt = enabled(storage)
                self.assertEqual(receipt["final_status"], "BLOCKED")
                self.assertEqual(receipt["error_code"], "SDK_CAPABILITY_MISSING")
                self.assertEqual(storage.loader_calls, 1)
                self.assertEqual(storage.constructor_calls, [])
                self.assertEqual(storage.upload_calls, [])
                self.assertEqual(receipt["operations"]["initialize"]["status"], "FAILED")

    def test_fresh_client_constructor_failure_triggers_cleanup(self):
        storage = FakeStorage()
        storage.constructor_failures[2] = RuntimeError("fresh constructor sentinel")
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(len(storage.upload_calls), 1)
        self.assertEqual(storage.delete_calls[0][0], 1)
        self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")

    def test_read_failure_is_retained_when_cleanup_confirms_absence(self):
        storage = FakeStorage()
        storage.operation_failures[("download", 1)] = RuntimeError(
            "RAW_PRIMARY_EXCEPTION_SENTINEL"
        )
        storage.one_shot_failures.add(("download", 1))
        receipt = enabled(storage)
        serialized = json.dumps(receipt)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")
        self.assertEqual(storage.delete_calls[0], (1, receipt["object_key"]))
        self.assertEqual([number for number, _ in storage.exists_calls], [1, 2, 1])
        self.assertTrue(receipt.get("error_code"))
        self.assertNotIn("RAW_PRIMARY_EXCEPTION_SENTINEL", serialized)

    def test_ambiguous_upload_after_store_is_cleaned_up(self):
        storage = FakeStorage()
        storage.ambiguous_upload = True
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(len(storage.upload_calls), 1)
        self.assertEqual(receipt["cleanup"]["status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["error_code"], "UPLOAD_OUTCOME_UNCONFIRMED")
        self.assertEqual(receipt["cleanup"]["absence_result"], "PASS")
        self.assertFalse(receipt["client_evidence"]["upload_acknowledged"])
        self.assertTrue(receipt["client_evidence"]["delete_acknowledged"])
        self.assertTrue(receipt["client_evidence"]["writer_bound_absence_verified"])
        self.assertNotIn(receipt["object_key"], storage.objects)
        self.assertNotIn("RAW_UPLOAD_EXCEPTION_SENTINEL", json.dumps(receipt))

    def test_upload_failure_before_store_still_runs_armed_cleanup(self):
        storage = FakeStorage()
        storage.operation_failures[("upload", 1)] = RuntimeError("write failed")
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(len(storage.upload_calls), 1)
        self.assertEqual(storage.delete_calls[0][0], 1)
        self.assertEqual(receipt["cleanup"]["status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["error_code"], "UPLOAD_OUTCOME_UNCONFIRMED")

    def test_delete_failure_makes_cleanup_failed(self):
        storage = FakeStorage()
        storage.operation_failures[("delete", 1)] = RuntimeError(
            "RAW_DELETE_EXCEPTION_SENTINEL"
        )
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["status"], "FAILED")
        self.assertFalse(receipt["client_evidence"]["delete_acknowledged"])
        self.assertNotIn("RAW_DELETE_EXCEPTION_SENTINEL", json.dumps(receipt))

    def test_other_bucket_absence_and_noop_delete_cannot_hide_writer_residual(self):
        storage = FakeStorage()
        storage.client_bucket_ids.update({3: "drifted", 4: "drifted"})
        storage.noop_delete = True
        receipt = enabled(storage)
        key = receipt["object_key"]
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["error_code"], "ABSENCE_NOT_CONFIRMED")
        self.assertEqual(receipt["cleanup"]["status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["error_code"], "ABSENCE_NOT_CONFIRMED")
        self.assertIn(key, storage.objects)
        self.assertNotIn(key, storage._objects_for("drifted"))
        self.assertTrue(receipt["client_evidence"]["delete_acknowledged"])
        self.assertFalse(receipt["client_evidence"]["writer_bound_absence_verified"])
        self.assertFalse(receipt["client_evidence"]["default_bucket_identity_verified"])

    def test_reused_client_is_rejected_and_cleanup_does_not_claim_confirmation(self):
        storage = FakeStorage()
        storage.reuse_client = True
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["error_code"], "FRESH_CLIENT_NOT_DISTINCT")
        self.assertEqual(receipt["cleanup"]["status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["error_code"], "FRESH_CLIENT_NOT_DISTINCT")
        self.assertEqual(
            receipt["operations"]["fresh_read"]["error_code"],
            "FRESH_CLIENT_NOT_DISTINCT",
        )
        self.assertEqual(len(storage.upload_calls), 1)
        self.assertEqual(storage.objects, {})

    def test_unknown_probe_failure_text_is_normalized_to_safe_sdk_code(self):
        storage = FakeStorage()
        storage.operation_failures[("download", 1)] = probe.ProbeFailure(
            "PRIVATE_CODE_SECRET_SENTINEL"
        )
        storage.one_shot_failures.add(("download", 1))
        receipt = enabled(storage)
        serialized = json.dumps(receipt)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["error_code"], "SDK_OPERATION_FAILED")
        self.assertNotIn("PRIVATE_CODE_SECRET_SENTINEL", serialized)

    def test_interruption_after_upload_still_runs_cleanup(self):
        for interruption, expected_code in (
            (probe.ProbeFailure("INTERRUPTED"), "INTERRUPTED"),
            (KeyboardInterrupt("RAW_INTERRUPT_SENTINEL"), "SDK_OPERATION_FAILED"),
        ):
            with self.subTest(interruption=type(interruption).__name__):
                storage = FakeStorage()
                storage.operation_failures[("download", 1)] = interruption
                storage.one_shot_failures.add(("download", 1))
                receipt = enabled(storage)
                self.assertEqual(receipt["final_status"], "FAILED")
                self.assertEqual(receipt["error_code"], expected_code)
                self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")
                self.assertEqual(storage.objects, {})
                self.assertNotIn("RAW_INTERRUPT_SENTINEL", json.dumps(receipt))

    def test_absence_exists_true_fails_closed(self):
        storage = FakeStorage()
        # Bind the generated key after upload without relying on its random value.
        storage.exists_overrides = _AnyKeyExistsOverride(3, True)
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")

    def test_absence_download_requires_typed_not_found_error(self):
        storage = FakeStorage()
        storage.operation_failures[("download", 3)] = RuntimeError(
            "RAW_ABSENCE_EXCEPTION_SENTINEL"
        )
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")
        self.assertNotIn("RAW_ABSENCE_EXCEPTION_SENTINEL", json.dumps(receipt))

    def test_typed_absence_is_required_and_arbitrary_errors_are_not_missing(self):
        storage = FakeStorage()
        storage.operation_failures[("download", 3)] = ValueError("not a typed 404")
        receipt = enabled(storage)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["status"], "CONFIRMED")

    def test_cleanup_retains_original_failure_if_cleanup_also_fails(self):
        storage = FakeStorage()
        storage.operation_failures[("download", 1)] = RuntimeError(
            "RAW_PRIMARY_SENTINEL"
        )
        storage.operation_failures[("delete", 1)] = RuntimeError(
            "RAW_CLEANUP_SENTINEL"
        )
        receipt = enabled(storage)
        serialized = json.dumps(receipt)
        self.assertEqual(receipt["final_status"], "FAILED")
        self.assertEqual(receipt["cleanup"]["status"], "FAILED")
        self.assertNotIn("RAW_PRIMARY_SENTINEL", serialized)
        self.assertNotIn("RAW_CLEANUP_SENTINEL", serialized)
        self.assertEqual(receipt["error_code"], "SDK_OPERATION_FAILED")
        self.assertEqual(receipt["cleanup"]["error_code"], "SDK_OPERATION_FAILED")

    def test_invalid_cli_arguments_emit_one_safe_json_receipt_without_sdk(self):
        output = io.StringIO()
        with mock.patch.object(probe, "run_probe") as run_probe:
            with mock.patch("sys.stdout", output):
                result = probe.main(argv=["--credential=CLI_SECRET_SENTINEL"])
        self.assertEqual(result, 2)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        receipt = json.loads(lines[0])
        self.assertEqual(receipt["final_status"], "BLOCKED")
        self.assertNotIn("CLI_SECRET_SENTINEL", output.getvalue())
        self.assertEqual(run_probe.call_count, 0)

    def test_main_suppresses_malicious_sdk_output_and_emits_only_json(self):
        storage = FakeStorage()
        storage.emit_sdk_output = True
        stdout = io.StringIO()
        stderr = io.StringIO()
        with isolated_fake_sdk():
            with mock.patch.dict(os.environ, {GATE: "true"}, clear=True):
                with mock.patch.object(probe, "_load_sdk", side_effect=storage.loader):
                    with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                        result = probe.main(argv=[])
        lines = stdout.getvalue().splitlines()
        self.assertEqual(result, 0)
        self.assertEqual(len(lines), 1)
        receipt = json.loads(lines[0])
        self.assertEqual(receipt["final_status"], "PASS")
        self.assertEqual(stderr.getvalue(), "")
        combined_output = stdout.getvalue() + stderr.getvalue() + json.dumps(receipt)
        self.assertNotIn("SDK_STDOUT_SECRET_SENTINEL", combined_output)
        self.assertNotIn("SDK_STDERR_SECRET_SENTINEL", combined_output)

    def test_probe_records_runtime_and_injected_test_mode_separately(self):
        receipt = enabled(FakeStorage(), environ={"REPLIT_DEPLOYMENT": "1"})
        evidence = receipt["client_evidence"]
        self.assertTrue(evidence["published_runtime_marker"])
        self.assertEqual(evidence["mode"], "injected_test")
        self.assertFalse(evidence["production_runtime_certified"])
        self.assertEqual(receipt["final_status"], "PASS")

    def test_script_imports_only_stdlib_with_sdk_deferred_inside_loader(self):
        tree = ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        allowed_stdlib_roots = {
            "__future__",
            "contextlib",
            "datetime",
            "hashlib",
            "json",
            "os",
            "re",
            "secrets",
            "signal",
            "sys",
        }
        deferred_sdk_modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".", 1)[0], allowed_stdlib_roots)
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".", 1)[0]
                if root == "replit":
                    deferred_sdk_modules.append(node.module)
                    parent = parents.get(node)
                    while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        parent = parents.get(parent)
                    self.assertIsNotNone(parent)
                    self.assertEqual(parent.name, "_load_sdk")
                else:
                    self.assertIn(root, allowed_stdlib_roots)
        self.assertEqual(
            deferred_sdk_modules,
            ["replit.object_storage", "replit.object_storage.errors"],
        )

    def test_enabled_path_with_fake_sdk_never_imports_business_db_or_provider_code(self):
        receipt = enabled(FakeStorage())
        self.assertIn(receipt["final_status"], ("PASS", "FAILED"))
        self.assertEqual(_BLOCKED_IMPORT_ATTEMPTS, [])
        self.assertEqual(_NETWORK_ATTEMPTS, [])
        self.assertIs(builtins.__import__, _original_import)


class _AnyKeyReadOverride(dict):
    def __init__(self, client_number, value):
        super().__init__()
        self.client_number = client_number
        self.value = value
        self.remaining = 1

    def get(self, key, default=None):
        if (
            self.remaining
            and isinstance(key, tuple)
            and key[0] == self.client_number
        ):
            self.remaining -= 1
            return self.value
        return default


class _AnyKeyExistsOverride(dict):
    def __init__(self, client_number, value):
        super().__init__()
        self.client_number = client_number
        self.value = value

    def get(self, key, default=None):
        if isinstance(key, tuple) and key[0] == self.client_number:
            return self.value
        return default


if __name__ == "__main__":
    unittest.main()