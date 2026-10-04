"""Explicit direct-client capture only; no environment/settings/provider fallback."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re

from .bridge_json import pg_json, strict_json
from .capture_diagnostics import CaptureStage, safe_diagnostic
from .catalog import canonical
from .select_bridge import source_sql, wrap_source_result
from .transfer import safe_plan, write_bundle


CONNECTION_OPTIONS = (
    "-c default_transaction_read_only=on -c timezone=UTC "
    "-c standard_conforming_strings=on -c search_path=pg_catalog,public "
    "-c statement_timeout=180000 -c lock_timeout=10000 "
    "-c idle_in_transaction_session_timeout=240000"
)


def _write_capsule(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _cleanup(connection, cursor):
    diagnostics = []
    operations = []
    if connection is not None:
        operations.append(connection.rollback)
    if cursor is not None:
        operations.append(cursor.close)
    if connection is not None:
        operations.append(connection.close)
    for operation in operations:
        try:
            operation()
        except Exception as exc:
            diagnostics.append(safe_diagnostic(exc, CaptureStage.ROLLBACK_CLOSE))
    return diagnostics


def capture_source_result(*, load_dsn, capsule_file: Path, bundle_file: Path,
                          connect=None) -> dict:
    """Execute the unchanged SELECT once, then privately hash and wrap offline.

    load_dsn is explicit operator input, invoked only inside CONNECT. The runner
    never retries and never returns a connection value, row, capsule, or bundle.
    Injectable connect is for offline tests; the default is psycopg.connect.
    """
    stage = CaptureStage.CONNECT
    connection = cursor = None
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    result = {}
    try:
        if capsule_file.resolve() == bundle_file.resolve():
            raise ValueError()
        dsn = load_dsn()
        if type(dsn) is not str or not dsn.strip():
            raise ValueError()
        if connect is None:
            import psycopg
            connect = psycopg.connect
        connection = connect(dsn, autocommit=True, connect_timeout=10,
                             options=CONNECTION_OPTIONS)
        stage = CaptureStage.SESSION_SETUP
        cursor = connection.cursor()
        cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        stage = CaptureStage.EXECUTE_SINGLE_STATEMENT
        cursor.execute(source_sql(), prepare=False)
        stage = CaptureStage.FETCH_ONE_ROW
        row = cursor.fetchone()
        extra = cursor.fetchone()
        stage = CaptureStage.VALIDATE_SINGLE_ROW
        if (cursor.description is None or len(cursor.description) != 1
                or cursor.description[0].name != "capsule"
                or row is None or len(row) != 1 or extra is not None):
            raise ValueError()
        capsule = row[0]
        if isinstance(capsule, (bytes, bytearray, memoryview)):
            raw = bytes(capsule)
            capsule = strict_json(raw.decode("utf-8"))
        elif type(capsule) is str:
            raw = capsule.encode("utf-8")
            capsule = strict_json(capsule)
        elif type(capsule) is dict:
            raw = canonical(capsule).encode("utf-8")
        else:
            raise ValueError()
        if type(capsule) is not dict or set(capsule) != {"payload", "capsule_sha256"}:
            raise ValueError()
        stage = CaptureStage.ROLLBACK_CLOSE
        cleanup = _cleanup(connection, cursor)
        connection = cursor = None
        if cleanup:
            result = {"success": False, "diagnostic": cleanup[0]}
            if len(cleanup) > 1:
                result["cleanup_diagnostics"] = cleanup[1:]
        else:
            stage = CaptureStage.WRITE_CAPSULE_FILE
            _write_capsule(capsule_file, raw)
            stage = CaptureStage.HASH_CAPSULE
            sha = hashlib.sha256(pg_json(capsule["payload"]).encode("utf-8")).hexdigest()
            claimed_sha = capsule["capsule_sha256"]
            if (type(claimed_sha) is not str or not re.fullmatch("[0-9a-f]{64}", claimed_sha)
                    or claimed_sha != sha):
                raise ValueError()
            stage = CaptureStage.OFFLINE_WRAP
            bundle = wrap_source_result(capsule, expected_capsule_sha256=sha)
            write_bundle(bundle, bundle_file)
            result = {"success": True, **safe_plan(bundle), "source_capsule_sha256": sha}
    except Exception as exc:
        result = {"success": False, "diagnostic": safe_diagnostic(exc, stage)}
    finally:
        cleanup = _cleanup(connection, cursor)
        if cleanup:
            result["cleanup_diagnostics"] = cleanup
        logging.disable(previous_disable)
    return result