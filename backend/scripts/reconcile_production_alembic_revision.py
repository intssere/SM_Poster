"""One-time fail-closed production Alembic revision bookkeeping reconciler.

This command is intentionally narrower than ``alembic stamp``. It never runs
migration upgrade/downgrade logic and never emits schema DDL. Its only allowed
mutation is changing the single ``alembic_version.version_num`` row from 0017
to 0018 after all guards prove that the database already has the exact
canonical final 0018 routine-publishing schema.

A fresh Replit pending-schema-diff attestation is mandatory because that state
is provided by the Replit control plane, not by the application database. The
attestation is bound to the Replit app and a credential-free database identity
hash and is content-addressed with a caller-supplied SHA-256 digest.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.session import sqlalchemy_database_url

SOURCE = "replit_pending_schema_diff"
CURRENT_REVISION = "0017"
TARGET_REVISION = "0018"
ATTESTATION_MAX_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
CONFIRMATION = "RECONCILE-PRODUCTION-ALEMBIC-0017-TO-0018-NO-DDL"

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0018_routine_pinterest_publishing_foundation.py"
)

REQUIRED_ATTESTATION_KEYS = {
    "source",
    "repl_id",
    "database_scope",
    "database_identity_sha256",
    "checked_at",
    "pending_statements",
    "structural_data_loss",
    "potential_incompatibility",
    "warnings",
}


class ReconciliationRefused(RuntimeError):
    """Raised whenever a fail-closed guard blocks bookkeeping reconciliation."""


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    revision_before: str
    revision_after: str
    mutated: bool


def _refuse(message: str) -> None:
    raise ReconciliationRefused(f"production Alembic reconciliation refused: {message}")


def _load_migration_0018() -> Any:
    spec = importlib.util.spec_from_file_location(
        "routine_pinterest_migration_0018_for_reconciliation", MIGRATION_PATH
    )
    if spec is None or spec.loader is None:
        _refuse("canonical migration 0018 could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def database_identity_sha256(database_url: str) -> str:
    """Return a credential-free stable identity hash for the target database."""

    url = make_url(sqlalchemy_database_url(database_url))
    backend = url.get_backend_name()
    host = (url.host or "").lower()
    port = url.port or 5432
    database = (url.database or "").strip()
    if backend != "postgresql" or not host or not database:
        _refuse("target database identity is not a concrete PostgreSQL database")
    canonical = f"postgresql://{host}:{port}/{database}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_checked_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _refuse("attestation checked_at is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReconciliationRefused(
            "production Alembic reconciliation refused: attestation checked_at is invalid"
        ) from exc
    if parsed.tzinfo is None:
        _refuse("attestation checked_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def load_replit_schema_diff_attestation(
    path: Path,
    *,
    expected_sha256: str,
    expected_repl_id: str,
    expected_database_identity_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load and strictly validate fresh zero-diff Replit control-plane evidence."""

    if not expected_sha256 or len(expected_sha256) != 64:
        _refuse("attestation SHA-256 must be supplied")
    if not expected_repl_id:
        _refuse("expected Replit app id must be supplied")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReconciliationRefused(
            "production Alembic reconciliation refused: attestation file is unreadable"
        ) from exc

    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256.lower() != expected_sha256.lower():
        _refuse("attestation SHA-256 does not match")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationRefused(
            "production Alembic reconciliation refused: attestation is not valid UTF-8 JSON"
        ) from exc

    if not isinstance(payload, dict) or set(payload) != REQUIRED_ATTESTATION_KEYS:
        _refuse("attestation field set differs from the required contract")
    if payload["source"] != SOURCE:
        _refuse("attestation source differs")
    if payload["repl_id"] != expected_repl_id:
        _refuse("attestation Replit app id differs")
    if payload["database_scope"] != "production":
        _refuse("attestation database scope is not production")
    if payload["database_identity_sha256"] != expected_database_identity_sha256:
        _refuse("attestation database identity differs")

    checked_at = _parse_checked_at(payload["checked_at"])
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (observed_now - checked_at).total_seconds()
    if age < -MAX_FUTURE_SKEW_SECONDS:
        _refuse("attestation timestamp is too far in the future")
    if age > ATTESTATION_MAX_AGE_SECONDS:
        _refuse("attestation is stale")

    if type(payload["pending_statements"]) is not int or payload["pending_statements"] != 0:
        _refuse("Replit pending schema diff is not empty")
    if payload["structural_data_loss"] is not False:
        _refuse("Replit schema diff reports structural data loss")
    if payload["potential_incompatibility"] is not False:
        _refuse("Replit schema diff reports potential incompatibility")
    if payload["warnings"] != []:
        _refuse("Replit schema diff contains warnings")

    return payload


