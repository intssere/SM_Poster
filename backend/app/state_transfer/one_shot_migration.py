"""Explicit one-process migration. No settings, providers, startup, or retries."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import stat
import tempfile

import sqlalchemy as sa

from .bridge_json import strict_json
from .capture_diagnostics import CaptureStage, safe_diagnostic
from .capture_runner import capture_source_result, connection_options
from .catalog import Refused
from .select_bridge import wrap_source_result
from .transfer import certify_target, import_target, verify_bundle


EXECUTION_ACK = "IMPORT_CLOSED_STATE_ONCE"
ROOT = Path(__file__).resolve().parents[3]
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


def _require(condition):
    if not condition:
        raise Refused()


def _env(name):
    _require(type(name) is str and ENV_NAME.fullmatch(name) is not None)
    value = os.environ[name]
    _require(bool(value.strip()))
    return value


def _url(value):
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    url = sa.engine.make_url(value)
    _require(url.get_backend_name() == "postgresql")
    _require(bool(url.database and url.username and (url.host or url.query.get("host"))))
    # Never permit DSN query overrides of the source's enforced options.
    _require(not set(url.query) & {"options", "service", "servicefile", "dbname"})
    return url.set(drivername="postgresql+psycopg")


def _private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                 and info.st_uid == os.geteuid() and info.st_nlink == 1)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            return strict_json(stream.read())
    finally:
        if fd is not None:
            os.close(fd)


def _remove_private(directory):
    """Best-effort overwrite/fsync, always unlink, then remove the private dir.

    Physical erasure on SSD/journaled storage is not promised. Operators needing
    that guarantee must use nonpersistent memory-backed ephemeral storage.
    """
    first_error = None
    for name in ("capsule.json", "bundle.json"):
        path = directory / name
        fd = None
        try:
            if not path.exists() and not path.is_symlink():
                continue
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                     and info.st_nlink == 1)
            remaining = info.st_size
            block = b"\0" * 65536
            while remaining:
                written = os.write(fd, block[:min(remaining, len(block))])
                _require(written > 0)
                remaining -= written
            os.fsync(fd)
        except Exception as exc:
            first_error = first_error or exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception as exc:
                    first_error = first_error or exc
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:
                first_error = first_error or exc
    try:
        directory.rmdir()
    except Exception as exc:
        first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _diagnostic(exc, stage):
    # SQLAlchemy may wrap a driver error; never inspect its text or parameters.
    driver = exc.orig if isinstance(exc, sa.exc.DBAPIError) else exc
    result = safe_diagnostic(driver, CaptureStage.CONNECT)
    result["stage"] = stage
    return result


def run_migration(*, source_env, target_env, execute=False, execution_env=None,
                  statement_timeout_ms=480000, lock_timeout_ms=10000,
                  ephemeral_parent=None):
    """One capture, offline verification, read-only plan, atomic import, certify.

    Only explicitly named variables are read. A resolved Railway environment
    reference is just the value of target_env; there is no reference resolver,
    DATABASE_URL fallback, provider client, migration, or automatic retry.
    """
    stage = "EXECUTION_GATE"
    directory = engine = None
    import_committed = False
    result = {}
    previous_logging = logging.root.manager.disable
    previous_umask = os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        _require(type(execute) is bool)
        _require(execute or (
            execution_env is not None and _env(execution_env) == EXECUTION_ACK))
        stage = "CONFIGURATION"
        connection_options(statement_timeout_ms, lock_timeout_ms)
        _require(source_env != target_env)
        source_url, target_url = _url(_env(source_env)), _url(_env(target_env))
        _require(source_url != target_url)
        parent = Path(ephemeral_parent or tempfile.gettempdir()).resolve()
        _require(parent != ROOT and ROOT not in parent.parents)
        _require(not any((p / ".git").exists() for p in (parent, *parent.parents)))
        directory = Path(tempfile.mkdtemp(prefix="closed-state-once-", dir=parent))
        os.chmod(directory, 0o700)
        stage = "SOURCE_CAPTURE"
        # The existing single SELECT verifies 0031 identity, all reviewed table
        # counts, catalog, publications, authorization and runtime closure in the
        # same read-only snapshot before returning any capsule.
        capture = capture_source_result(
            load_dsn=lambda: source_url.set(drivername="postgresql").render_as_string(
                hide_password=False),
            capsule_file=directory / "capsule.json", bundle_file=directory / "bundle.json",
            statement_timeout_ms=statement_timeout_ms, lock_timeout_ms=lock_timeout_ms,
        )
        if capture.get("success") is not True or capture.get("cleanup_diagnostics"):
            result = {"success": False, "diagnostic": capture.get(
                "diagnostic", (capture.get("cleanup_diagnostics") or [
                    {"stage": "ROLLBACK/CLOSE", "exception_class": "Exception"}])[0])}
        else:
            stage = "VERIFY_DIGESTS"
            capsule = _private_json(directory / "capsule.json")
            verified = wrap_source_result(
                capsule, expected_capsule_sha256=capture["source_capsule_sha256"])
            bundle = _private_json(directory / "bundle.json")
            manifest = verify_bundle(bundle, capture["manifest_sha256"])
            _require(manifest["manifest_sha256"] == verified["manifest"]["manifest_sha256"])
            _require(manifest["source_capsule_sha256"] == capture["source_capsule_sha256"])
            stage = "TARGET_PREFLIGHT"
            engine = sa.create_engine(target_url, echo=False, hide_parameters=True)
            plan = import_target(engine, bundle, manifest["manifest_sha256"], plan=True)
            _require(plan.get("target_preflight") == "PASS" and plan.get("writes") == 0)
            stage = "TARGET_IMPORT"
            imported = import_target(engine, bundle, manifest["manifest_sha256"])
            import_committed = True
            _require(imported.get("database_certification") == "PASS")
            stage = "TARGET_CERTIFICATION"
            certified = certify_target(engine, bundle, manifest["manifest_sha256"])
            _require(certified.get("database_certification") == "PASS")
            counts = {name: {"source_count": info["source_count"],
                             "imported_count": info["exported_count"]}
                      for name, info in manifest["tables"].items()}
            result = {
                "success": True, "stage": "CERTIFIED", "identity_closed_state": "PASS",
                "source_revision": manifest["source_revision"],
                "target_revision": manifest["target_revision"],
                "capture_mode": manifest["snapshot"]["capture_mode"],
                "statement_count": manifest["snapshot"]["statement_count"],
                "read_only": manifest["snapshot"]["read_only"],
                "isolation": manifest["snapshot"]["isolation"],
                "capsule_sha256": capture["source_capsule_sha256"],
                "manifest_sha256": manifest["manifest_sha256"],
                "source_table_count": len(counts),
                "source_total": sum(c["source_count"] for c in counts.values()),
                "imported_total": sum(c["imported_count"] for c in counts.values()),
                "publication_counts": manifest["publication_status_counts"],
                "tables": counts, "target_preflight": "PASS",
                "atomic_certification": "PASS", "durable_certification": "PASS",
                "schema_canonicality": "PASS", "provider_calls": 0,
            }
    except BaseException as exc:
        result = {"success": False, "diagnostic": _diagnostic(exc, stage)}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exc:
                result = {"success": False, "diagnostic": _diagnostic(exc, "TARGET_CLOSE")}
        if directory is not None:
            try:
                _remove_private(directory)
                result["ephemeral_cleanup"] = "PASS"
            except Exception as exc:
                result = {"success": False, "diagnostic": _diagnostic(exc, "EPHEMERAL_CLEANUP"),
                          "ephemeral_cleanup": "FAIL"}
        result["import_committed"] = import_committed
        if not result.get("success") and stage == "TARGET_IMPORT":
            # A transport failure during COMMIT can have an uncertain outcome.
            result["import_outcome"] = "UNKNOWN_NO_AUTOMATIC_RETRY"
        os.umask(previous_umask)
        logging.disable(previous_logging)
    return result