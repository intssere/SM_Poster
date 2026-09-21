"""Fail-closed adoption of Replit pre-applied migration tables.

This module is intentionally migration-only.  The fingerprints are frozen from
the canonical 0020-0027 PostgreSQL catalog and must not be derived from ORM
metadata at runtime.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import sqlalchemy as sa


OWNED_TABLES: dict[str, tuple[str, ...]] = {
    "0020": ("pinterest_portfolio_plans", "pinterest_portfolio_plan_items"),
    "0021": ("pinterest_seo_briefs",),
    "0022": ("pinterest_autonomous_generation_runs",),
    "0023": ("pinterest_analytics_snapshots", "pinterest_analytics_ingestion_runs"),
    "0024": ("pinterest_learning_snapshots",),
    "0025": ("pinterest_optimizer_applications",),
    "0026": ("pinterest_autonomous_execution_runs",),
    "0027": ("pinterest_autonomous_destination_runs",),
}

# SHA-256(json.dumps(catalog_contract, sort_keys=True)) for the canonical
# migration-created PostgreSQL tables.  Keeping hashes here prevents future
# ORM metadata changes from widening the adoption contract.
FROZEN_FINGERPRINTS = {
    "pinterest_portfolio_plans": "75051576e9e5504e96235270f95d66e082e897e309d05879c8f1b3d0ffd9427e",
    "pinterest_portfolio_plan_items": "240b3c0f6a14a591376b6cd0482a50f03584ff80738f35cdb249f197b701caed",
    "pinterest_seo_briefs": "3505080724f6bc4f1343863a46f3d6bef0ad14064338cde4ec2cb48f22b5c0bb",
    "pinterest_autonomous_generation_runs": "205044f1d8624a43a074314ee2dc4217c2f4452ebb1addea9f5b46725c10a55a",
    "pinterest_analytics_snapshots": "ab23689139d004faf32bf0c71cd730abd91ec33c3b44c5e6ae8f5c448102aec7",
    "pinterest_analytics_ingestion_runs": "249c78c57556b74a295d05015f98353fedd7f795d5025f939680b09723df0f49",
    "pinterest_learning_snapshots": "1867887b11234216ad1bddf85a213a8d8d8a2eb6ddb33f9a3bf84e86ded7d6a3",
    "pinterest_optimizer_applications": "ddd9179f9e304a7fb5a5791d0201955a5d9944c33127f6ae225e461dbbfd9f81",
    "pinterest_autonomous_execution_runs": "ca54c18e8a5be6c46c5420446e267dfe5fa987dd1900ffd025170bdf74dd1ff5",
    "pinterest_autonomous_destination_runs": "4a9acf5c2acd1cf24a6e937edb4cc8d429aaabd7731acb3673e29246305b4c44",
}

# Exact fingerprints observed on the empty production tables that were
# pre-applied by the ORM before Alembic bookkeeping reached revision 0020.
# These are the only non-canonical fingerprints eligible for repair.
LEGACY_0020_FINGERPRINTS = {
    "pinterest_portfolio_plans": "9a91ae0ff2d722c7e12c23e236698cfd71259308e1ef3a42e4782075754129bf",
    "pinterest_portfolio_plan_items": "3085a01cf1385f9ed5cab605f236c7eceba49fab8f7ae4d6eac5e63e63f61274",
}


class SchemaAdoptionRefused(RuntimeError):
    """Raised when a pre-applied table is not exactly canonical and empty."""


def _refuse(message: str) -> None:
    raise SchemaAdoptionRefused(f"Replit schema adoption refused: {message}")


def _normalize_sql(value: Any) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value).strip())


def _normalize_default(value: Any) -> str | None:
    """Ignore only PostgreSQL's harmless scalar-cast/parenthesis rendering."""
    text = _normalize_sql(value)
    if text is None:
        return None
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    text = re.sub(
        r"::(?:character varying|integer|bigint|numeric|boolean|date|timestamp with time zone)$",
        "",
        text,
    )
    return text