def _validate_final_schema(connection: Any) -> None:
    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("target connection is not PostgreSQL")
    migration = _load_migration_0018()
    try:
        migration._validate_postgresql_contract(connection, "final")
    except Exception as exc:
        raise ReconciliationRefused(
            "production Alembic reconciliation refused: canonical final 0018 schema validation failed"
        ) from exc


def _locked_revision(connection: Any) -> str:
    rows = connection.execute(
        sa.text("SELECT version_num FROM alembic_version FOR UPDATE")
    ).fetchall()
    if len(rows) != 1:
        _refuse("alembic_version must contain exactly one row")
    value = str(rows[0][0])
    if value not in {CURRENT_REVISION, TARGET_REVISION}:
        _refuse(f"unexpected Alembic revision {value}")
    return value


def reconcile_revision(connection: Any) -> ReconciliationResult:
    """Validate exact final schema and reconcile only Alembic bookkeeping.

    The caller must already have validated the fresh Replit zero-diff
    attestation. The database transaction is expected to be controlled by the
    caller (``engine.begin()`` in the CLI). The only mutation emitted here is a
    guarded UPDATE against ``alembic_version``.
    """

    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("target connection is not PostgreSQL")

    revision_before = _locked_revision(connection)
    _validate_final_schema(connection)

    if revision_before == TARGET_REVISION:
        return ReconciliationResult(
            status="already_0018",
            revision_before=TARGET_REVISION,
            revision_after=TARGET_REVISION,
            mutated=False,
        )

    result = connection.execute(
        sa.text(
            "UPDATE alembic_version "
            "SET version_num = :target "
            "WHERE version_num = :current"
        ),
        {"target": TARGET_REVISION, "current": CURRENT_REVISION},
    )
    if result.rowcount != 1:
        _refuse("Alembic revision compare-and-set did not update exactly one row")

    rows = connection.execute(
        sa.text("SELECT version_num FROM alembic_version")
    ).fetchall()
    if len(rows) != 1 or str(rows[0][0]) != TARGET_REVISION:
        _refuse("Alembic revision did not verify as 0018 after bookkeeping update")

    return ReconciliationResult(
        status="reconciled",
        revision_before=CURRENT_REVISION,
        revision_after=TARGET_REVISION,
        mutated=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed one-time production Alembic 0017→0018 bookkeeping reconciler"
    )
    parser.add_argument("--attestation-file", type=Path, required=True)
    parser.add_argument("--attestation-sha256", required=True)
    parser.add_argument("--expected-repl-id", required=True)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.confirm != CONFIRMATION:
        _refuse("explicit production reconciliation confirmation phrase differs")

    settings = get_settings()
    if settings.app_env.lower() not in {"production", "prod", "replit"}:
        _refuse("APP_ENV is not an allowed production environment value")

    database_url = sqlalchemy_database_url(settings.database_url)
    identity = database_identity_sha256(database_url)
    load_replit_schema_diff_attestation(
        args.attestation_file,
        expected_sha256=args.attestation_sha256,
        expected_repl_id=args.expected_repl_id,
        expected_database_identity_sha256=identity,
    )

    engine = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            result = reconcile_revision(connection)
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "status": result.status,
                "revision_before": result.revision_before,
                "revision_after": result.revision_after,
                "mutated": result.mutated,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
