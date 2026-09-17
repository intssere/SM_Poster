from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.core.config import get_settings

EXPECTED_REPL_ID = "e1147f4b-beb6-45e2-9141-bf4ff1966ec4"
EXPECTED_DATABASE_SYSTEM_IDENTIFIER = "7683126265079819246"
EXPECTED_DATABASE_NAME = "neondb"
EXPECTED_RECONCILER_SHA256 = "b508614c2202624bb67741e2f4bfc856a3f062698a67c106b8ce6f66964377a7"
CONFIRMATION = "RECONCILE-PRODUCTION-ALEMBIC-0017-TO-0018-NO-DDL"
RECONCILER_PATH = Path(__file__).parents[2] / "scripts" / "reconcile_production_alembic_revision.py"
REQUIRED_EVIDENCE_KEYS = {
    "source",
    "repl_id",
    "database_scope",
    "checked_at",
    "pending_statements",
    "structural_data_loss",
    "potential_incompatibility",
    "warnings",
}


class ReconciliationControlRefused(RuntimeError):
    """Raised when any temporary production reconciliation guard fails."""


def _refuse(message: str) -> None:
    raise ReconciliationControlRefused(
        f"temporary production Alembic reconciliation refused: {message}"
    )


def _load_canonical_reconciler() -> Any:
    try:
        raw = RECONCILER_PATH.read_bytes()
    except OSError as exc:
        raise ReconciliationControlRefused(
            "temporary production Alembic reconciliation refused: canonical reconciler is unreadable"
        ) from exc

    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != EXPECTED_RECONCILER_SHA256:
        _refuse("canonical reconciler source identity differs")

    module_name = "canonical_production_alembic_reconciler_for_temporary_control"
    spec = importlib.util.spec_from_file_location(module_name, RECONCILER_PATH)
    if spec is None or spec.loader is None:
        _refuse("canonical reconciler could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _validate_runtime_and_secret(*, supplied_secret: str, confirmation: str) -> None:
    settings = get_settings()
    if os.getenv("REPLIT_DEPLOYMENT") != "1":
        _refuse("runtime is not an active Replit deployment")
    if settings.app_env.lower() not in {"production", "prod", "replit"}:
        _refuse("APP_ENV is not an allowed production value")
    if os.getenv("REPL_ID") != EXPECTED_REPL_ID:
        _refuse("Replit app id differs")

    configured_secret = settings.alembic_reconciliation_secret
    if len(configured_secret) < 32:
        _refuse("one-time authorization secret is not configured")
    if not supplied_secret or not hmac.compare_digest(supplied_secret, configured_secret):
        _refuse("one-time authorization secret differs")
    if confirmation != CONFIRMATION:
        _refuse("explicit confirmation phrase differs")


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReconciliationControlRefused(
            "temporary production Alembic reconciliation refused: evidence is not JSON serializable"
        ) from exc


def _validate_external_zero_diff_evidence(
    evidence: dict[str, Any],
    *,
    expected_sha256: str,
) -> bytes:
    if not isinstance(evidence, dict) or set(evidence) != REQUIRED_EVIDENCE_KEYS:
        _refuse("zero-diff evidence field set differs")

    raw = _canonical_json_bytes(evidence)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256.lower()):
        _refuse("zero-diff evidence SHA-256 does not match")

    if evidence["source"] != "replit_pending_schema_diff":
        _refuse("zero-diff evidence source differs")
    if evidence["repl_id"] != EXPECTED_REPL_ID:
        _refuse("zero-diff evidence Replit app id differs")
    if evidence["database_scope"] != "production":
        _refuse("zero-diff evidence database scope is not production")
    if type(evidence["pending_statements"]) is not int or evidence["pending_statements"] != 0:
        _refuse("zero-diff evidence reports pending statements")
    if evidence["structural_data_loss"] is not False:
        _refuse("zero-diff evidence reports structural data loss")
    if evidence["potential_incompatibility"] is not False:
        _refuse("zero-diff evidence reports potential incompatibility")
    if evidence["warnings"] != []:
        _refuse("zero-diff evidence contains warnings")

    return raw


def execute_temporary_reconciliation(
    *,
    supplied_secret: str,
    confirmation: str,
    evidence: dict[str, Any],
    evidence_sha256: str,
) -> dict[str, Any]:
    """Execute the one permitted 0017→0018 bookkeeping CAS after every guard passes."""

    _validate_runtime_and_secret(
        supplied_secret=supplied_secret,
        confirmation=confirmation,
    )
    _validate_external_zero_diff_evidence(
        evidence,
        expected_sha256=evidence_sha256,
    )
    reconciler = _load_canonical_reconciler()

    settings = get_settings()
    database_url = reconciler._sqlalchemy_database_url(settings.database_url)
    database_identity_sha256 = reconciler.database_identity_sha256(database_url)

    attestation = {
        **evidence,
        "database_identity_sha256": database_identity_sha256,
    }
    raw_attestation = _canonical_json_bytes(attestation)
    attestation_sha256 = hashlib.sha256(raw_attestation).hexdigest()

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="alembic-reconcile-attestation-", suffix=".json", delete=False) as handle:
            handle.write(raw_attestation)
            temporary_path = Path(handle.name)

        reconciler.load_replit_schema_diff_attestation(
            temporary_path,
            expected_sha256=attestation_sha256,
            expected_repl_id=EXPECTED_REPL_ID,
            expected_database_identity_sha256=database_identity_sha256,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    engine = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            system_identifier, database_name = connection.execute(
                sa.text(
                    "SELECT (pg_control_system()).system_identifier::text, current_database()"
                )
            ).one()
            if str(system_identifier) != EXPECTED_DATABASE_SYSTEM_IDENTIFIER:
                _refuse("PostgreSQL cluster identity differs from live production")
            if str(database_name) != EXPECTED_DATABASE_NAME:
                _refuse("PostgreSQL database name differs from live production")

            rows = connection.execute(
                sa.text("SELECT version_num FROM alembic_version FOR UPDATE")
            ).fetchall()
            if len(rows) != 1 or str(rows[0][0]) != "0017":
                _refuse("alembic_version is not exactly one row at 0017")

            result = reconciler.reconcile_revision(connection)
            if not (
                result.mutated is True
                and result.status == "reconciled"
                and result.revision_before == "0017"
                and result.revision_after == "0018"
            ):
                _refuse("canonical reconciler did not report the exact permitted mutation")
    finally:
        engine.dispose()

    return {
        "status": "reconciled",
        "revision_before": "0017",
        "revision_after": "0018",
        "mutated": True,
        "control": "temporary-production-alembic-reconciliation",
    }