def _catalog_contract(connection: Any, table: str) -> dict[str, Any]:
    columns = connection.execute(sa.text(
        """SELECT column_name, data_type, udt_name, character_maximum_length,
        numeric_precision, numeric_scale, datetime_precision, is_nullable,
        column_default, collation_name, is_identity, identity_generation,
        is_generated, generation_expression
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:table
        ORDER BY ordinal_position"""
    ), {"table": table}).mappings().all()
    column_values = [{
        "name": str(row["column_name"]),
        "type": str(row["data_type"]),
        "udt": str(row["udt_name"]),
        "length": row["character_maximum_length"],
        "precision": row["numeric_precision"],
        "scale": row["numeric_scale"],
        "datetime_precision": row["datetime_precision"],
        "nullable": str(row["is_nullable"]) == "YES",
        "default": _normalize_default(row["column_default"]),
        "collation": row["collation_name"],
        "identity": str(row["is_identity"]) == "YES",
        "identity_generation": row["identity_generation"],
        "generated": row["is_generated"],
        "generation_expression": _normalize_sql(row["generation_expression"]),
    } for row in columns]
    constraints = connection.execute(sa.text(
        """SELECT c.conname, c.contype, pg_get_constraintdef(c.oid, true) AS definition,
        c.condeferrable, c.condeferred, c.convalidated
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='public' AND t.relname=:table ORDER BY c.conname"""
    ), {"table": table}).mappings().all()
    indexes = connection.execute(sa.text(
        """SELECT idx.relname AS index_name, i.indisunique AS is_unique,
        i.indisvalid, i.indisready, i.indnullsnotdistinct,
        i.indnkeyatts, i.indnatts, am.amname AS access_method,
        pg_get_indexdef(i.indexrelid) AS index_definition,
        pg_get_expr(i.indpred, i.indrelid) AS predicate
        FROM pg_index i JOIN pg_class tbl ON tbl.oid=i.indrelid
        JOIN pg_class idx ON idx.oid=i.indexrelid
        JOIN pg_am am ON am.oid=idx.relam
        JOIN pg_namespace n ON n.oid=tbl.relnamespace
        WHERE n.nspname='public' AND tbl.relname=:table
        ORDER BY idx.relname"""
    ), {"table": table}).mappings().all()
    return {
        "columns": column_values,
        "constraints": [
            {"name": str(row["conname"]), "kind": str(row["contype"]),
             "definition": _normalize_sql(row["definition"]),
             "deferrable": bool(row["condeferrable"]),
             "deferred": bool(row["condeferred"]),
             "validated": bool(row["convalidated"])}
            for row in constraints
        ],
        "indexes": [
            {"name": str(row["index_name"]), "unique": bool(row["is_unique"]),
             "valid": bool(row["indisvalid"]), "ready": bool(row["indisready"]),
             "nulls_not_distinct": bool(row["indnullsnotdistinct"]),
             "key_columns": int(row["indnkeyatts"]), "columns": int(row["indnatts"]),
             "access_method": str(row["access_method"]),
             "definition": _normalize_sql(row["index_definition"]),
             "predicate": _normalize_sql(row["predicate"])}
            for row in indexes
        ],
    }


def _fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _lock_owned_tables(connection: Any, owned: tuple[str, ...]) -> None:
    """Prevent concurrent DML/DDL until this Alembic transaction commits."""
    for table in sorted(owned):
        connection.execute(sa.text(
            f'LOCK TABLE "public"."{table}" IN ACCESS EXCLUSIVE MODE'
        ))



def repair_known_legacy_preapplied_revision(connection: Any, revision: str) -> bool:
    """Drop only the exact known empty legacy 0020 pair so Alembic can recreate it.

    PostgreSQL DDL is transactional, so any later failure in revision 0020
    restores the original legacy tables together with the Alembic transaction.
    Every state other than the exact production-observed pair fails closed.
    """
    if revision != "0020":
        return False
    if getattr(connection.dialect, "name", None) != "postgresql":
        return False

    owned = OWNED_TABLES[revision]
    present = [
        table for table in owned
        if connection.execute(
            sa.text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{table}"},
        ).scalar_one() is not None
    ]
    if not present:
        return False
    if len(present) != len(owned):
        _refuse(f"revision {revision} has partial table presence: {present}")

    _lock_owned_tables(connection, owned)
    locked_present = [
        table for table in owned
        if connection.execute(
            sa.text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{table}"},
        ).scalar_one() is not None
    ]
    if tuple(locked_present) != tuple(owned):
        _refuse(f"revision {revision} table presence changed while locking")

    actual_fingerprints = {
        table: _fingerprint(_catalog_contract(connection, table))
        for table in owned
    }
    canonical_fingerprints = {
        table: FROZEN_FINGERPRINTS[table]
        for table in owned
    }
    if actual_fingerprints == canonical_fingerprints:
        return False
    if actual_fingerprints != LEGACY_0020_FINGERPRINTS:
        _refuse("revision 0020 pre-applied schema is not the known legacy repair contract")

    for table in owned:
        count = connection.execute(
            sa.text(f'SELECT count(*) FROM "public"."{table}"')
        ).scalar_one()
        if int(count) != 0:
            _refuse(f"{table} is not empty")

    dependencies = connection.execute(sa.text(
        """SELECT child.relname AS child_table, c.conname AS constraint_name
        FROM pg_constraint c
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE c.contype = 'f'
          AND child_ns.nspname = 'public'
          AND parent_ns.nspname = 'public'
          AND parent.relname = ANY(:owned)
          AND NOT (child.relname = ANY(:owned))
        ORDER BY child.relname, c.conname"""
    ), {"owned": list(owned)}).mappings().all()
    if dependencies:
        names = [
            f"{row['child_table']}.{row['constraint_name']}"
            for row in dependencies
        ]
        _refuse(
            "revision 0020 legacy tables have unexpected external dependencies: "
            + ", ".join(names)
        )

    # Child first, then parent.  No CASCADE: an unexpected dependency must fail
    # instead of being silently removed.
    connection.execute(sa.text(
        'DROP TABLE "public"."pinterest_portfolio_plan_items"'
    ))
    connection.execute(sa.text(
        'DROP TABLE "public"."pinterest_portfolio_plans"'
    ))

    remaining = [
        table for table in owned
        if connection.execute(
            sa.text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{table}"},
        ).scalar_one() is not None
    ]
    if remaining:
        _refuse(f"revision {revision} legacy repair did not remove all owned tables")
    return True


def adopt_preapplied_revision(connection: Any, revision: str) -> bool:
    """Validate and adopt all tables owned by *revision*, returning whether adopted."""
    owned = OWNED_TABLES.get(revision)
    if owned is None:
        _refuse(f"unsupported migration revision {revision}")
    is_postgresql = getattr(connection.dialect, "name", None) == "postgresql"
    if is_postgresql:
        present = [
            table for table in owned
            if connection.execute(
                sa.text("SELECT to_regclass(:qualified)"),
                {"qualified": f"public.{table}"},
            ).scalar_one() is not None
        ]
    else:
        # SQLite and other backends retain the canonical fresh-create path,
        # but never silently accept a pre-applied schema.
        present = [
            table
            for table in owned
            if connection.dialect.has_table(connection, table)
        ]
        if present:
            _refuse("pre-applied adoption requires PostgreSQL")
    if not present:
        return False
    if len(present) != len(owned):
        _refuse(f"revision {revision} has partial table presence: {present}")
    _lock_owned_tables(connection, owned)
    locked_present = [
        table for table in owned
        if connection.execute(
            sa.text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{table}"},
        ).scalar_one() is not None
    ]
    if tuple(locked_present) != tuple(owned):
        _refuse(f"revision {revision} table presence changed while locking")
    for table in owned:
        actual = _catalog_contract(connection, table)
        expected = FROZEN_FINGERPRINTS[table]
        if _fingerprint(actual) != expected:
            _refuse(f"{table} differs from its frozen revision contract")
        count = connection.execute(
            sa.text(f'SELECT count(*) FROM "public"."{table}"')
        ).scalar_one()
        if int(count) != 0:
            _refuse(f"{table} is not empty")
    return True