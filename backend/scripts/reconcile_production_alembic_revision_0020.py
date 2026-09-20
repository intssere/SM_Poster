"""One-time fail-closed production Alembic 0019->0020 bookkeeping reconciler.

This command never runs Alembic upgrade/downgrade/stamp and never emits schema
DDL. Its only allowed database mutation is a compare-and-set update of the
single alembic_version row from 0019 to 0020 after proving that production
already has the exact Task #50 structural schema and both Task #50 tables are
empty.

A fresh Replit pending-schema-diff attestation is mandatory because the control
plane's pending-diff state is external to the application database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.core.config import get_settings

SOURCE = "replit_pending_schema_diff"
CURRENT_REVISION = "0019"
TARGET_REVISION = "0020"
ATTESTATION_MAX_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
CONFIRMATION = "RECONCILE-PRODUCTION-ALEMBIC-0019-TO-0020-NO-DDL"

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

EXPECTED_TABLES = {
    "pinterest_portfolio_plans": {
        "columns": {
            "id": ("character varying(36)", False),
            "store_id": ("character varying(36)", False),
            "month_start": ("date", False),
            "month_end": ("date", False),
            "target_pins": ("integer", False),
            "existing_commitments": ("integer", False),
            "planned_active_slots": ("integer", False),
            "reserve_slots": ("integer", False),
            "policy_version": ("character varying(80)", False),
            "input_fingerprint": ("character varying(64)", False),
            "plan_fingerprint": ("character varying(64)", False),
            "status": ("character varying(20)", False),
            "metadata_json": ("json", False),
            "created_at": ("timestamp with time zone", False),
            "updated_at": ("timestamp with time zone", False),
        },
        "defaults": {
            "existing_commitments": "0",
            "planned_active_slots": "0",
            "reserve_slots": "0",
            "status": "'DRAFT'::character varying",
            "created_at": "now()",
            "updated_at": "now()",
        },
        "pk": ("id",),
        "fks": {
            ("store_id",): ("stores", ("id",), "CASCADE"),
        },
        "uniques": {
            "uq_pinterest_portfolio_plan_fingerprint": ("plan_fingerprint",),
        },
        "checks": {
            "ck_pinterest_portfolio_plan_status":
                "CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'ACTIVE'::character varying, 'COMPLETED'::character varying, 'CANCELLED'::character varying])::text[])))",
        },
        "indexes": {
            "ix_pinterest_portfolio_plans_input_fp": (False, ("input_fingerprint",), None),
            "ix_pinterest_portfolio_plans_month_start": (False, ("month_start",), None),
            "ix_pinterest_portfolio_plans_status": (False, ("status",), None),
            "ix_pinterest_portfolio_plans_store_id": (False, ("store_id",), None),
            "uq_pinterest_portfolio_active_month": (
                True,
                ("store_id", "month_start"),
                "((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'ACTIVE'::character varying])::text[]))",
            ),
        },
    },
    "pinterest_portfolio_plan_items": {
        "columns": {
            "id": ("character varying(36)", False),
            "plan_id": ("character varying(36)", False),
            "slot_index": ("integer", False),
            "is_reserve": ("boolean", False),
            "planned_date": ("date", True),
            "product_id": ("character varying(36)", False),
            "local_board_id": ("character varying(36)", False),
            "board_key_snapshot": ("character varying(255)", False),
            "content_angle_id": ("character varying(36)", False),
            "angle_key_snapshot": ("character varying(100)", False),
            "seed_keywords": ("json", False),
            "selection_score": ("numeric(12,6)", False),
            "selection_metadata": ("json", False),
            "item_fingerprint": ("character varying(64)", False),
            "status": ("character varying(20)", False),
            "publication_id": ("character varying(36)", True),
            "created_at": ("timestamp with time zone", False),
            "updated_at": ("timestamp with time zone", False),
        },
        "defaults": {
            "is_reserve": "false",
            "status": "'PLANNED'::character varying",
            "created_at": "now()",
            "updated_at": "now()",
        },
        "pk": ("id",),
        "fks": {
            ("content_angle_id",): ("content_angles", ("id",), "RESTRICT"),
            ("local_board_id",): ("boards", ("id",), "RESTRICT"),
            ("plan_id",): ("pinterest_portfolio_plans", ("id",), "CASCADE"),
            ("product_id",): ("products", ("id",), "RESTRICT"),
            ("publication_id",): ("pin_publications", ("id",), "SET NULL"),
        },
        "uniques": {
            "uq_pinterest_portfolio_item_fingerprint": ("item_fingerprint",),
            "uq_pinterest_portfolio_plan_item_slot": ("plan_id", "slot_index"),
        },
        "checks": {
            "ck_pinterest_portfolio_plan_item_status":
                "CHECK (((status)::text = ANY ((ARRAY['PLANNED'::character varying, 'PROMOTED'::character varying, 'GENERATED'::character varying, 'SCHEDULED'::character varying, 'PUBLISHED'::character varying, 'FAILED'::character varying, 'SKIPPED'::character varying])::text[])))",
        },
        "indexes": {
            "ix_pinterest_portfolio_items_angle_id": (False, ("content_angle_id",), None),
            "ix_pinterest_portfolio_items_board_id": (False, ("local_board_id",), None),
            "ix_pinterest_portfolio_items_plan_id": (False, ("plan_id",), None),
            "ix_pinterest_portfolio_items_planned_date": (False, ("planned_date",), None),
            "ix_pinterest_portfolio_items_product_id": (False, ("product_id",), None),
            "ix_pinterest_portfolio_items_publication_id": (False, ("publication_id",), None),
            "ix_pinterest_portfolio_items_status": (False, ("status",), None),
        },
    },
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
    raise ReconciliationRefused(
        f"production Alembic 0020 reconciliation refused: {message}"
    )


def _sqlalchemy_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def database_identity_sha256(database_url: str) -> str:
    url = make_url(_sqlalchemy_database_url(database_url))
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
            "production Alembic 0020 reconciliation refused: "
            "attestation checked_at is invalid"
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
    if not expected_sha256 or len(expected_sha256) != 64:
        _refuse("attestation SHA-256 must be supplied")
    if not expected_repl_id:
        _refuse("expected Replit app id must be supplied")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReconciliationRefused(
            "production Alembic 0020 reconciliation refused: "
            "attestation file is unreadable"
        ) from exc

    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256.lower() != expected_sha256.lower():
        _refuse("attestation SHA-256 does not match")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationRefused(
            "production Alembic 0020 reconciliation refused: "
            "attestation is not valid UTF-8 JSON"
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


def _normalize_sql(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_default(value: str | None) -> str | None:
    if value is None:
        return None
    text = _normalize_sql(value)
    # Replit/PostgreSQL may wrap scalar defaults in harmless casts/parentheses.
    if text in {"0", "0::integer", "(0)::integer"}:
        return "0"
    if text in {"false", "false::boolean", "(false)::boolean"}:
        return "false"
    return text


def _type_signature(row: Any) -> str:
    data_type = str(row["data_type"])
    if data_type == "character varying":
        return f"character varying({int(row['character_maximum_length'])})"
    if data_type == "numeric":
        return f"numeric({int(row['numeric_precision'])},{int(row['numeric_scale'])})"
    return data_type


def _column_contract(connection: Any, table: str) -> dict[str, tuple[str, bool, str | None]]:
    rows = connection.execute(
        sa.text(
            """
            SELECT column_name, data_type, character_maximum_length,
                   numeric_precision, numeric_scale, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:table
            ORDER BY ordinal_position
            """
        ),
        {"table": table},
    ).mappings().all()
    return {
        str(row["column_name"]): (
            _type_signature(row),
            str(row["is_nullable"]) == "YES",
            _normalize_default(row["column_default"]),
        )
        for row in rows
    }


def _constraint_contract(connection: Any, table: str) -> dict[str, Any]:
    rows = connection.execute(
        sa.text(
            """
            SELECT c.conname, c.contype, pg_get_constraintdef(c.oid, true) AS definition
            FROM pg_constraint c
            JOIN pg_class t ON t.oid=c.conrelid
            JOIN pg_namespace n ON n.oid=t.relnamespace
            WHERE n.nspname='public' AND t.relname=:table
            ORDER BY c.conname
            """
        ),
        {"table": table},
    ).mappings().all()
    return {
        str(row["conname"]): (
            str(row["contype"]),
            _normalize_sql(row["definition"]),
        )
        for row in rows
    }


def _index_contract(connection: Any, table: str) -> dict[str, tuple[bool, tuple[str, ...], str | None]]:
    rows = connection.execute(
        sa.text(
            """
            SELECT idx.relname AS index_name,
                   i.indisunique AS is_unique,
                   pg_get_indexdef(i.indexrelid) AS indexdef,
                   pg_get_expr(i.indpred, i.indrelid) AS predicate
            FROM pg_index i
            JOIN pg_class tbl ON tbl.oid=i.indrelid
            JOIN pg_class idx ON idx.oid=i.indexrelid
            JOIN pg_namespace n ON n.oid=tbl.relnamespace
            WHERE n.nspname='public'
              AND tbl.relname=:table
              AND NOT i.indisprimary
            ORDER BY idx.relname
            """
        ),
        {"table": table},
    ).mappings().all()

    result = {}
    for row in rows:
        indexdef = str(row["indexdef"])
        match = re.search(r"\(([^)]*)\)(?: WHERE|$)", indexdef)
        if not match:
            _refuse(f"could not parse index definition for {row['index_name']}")
        columns = tuple(
            part.strip().strip('"')
            for part in match.group(1).split(",")
        )
        result[str(row["index_name"])] = (
            bool(row["is_unique"]),
            columns,
            _normalize_sql(row["predicate"]) or None,
        )
    return result


def _fk_signature(definition: str) -> tuple[tuple[str, ...], str, tuple[str, ...], str]:
    text = _normalize_sql(definition)
    match = re.fullmatch(
        r"FOREIGN KEY \(([^)]*)\) REFERENCES ([^ (]+)\(([^)]*)\)(?: ON DELETE (CASCADE|RESTRICT|SET NULL))?",
        text,
    )
    if not match:
        _refuse(f"could not parse foreign key definition: {text}")
    local = tuple(x.strip().strip('"') for x in match.group(1).split(","))
    target_table = match.group(2).strip('"')
    remote = tuple(x.strip().strip('"') for x in match.group(3).split(","))
    on_delete = match.group(4) or "NO ACTION"
    return local, target_table, remote, on_delete


def _validate_table(connection: Any, table: str, expected: dict[str, Any]) -> None:
    columns = _column_contract(connection, table)
    if set(columns) != set(expected["columns"]):
        _refuse(f"{table} column set differs from canonical 0020")

    for name, (expected_type, expected_nullable) in expected["columns"].items():
        actual_type, actual_nullable, actual_default = columns[name]
        if actual_type != expected_type or actual_nullable != expected_nullable:
            _refuse(f"{table}.{name} type/nullability differs from canonical 0020")
        expected_default = expected["defaults"].get(name)
        if _normalize_default(actual_default) != _normalize_default(expected_default):
            _refuse(f"{table}.{name} default differs from canonical 0020")

    constraints = _constraint_contract(connection, table)
    pk_defs = [definition for kind, definition in constraints.values() if kind == "p"]
    if len(pk_defs) != 1:
        _refuse(f"{table} must have exactly one primary key")
    pk_match = re.fullmatch(r"PRIMARY KEY \(([^)]*)\)", pk_defs[0])
    if not pk_match:
        _refuse(f"{table} primary key definition is invalid")
    pk_cols = tuple(x.strip().strip('"') for x in pk_match.group(1).split(","))
    if pk_cols != expected["pk"]:
        _refuse(f"{table} primary key differs from canonical 0020")

    actual_fks = {}
    for kind, definition in constraints.values():
        if kind != "f":
            continue
        local, target, remote, on_delete = _fk_signature(definition)
        actual_fks[local] = (target, remote, on_delete)
    if actual_fks != expected["fks"]:
        _refuse(f"{table} foreign keys differ from canonical 0020")

    actual_uniques = {}
    actual_checks = {}
    for name, (kind, definition) in constraints.items():
        if kind == "u":
            match = re.fullmatch(r"UNIQUE \(([^)]*)\)", definition)
            if not match:
                _refuse(f"{table} unique constraint {name} is invalid")
            actual_uniques[name] = tuple(
                x.strip().strip('"') for x in match.group(1).split(",")
            )
        elif kind == "c":
            actual_checks[name] = definition

    if actual_uniques != expected["uniques"]:
        _refuse(f"{table} unique constraints differ from canonical 0020")
    for name, expected_def in expected["checks"].items():
        if _normalize_sql(actual_checks.get(name)) != _normalize_sql(expected_def):
            _refuse(f"{table} check constraint {name} differs from canonical 0020")
    if set(actual_checks) != set(expected["checks"]):
        _refuse(f"{table} check constraint set differs from canonical 0020")

    indexes = _index_contract(connection, table)
    # Unique constraints create backing indexes. Ignore only those exact backing
    # index names; every ordinary/partial Task #50 index must otherwise match.
    for unique_name in expected["uniques"]:
        indexes.pop(unique_name, None)
    if indexes != expected["indexes"]:
        _refuse(f"{table} index contract differs from canonical 0020")


def _validate_final_schema(connection: Any) -> None:
    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("target connection is not PostgreSQL")
    for table, expected in EXPECTED_TABLES.items():
        _validate_table(connection, table, expected)


def _validate_empty_task50_tables(connection: Any) -> None:
    for table in EXPECTED_TABLES:
        count = connection.execute(
            sa.text(f'SELECT count(*) FROM "{table}"')
        ).scalar_one()
        if int(count) != 0:
            _refuse(f"{table} is not empty")


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
    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("target connection is not PostgreSQL")

    revision_before = _locked_revision(connection)
    _validate_final_schema(connection)
    _validate_empty_task50_tables(connection)

    if revision_before == TARGET_REVISION:
        return ReconciliationResult(
            status="already_0020",
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
        _refuse("Alembic revision did not verify as 0020 after bookkeeping update")

    return ReconciliationResult(
        status="reconciled",
        revision_before=CURRENT_REVISION,
        revision_after=TARGET_REVISION,
        mutated=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed production Alembic 0019->0020 bookkeeping reconciler"
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

    database_url = _sqlalchemy_database_url(settings.database_url)
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

    print(json.dumps({
        "status": result.status,
        "revision_before": result.revision_before,
        "revision_after": result.revision_after,
        "mutated": result.mutated,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
