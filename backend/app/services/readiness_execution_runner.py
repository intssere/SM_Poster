"""Bounded, isolated execution of the opt-in object-storage readiness probe."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
from typing import Any

from app.services.readiness_execution_contract import (
    PROBE_DIGEST,
    PROBE_PREFIX,
    PROBE_SIZE,
    PROBE_VERSION,
    ReadinessError,
)


PROBE_PATH = (Path(__file__).resolve().parents[2] / "scripts" / "object_storage_readiness.py").resolve()
PROBE_TIMEOUT_SECONDS = 90
TERMINATION_GRACE_SECONDS = 0.75
MAX_STDOUT_BYTES = 64 * 1024

_GATE = "OBJECT_STORAGE_READINESS_PROBE_ENABLED"
_STAGES = (
    "gate", "key", "payload", "initialize", "preflight", "write", "read",
    "verify", "fresh_read", "fresh_verify", "delete", "absence",
)
_SAFE_ERRORS = frozenset({
    "GATE_DISABLED", "INVALID_GATE", "INVALID_NAMESPACE", "INVALID_BYTES",
    "SIZE_MISMATCH", "PNG_SIGNATURE_MISMATCH", "DIGEST_MISMATCH",
    "BYTES_MISMATCH", "FRESH_CLIENT_NOT_DISTINCT", "SDK_CAPABILITY_MISSING",
    "ABSENCE_NOT_CONFIRMED", "KEY_NOT_CONFIRMED_UNUSED", "INTERRUPTED",
    "SDK_OPERATION_FAILED", "UPLOAD_OUTCOME_UNCONFIRMED",
})
_KEY_PATTERN = re.compile(re.escape(PROBE_PREFIX) + r"[0-9a-f]{32}\.png\Z")
_HEX_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_FIELDS = frozenset({
    "probe_version", "object_key", "expected_digest", "byte_size",
    "started_at", "finished_at", "operations", "client_evidence", "cleanup",
    "final_status", "error_code",
})
_CLIENT_FIELDS = frozenset({
    "backend", "constructor", "default_bucket_only", "mode", "clients_created",
    "fresh_read_distinct", "absence_client_distinct",
    "writer_bound_absence_verified", "default_bucket_identity_verified",
    "upload_acknowledged", "delete_acknowledged", "published_runtime_marker",
    "production_runtime_certified",
})


def _invalid_receipt() -> ReadinessError:
    return ReadinessError("PROBE_RECEIPT_INVALID")


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _is_int(value: Any) -> bool:
    return type(value) is int


def _validate_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise _invalid_receipt()
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        raise _invalid_receipt() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_receipt()
    return parsed


def _validate_cleanup(cleanup: Any) -> str:
    if not isinstance(cleanup, dict) or not isinstance(cleanup.get("status"), str):
        raise _invalid_receipt()
    status = cleanup["status"]
    if status == "NOT_NEEDED":
        if set(cleanup) != {"status", "attempted"} or cleanup["attempted"] is not False:
            raise _invalid_receipt()
        return status

    if status == "CONFIRMED" and cleanup.get("via") == "normal_sequence":
        if set(cleanup) != {"status", "attempted", "via", "finished_at"}:
            raise _invalid_receipt()
        if cleanup["attempted"] is not True:
            raise _invalid_receipt()
        _validate_timestamp(cleanup["finished_at"])
        return status

    allowed = {
        "status", "attempted", "via", "started_at", "upload_acknowledged",
        "delete_result", "absence_result", "error_code", "finished_at",
    }
    if status not in {"FAILED", "CONFIRMED"} or set(cleanup) - allowed:
        raise _invalid_receipt()
    if cleanup.get("attempted") is not True or cleanup.get("via") != "finally":
        raise _invalid_receipt()
    cleanup_started = _validate_timestamp(cleanup.get("started_at"))
    cleanup_finished = _validate_timestamp(cleanup.get("finished_at"))
    if cleanup_finished < cleanup_started:
        raise _invalid_receipt()
    if not _is_bool(cleanup.get("upload_acknowledged")):
        raise _invalid_receipt()
    if "delete_result" in cleanup and cleanup["delete_result"] not in {
        "PASS", "ALREADY_NOT_FOUND",
    }:
        raise _invalid_receipt()
    if "absence_result" in cleanup and cleanup["absence_result"] != "PASS":
        raise _invalid_receipt()
    if "error_code" in cleanup and cleanup["error_code"] not in _SAFE_ERRORS:
        raise _invalid_receipt()
    if status == "CONFIRMED" and (
        cleanup.get("upload_acknowledged") is not True
        or cleanup.get("absence_result") != "PASS"
    ):
        raise _invalid_receipt()
    return status


def validate_receipt(receipt: Any, exit_code: int | None) -> dict:
    """Validate and return only the fixed-schema receipt the probe is allowed to emit.

    Invalid or mismatched receipts raise a fixed-code ReadinessError. This also
    makes this function suitable for sanitizing receipts read back from storage.
    """
    try:
        if not isinstance(receipt, dict) or set(receipt) - _RECEIPT_FIELDS:
            raise _invalid_receipt()
        required = _RECEIPT_FIELDS - {"error_code"}
        if not required.issubset(receipt):
            raise _invalid_receipt()
        if not _is_int(exit_code):
            raise _invalid_receipt()
        if receipt["probe_version"] != PROBE_VERSION:
            raise _invalid_receipt()
        if receipt["expected_digest"] != PROBE_DIGEST or not _HEX_DIGEST_PATTERN.fullmatch(
            receipt["expected_digest"]
        ):
            raise _invalid_receipt()
        if not _is_int(receipt["byte_size"]) or receipt["byte_size"] != PROBE_SIZE:
            raise _invalid_receipt()

        started = _validate_timestamp(receipt["started_at"])
        finished = _validate_timestamp(receipt["finished_at"])
        if finished < started:
            raise _invalid_receipt()

        key = receipt["object_key"]
        if key is not None and (
            not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key)
        ):
            raise _invalid_receipt()

        operations = receipt["operations"]
        if not isinstance(operations, dict) or set(operations) != set(_STAGES):
            raise _invalid_receipt()
        statuses = []
        for stage_name in _STAGES:
            stage = operations[stage_name]
            if not isinstance(stage, dict) or not isinstance(stage.get("status"), str):
                raise _invalid_receipt()
            status = stage["status"]
            statuses.append(status)
            if status == "NOT_RUN":
                if set(stage) != {"status"}:
                    raise _invalid_receipt()
            elif status in {"PASS", "FAILED"}:
                expected_fields = {"status", "started_at", "finished_at"}
                if status == "FAILED":
                    expected_fields.add("error_code")
                if set(stage) != expected_fields:
                    raise _invalid_receipt()
                stage_started = _validate_timestamp(stage["started_at"])
                stage_finished = _validate_timestamp(stage["finished_at"])
                if stage_finished < stage_started or stage_started < started or stage_finished > finished:
                    raise _invalid_receipt()
                if status == "FAILED" and stage["error_code"] not in _SAFE_ERRORS:
                    raise _invalid_receipt()
            else:
                # RUNNING is never a terminal receipt; unknown statuses are unsafe.
                raise _invalid_receipt()

        failed_indexes = [i for i, status in enumerate(statuses) if status == "FAILED"]
        if len(failed_indexes) > 1:
            raise _invalid_receipt()
        if failed_indexes:
            failed_at = failed_indexes[0]
            if (
                any(status != "PASS" for status in statuses[:failed_at])
                or any(status != "NOT_RUN" for status in statuses[failed_at + 1:])
            ):
                raise _invalid_receipt()
        else:
            first_not_run = next(
                (index for index, status in enumerate(statuses) if status == "NOT_RUN"),
                len(statuses),
            )
            if (
                any(status != "PASS" for status in statuses[:first_not_run])
                or any(status != "NOT_RUN" for status in statuses[first_not_run:])
            ):
                raise _invalid_receipt()
        if (operations["key"]["status"] == "PASS") != (key is not None):
            raise _invalid_receipt()

        evidence = receipt["client_evidence"]
        if not isinstance(evidence, dict) or set(evidence) != _CLIENT_FIELDS:
            raise _invalid_receipt()
        if (
            evidence["backend"] != "replit.object_storage"
            or evidence["constructor"] != "Client()"
            or evidence["default_bucket_only"] is not True
            or evidence["mode"] not in {"official_sdk", "injected_test"}
            or not _is_int(evidence["clients_created"])
            or not 0 <= evidence["clients_created"] <= 4
        ):
            raise _invalid_receipt()
        for field in _CLIENT_FIELDS - {
            "backend", "constructor", "mode", "clients_created",
        }:
            if not _is_bool(evidence[field]):
                raise _invalid_receipt()
        if (
            evidence["default_bucket_identity_verified"] is not False
            or evidence["production_runtime_certified"] is not False
        ):
            raise _invalid_receipt()

        cleanup_status = _validate_cleanup(receipt["cleanup"])
        final_status = receipt["final_status"]
        if final_status == "PASS":
            if exit_code != 0 or "error_code" in receipt:
                raise _invalid_receipt()
            if any(status != "PASS" for status in statuses):
                raise _invalid_receipt()
            if (
                evidence["mode"] != "official_sdk"
                or evidence["clients_created"] != 3
                or evidence["fresh_read_distinct"] is not True
                or evidence["absence_client_distinct"] is not True
                or evidence["writer_bound_absence_verified"] is not True
                or evidence["upload_acknowledged"] is not True
                or evidence["delete_acknowledged"] is not True
                or evidence["published_runtime_marker"] is not True
                or cleanup_status != "CONFIRMED"
                or receipt["cleanup"].get("via") != "normal_sequence"
            ):
                raise _invalid_receipt()
        elif final_status in {"FAILED", "BLOCKED"}:
            expected_exit = 1 if final_status == "FAILED" else 2
            if exit_code != expected_exit:
                raise _invalid_receipt()
            error_code = receipt.get("error_code")
            if error_code not in _SAFE_ERRORS:
                raise _invalid_receipt()
            if failed_indexes:
                if operations[_STAGES[failed_indexes[0]]].get("error_code") != error_code:
                    raise _invalid_receipt()
            if final_status == "BLOCKED":
                if (
                    not failed_indexes
                    or failed_indexes[0] >= _STAGES.index("write")
                    or cleanup_status != "NOT_NEEDED"
                ):
                    raise _invalid_receipt()
            elif cleanup_status not in {"FAILED", "CONFIRMED"} or (
                failed_indexes and failed_indexes[0] < _STAGES.index("write")
            ):
                raise _invalid_receipt()
        else:
            raise _invalid_receipt()

        # Copy known fields only; do not leak custom mapping subclasses or extras.
        return {
            key_name: (
                {stage_name: dict(operations[stage_name]) for stage_name in _STAGES}
                if key_name == "operations"
                else dict(evidence) if key_name == "client_evidence"
                else dict(receipt[key_name]) if key_name == "cleanup"
                else receipt[key_name]
            )
            for key_name in receipt
        }
    except ReadinessError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise _invalid_receipt() from None


def _stop_process(process: subprocess.Popen, *, force: bool = False) -> None:
    """Reap a child, escalating from TERM to KILL only after the grace period."""
    try:
        if process.poll() is not None:
            return
    except Exception:
        return
    if force:
        try:
            process.send_signal(signal.SIGTERM)
        except Exception:
            pass
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # Do not obscure the fixed UNKNOWN result with an OS exception.
        pass
    except Exception:
        pass


def _unknown(code: str, exit_code: int | None = None) -> dict:
    return {
        "outcome": "UNKNOWN",
        "exit_code": exit_code,
        "receipt": None,
        "error_code": code,
    }


def _read_child_output(process: subprocess.Popen) -> tuple[bytes, str | None]:
    """Read no more than the stdout cap while observing the fixed timeout."""
    if process.stdout is None:
        return b"", "PROBE_LAUNCH_FAILED"
    output = bytearray()
    selector = selectors.DefaultSelector()
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    selector.register(fd, selectors.EVENT_READ)
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    eof = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return bytes(output), "PROBE_TIMEOUT"
            events = selector.select(min(remaining, 0.1))
            if events:
                try:
                    chunk = os.read(fd, min(8192, MAX_STDOUT_BYTES + 1 - len(output)))
                except BlockingIOError:
                    chunk = None
                if chunk == b"":
                    eof = True
                    selector.unregister(fd)
                elif chunk:
                    output.extend(chunk)
                    if len(output) > MAX_STDOUT_BYTES:
                        return bytes(output[:MAX_STDOUT_BYTES]), "PROBE_OUTPUT_TOO_LARGE"
            if process.poll() is not None and eof:
                return bytes(output), None
    finally:
        selector.close()


def _parse_receipt(data: bytes) -> dict:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise _invalid_receipt() from None
    if not isinstance(value, dict):
        raise _invalid_receipt()
    return value


def launch_probe() -> dict:
    """Run the fixed script with a child-only opt-in and return a safe outcome."""
    raw_gate = os.environ.get(_GATE)
    if raw_gate not in (None, "false"):
        raise ReadinessError("PARENT_GATE_NOT_CLOSED")

    child_env = os.environ.copy()
    child_env[_GATE] = "true"
    argv = [sys.executable, str(PROBE_PATH)]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
            shell=False,
            close_fds=True,
        )
    except Exception:
        return _unknown("PROBE_LAUNCH_FAILED")

    try:
        output, read_error = _read_child_output(process)
        if read_error is not None:
            _stop_process(process, force=True)
            return _unknown(read_error, process.poll())
        exit_code = process.wait()
        parsed = None
        try:
            parsed = _parse_receipt(output)
            safe_receipt = validate_receipt(parsed, exit_code)
        except ReadinessError as exc:
            code = (
                "PROBE_EXIT_MISMATCH"
                if isinstance(parsed, dict)
                and parsed.get("final_status") in {"PASS", "FAILED", "BLOCKED"}
                and exit_code != {
                    "PASS": 0, "FAILED": 1, "BLOCKED": 2,
                }.get(parsed.get("final_status"))
                else "PROBE_RECEIPT_INVALID"
            )
            return _unknown(code, exit_code)
        outcome = "PASS" if safe_receipt["final_status"] == "PASS" else "FAILED"
        return {"outcome": outcome, "exit_code": exit_code, "receipt": safe_receipt}
    except Exception:
        _stop_process(process, force=True)
        return _unknown("PROBE_EXECUTION_FAILED", process.poll())
    except BaseException:
        _stop_process(process, force=True)
        raise
    finally:
        if process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass