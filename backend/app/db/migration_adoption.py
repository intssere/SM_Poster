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
    "0028": (),
    "0029": (),
    "0030": ("pinterest_autonomous_run_reconciliations",),
}

BUNDLE_TABLES = tuple(
    table
    for revision in ("0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027")
    for table in OWNED_TABLES[revision]
)

# Exact empty catalogs left in production by the failed Replit publish for
# Issue #117. This complete mapping is the only non-canonical 0020-0027 bundle
# eligible for reconciliation.
PREAPPLIED_BUNDLE_FINGERPRINTS = {
    "pinterest_portfolio_plans": "9a91ae0ff2d722c7e12c23e236698cfd71259308e1ef3a42e4782075754129bf",
    "pinterest_portfolio_plan_items": "3085a01cf1385f9ed5cab605f236c7eceba49fab8f7ae4d6eac5e63e63f61274",
    "pinterest_seo_briefs": "32f3db20631cc243c492fb9fa9770a3fd33948b5b6345cc7bafa92275e7ee9e1",
    "pinterest_autonomous_generation_runs": "a6c0ccdb9c5f8d73e06b0c554976d5923b01ad62582cd42aa335aab5e2557a57",
    "pinterest_analytics_snapshots": "f48ddbaa2a3aa476e738a922be5bd894ec51f30c61bf452148c0a4eb4e09b720",
    "pinterest_analytics_ingestion_runs": "7863c20acda5feca706a72a96f047f9683031346613bef257ca9a99cfe1ffe39",
    "pinterest_learning_snapshots": "985935d8d8424d69bf5003e6c0c0e55656eb8990b418ec0cd3c23e4219f6e231",
    "pinterest_optimizer_applications": "2605cb38c7f05c91f92931ab836592051277048fb711ce6cc8a03d8347fc7889",
    "pinterest_autonomous_execution_runs": "e12a88dcc472ff7411ac09bf0f6d8ee89f4710f0e2a4e7f31e9689018c717c3a",
    "pinterest_autonomous_destination_runs": "9acb16898b1f5db70bfade1dabe29d449d26a49a5c8752693f30ca3b292d523f",
}

# Child tables always precede their referenced parents. No statement may add
# CASCADE: an unrecognized dependency must abort and roll back the transaction.
PREAPPLIED_BUNDLE_DROP_ORDER = (
    "pinterest_autonomous_destination_runs",
    "pinterest_autonomous_execution_runs",
    "pinterest_optimizer_applications",
    "pinterest_learning_snapshots",
    "pinterest_analytics_ingestion_runs",
    "pinterest_analytics_snapshots",
    "pinterest_autonomous_generation_runs",
    "pinterest_seo_briefs",
    "pinterest_portfolio_plan_items",
    "pinterest_portfolio_plans",
)

_BUNDLE_RECONCILIATION_INFO_KEY = "issue_117_bundle_reconciliation"

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


# Exact empty six-table catalog Replit produced after the successful Task #58.2
# website publish.  This state is repairable only at Alembic 0027 and only as
# one indivisible bundle.
POST_PUBLISH_DRIFT_TABLES = (
    "pinterest_analytics_snapshots",
    "pinterest_analytics_ingestion_runs",
    "pinterest_learning_snapshots",
    "pinterest_optimizer_applications",
    "pinterest_autonomous_execution_runs",
    "pinterest_autonomous_destination_runs",
)

POST_PUBLISH_PRESERVED_TABLES = (
    "pinterest_portfolio_plans",
    "pinterest_portfolio_plan_items",
    "pinterest_seo_briefs",
    "pinterest_autonomous_generation_runs",
)

POST_PUBLISH_DRIFT_FINGERPRINTS = {
    "pinterest_analytics_snapshots": "54751d9124942ca617d34da53e997f45f2a5924efae2d92deadfca944b5ca25e",
    "pinterest_analytics_ingestion_runs": "4aaccd7247ba75c43f3319842b3a86a816b55cb205537cb7d1dfb1da2abba3c5",
    "pinterest_learning_snapshots": "985935d8d8424d69bf5003e6c0c0e55656eb8990b418ec0cd3c23e4219f6e231",
    "pinterest_optimizer_applications": "2605cb38c7f05c91f92931ab836592051277048fb711ce6cc8a03d8347fc7889",
    "pinterest_autonomous_execution_runs": "92143aab35020f091c1ae917d7c6aed7ebe7880302abffccda79d8c4f5416af6",
    "pinterest_autonomous_destination_runs": "fc7f6af3194be33a88ae130358250d75c08e0fd839606dcb1f86ef54fce7664b",
}

POST_PUBLISH_DRIFT_DROP_ORDER = (
    "pinterest_autonomous_destination_runs",
    "pinterest_autonomous_execution_runs",
    "pinterest_optimizer_applications",
    "pinterest_analytics_ingestion_runs",
    "pinterest_analytics_snapshots",
    "pinterest_learning_snapshots",
)


HEAD_EXECUTION_CHECK_RENDERING_TABLE = "pinterest_autonomous_execution_runs"
HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT = (
    "3fd9f7f1f65c518fdc1b36d6b2c8d4b0fdcecb23ecd28482038f4866012acd3a"
)
HEAD_EXECUTION_CHECK_NAMES = (
    "ck_pinterest_auto_exec_stage",
    "ck_pinterest_auto_exec_status",
)


# Exact empty Replit development database state observed at Alembic 0023.
# This is intentionally separate from the production Task #58.3 allowlist
# because the development execution-run catalog has a different fingerprint.
DEV_0023_DRIFT_TABLES = POST_PUBLISH_DRIFT_TABLES
DEV_0023_PRESERVED_TABLES = POST_PUBLISH_PRESERVED_TABLES
DEV_0023_DRIFT_FINGERPRINTS = {
    "pinterest_analytics_snapshots": "54751d9124942ca617d34da53e997f45f2a5924efae2d92deadfca944b5ca25e",
    "pinterest_analytics_ingestion_runs": "4aaccd7247ba75c43f3319842b3a86a816b55cb205537cb7d1dfb1da2abba3c5",
    "pinterest_learning_snapshots": "985935d8d8424d69bf5003e6c0c0e55656eb8990b418ec0cd3c23e4219f6e231",
    "pinterest_optimizer_applications": "2605cb38c7f05c91f92931ab836592051277048fb711ce6cc8a03d8347fc7889",
    "pinterest_autonomous_execution_runs": "26cb4cbfa1b2adb97f42a0ef8f8a0700c40937b06e07387ab57e9cffffc8d28d",
    "pinterest_autonomous_destination_runs": "fc7f6af3194be33a88ae130358250d75c08e0fd839606dcb1f86ef54fce7664b",
}
DEV_0023_DROP_ORDER = POST_PUBLISH_DRIFT_DROP_ORDER
_DEV_0023_RECONCILIATION_INFO_KEY = "task_58_5_dev_0023_reconciliation"


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


def _canonical_constraint_rows(connection: Any, table: str) -> list[Any]:
    """Normalize PG18's redundant NOT NULL rows, never weaker enforcement.

    Before PG18 nullability lived only in pg_attribute/information_schema.
    Frozen contracts already include every column's nullability. PG18 also
    records it as contype='n'; names of these new rows are not historical
    constraint identities. Validate their enforcement before omitting them.
    """
    rows = connection.execute(sa.text(
        """SELECT c.conname, c.contype, pg_get_constraintdef(c.oid, true) AS definition,
        c.condeferrable, c.condeferred, c.convalidated, c.conkey, c.connoinherit,
        COALESCE((to_jsonb(c)->>'conenforced')::boolean, true) AS enforced,
        ARRAY(SELECT a.attname FROM pg_attribute a
              WHERE a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
                AND a.attnotnull AND NOT a.attisdropped) AS not_null_columns
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='public' AND t.relname=:table ORDER BY c.conname"""
    ), {"table": table}).mappings().all()
    canonical = []
    for row in rows:
        if not row["enforced"]:
            _refuse(f"{table} has an unenforced constraint: {row['conname']}")
        if row["contype"] == "n":
            if (
                not row["convalidated"]
                or row["condeferrable"]
                or row["condeferred"]
                or row["connoinherit"]
                or len(row["conkey"] or ()) != 1
                or len(row["not_null_columns"]) != 1
            ):
                _refuse(f"{table} has a noncanonical NOT NULL constraint: {row['conname']}")
        else:
            canonical.append(row)
    return canonical


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
    constraints = _canonical_constraint_rows(connection, table)
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



def _present_postgresql_tables(
    connection: Any,
    tables: tuple[str, ...],
) -> list[str]:
    return [
        table for table in tables
        if connection.execute(
            sa.text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{table}"},
        ).scalar_one() is not None
    ]


def _table_fingerprints(
    connection: Any,
    tables: tuple[str, ...],
) -> dict[str, str]:
    return {
        table: _fingerprint(_catalog_contract(connection, table))
        for table in tables
    }


def _require_empty_tables(connection: Any, tables: tuple[str, ...]) -> None:
    for table in tables:
        count = connection.execute(
            sa.text(f'SELECT count(*) FROM "public"."{table}"')
        ).scalar_one()
        if int(count) != 0:
            _refuse(f"{table} is not empty")


def _external_bundle_dependencies(connection: Any) -> list[str]:
    foreign_keys = connection.execute(sa.text(
        """SELECT child_ns.nspname AS child_schema,
               child.relname AS child_table,
               c.conname AS constraint_name
        FROM pg_constraint c
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE c.contype = 'f'
          AND parent_ns.nspname = 'public'
          AND parent.relname = ANY(:owned)
          AND NOT (
              child_ns.nspname = 'public'
              AND child.relname = ANY(:owned)
          )
        ORDER BY child_ns.nspname, child.relname, c.conname"""
    ), {"owned": list(BUNDLE_TABLES)}).mappings().all()
    rewrites = connection.execute(sa.text(
        """SELECT DISTINCT dependent_ns.nspname AS dependent_schema,
               dependent.relname AS dependent_relation,
               rewrite.rulename AS dependent_rule
        FROM pg_depend dependency
        JOIN pg_rewrite rewrite ON rewrite.oid = dependency.objid
        JOIN pg_class dependent ON dependent.oid = rewrite.ev_class
        JOIN pg_namespace dependent_ns
          ON dependent_ns.oid = dependent.relnamespace
        JOIN pg_class parent ON parent.oid = dependency.refobjid
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE dependency.classid = 'pg_rewrite'::regclass
          AND parent_ns.nspname = 'public'
          AND parent.relname = ANY(:owned)
          AND NOT (
              dependent_ns.nspname = 'public'
              AND dependent.relname = ANY(:owned)
          )
        ORDER BY dependent_ns.nspname, dependent.relname, rewrite.rulename"""
    ), {"owned": list(BUNDLE_TABLES)}).mappings().all()
    inheritance = connection.execute(sa.text(
        """SELECT child_ns.nspname AS child_schema,
               child.relname AS child_table
        FROM pg_inherits inheritance
        JOIN pg_class child ON child.oid = inheritance.inhrelid
        JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_class parent ON parent.oid = inheritance.inhparent
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE parent_ns.nspname = 'public'
          AND parent.relname = ANY(:owned)
          AND NOT (
              child_ns.nspname = 'public'
              AND child.relname = ANY(:owned)
          )
        ORDER BY child_ns.nspname, child.relname"""
    ), {"owned": list(BUNDLE_TABLES)}).mappings().all()
    return [
        f"foreign key {row['child_schema']}.{row['child_table']}."
        f"{row['constraint_name']}"
        for row in foreign_keys
    ] + [
        f"rewrite {row['dependent_schema']}.{row['dependent_relation']}."
        f"{row['dependent_rule']}"
        for row in rewrites
    ] + [
        f"inheritance {row['child_schema']}.{row['child_table']}"
        for row in inheritance
    ]


def _require_bundle_bookkeeping_at_0019(connection: Any) -> None:
    revisions = connection.execute(sa.text(
        "SELECT version_num FROM alembic_version ORDER BY version_num"
    )).scalars().all()
    if list(revisions) != ["0019"]:
        _refuse(
            "pre-applied bundle reconciliation requires Alembic revision 0019"
        )


def reconcile_preapplied_bundle(connection: Any, revision: str) -> bool:
    """Remove only the exact empty Issue #117 bundle for canonical rebuilding."""
    if revision != "0020":
        return False

    is_postgresql = getattr(connection.dialect, "name", None) == "postgresql"
    if not is_postgresql:
        present = [
            table for table in BUNDLE_TABLES
            if connection.dialect.has_table(connection, table)
        ]
        if present:
            _refuse("pre-applied bundle reconciliation requires PostgreSQL")
        return False

    present = _present_postgresql_tables(connection, BUNDLE_TABLES)
    if not present:
        return False

    # The exact 0020 pair belongs to Task #58.1 and must retain its existing
    # repair/adoption behavior rather than being treated as a partial bundle.
    if tuple(present) == OWNED_TABLES["0020"]:
        return False
    if tuple(present) != BUNDLE_TABLES:
        _refuse(f"pre-applied bundle has partial table presence: {present}")

    _require_bundle_bookkeeping_at_0019(connection)
    _lock_owned_tables(connection, BUNDLE_TABLES)

    locked_present = _present_postgresql_tables(connection, BUNDLE_TABLES)
    if tuple(locked_present) != BUNDLE_TABLES:
        _refuse("pre-applied bundle table presence changed while locking")

    fingerprints = _table_fingerprints(connection, BUNDLE_TABLES)
    canonical = {
        table: FROZEN_FINGERPRINTS[table]
        for table in BUNDLE_TABLES
    }
    if fingerprints == canonical:
        return False
    if fingerprints != PREAPPLIED_BUNDLE_FINGERPRINTS:
        _refuse("pre-applied bundle is not the exact Issue #117 fingerprint set")

    _require_empty_tables(connection, BUNDLE_TABLES)

    dependencies = _external_bundle_dependencies(connection)
    if dependencies:
        _refuse(
            "pre-applied bundle has unexpected external dependencies: "
            + ", ".join(dependencies)
        )

    _drop_preapplied_bundle_tables(connection)

    remaining = _present_postgresql_tables(connection, BUNDLE_TABLES)
    if remaining:
        _refuse(f"pre-applied bundle teardown left tables present: {remaining}")

    connection.info[_BUNDLE_RECONCILIATION_INFO_KEY] = True
    return True


def _drop_preapplied_bundle_tables(connection: Any) -> None:
    for table in PREAPPLIED_BUNDLE_DROP_ORDER:
        connection.execute(sa.text(f'DROP TABLE "public"."{table}"'))


def verify_reconciled_bundle(connection: Any, revision: str) -> None:
    """Require a reconciled bundle to end revision 0027 exactly canonical."""
    if revision != "0027":
        return
    if not connection.info.get(_BUNDLE_RECONCILIATION_INFO_KEY):
        return
    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("reconciled bundle verification requires PostgreSQL")

    present = _present_postgresql_tables(connection, BUNDLE_TABLES)
    if tuple(present) != BUNDLE_TABLES:
        _refuse(f"reconciled bundle was not fully recreated: {present}")

    fingerprints = _table_fingerprints(connection, BUNDLE_TABLES)
    canonical = {
        table: FROZEN_FINGERPRINTS[table]
        for table in BUNDLE_TABLES
    }
    if fingerprints != canonical:
        _refuse("reconciled bundle did not produce all frozen canonical contracts")
    _require_empty_tables(connection, BUNDLE_TABLES)
    connection.info.pop(_BUNDLE_RECONCILIATION_INFO_KEY, None)


def _external_dependencies_for_tables(
    connection: Any,
    tables: tuple[str, ...],
) -> list[str]:
    foreign_keys = connection.execute(sa.text(
        """SELECT child_ns.nspname AS child_schema,
               child.relname AS child_table,
               c.conname AS constraint_name
        FROM pg_constraint c
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE c.contype = 'f'
          AND parent_ns.nspname = 'public'
          AND parent.relname = ANY(:owned)
          AND NOT (
              child_ns.nspname = 'public'
              AND child.relname = ANY(:owned)
          )
        ORDER BY child_ns.nspname, child.relname, c.conname"""
    ), {"owned": list(tables)}).mappings().all()
    rewrites = connection.execute(sa.text(
        """SELECT DISTINCT dependent_ns.nspname AS dependent_schema,
               dependent.relname AS dependent_relation,
               rewrite.rulename AS dependent_rule
        FROM pg_depend dependency
        JOIN pg_rewrite rewrite ON rewrite.oid = dependency.objid
        JOIN pg_class dependent ON dependent.oid = rewrite.ev_class
        JOIN pg_namespace dependent_ns ON dependent_ns.oid = dependent.relnamespace
        JOIN pg_class parent ON parent.oid = dependency.refobjid
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE dependency.classid = 'pg_rewrite'::regclass
          AND parent_ns.nspname = 'public'
          AND parent.relname = ANY(:owned)
          AND NOT (
              dependent_ns.nspname = 'public'
              AND dependent.relname = ANY(:owned)
          )
        ORDER BY dependent_ns.nspname, dependent.relname, rewrite.rulename"""
    ), {"owned": list(tables)}).mappings().all()
    inheritance = connection.execute(sa.text(
        """SELECT child_ns.nspname AS child_schema,
               child.relname AS child_table
        FROM pg_inherits inheritance
        JOIN pg_class child ON child.oid = inheritance.inhrelid
        JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_class parent ON parent.oid = inheritance.inhparent
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        WHERE parent_ns.nspname = 'public'
          AND parent.relname = ANY(:owned)
          AND NOT (
              child_ns.nspname = 'public'
              AND child.relname = ANY(:owned)
          )
        ORDER BY child_ns.nspname, child.relname"""
    ), {"owned": list(tables)}).mappings().all()
    return [
        f"foreign key {row['child_schema']}.{row['child_table']}."
        f"{row['constraint_name']}"
        for row in foreign_keys
    ] + [
        f"rewrite {row['dependent_schema']}.{row['dependent_relation']}."
        f"{row['dependent_rule']}"
        for row in rewrites
    ] + [
        f"inheritance {row['child_schema']}.{row['child_table']}"
        for row in inheritance
    ]


def _require_exact_alembic_revision(connection: Any, revision: str) -> None:
    revisions = connection.execute(sa.text(
        'SELECT version_num FROM "public"."alembic_version" ORDER BY version_num'
    )).scalars().all()
    if list(revisions) != [revision]:
        _refuse(
            f"schema reconciliation requires Alembic revision {revision}"
        )


def _require_canonical_tables(
    connection: Any,
    tables: tuple[str, ...],
) -> None:
    present = _present_postgresql_tables(connection, tables)
    if tuple(present) != tables:
        _refuse(f"canonical predecessor table presence mismatch: {present}")
    fingerprints = _table_fingerprints(connection, tables)
    expected = {table: FROZEN_FINGERPRINTS[table] for table in tables}
    if fingerprints != expected:
        _refuse("canonical predecessor contracts do not match frozen fingerprints")


def reconcile_development_drift_at_0024(
    connection: Any,
    revision: str,
) -> bool:
    """Remove only the exact empty development drift observed at Alembic 0023."""
    if revision != "0024":
        return False
    if getattr(connection.dialect, "name", None) != "postgresql":
        return False

    _require_exact_alembic_revision(connection, "0023")
    _require_canonical_tables(connection, DEV_0023_PRESERVED_TABLES)

    present = _present_postgresql_tables(connection, DEV_0023_DRIFT_TABLES)

    # Normal canonical 0023 has only its two analytics tables present.  Leave
    # that state to the ordinary 0024 migration path.
    analytics_only = OWNED_TABLES["0023"]
    if tuple(present) == analytics_only:
        analytics_fingerprints = _table_fingerprints(connection, analytics_only)
        expected = {
            table: FROZEN_FINGERPRINTS[table]
            for table in analytics_only
        }
        if analytics_fingerprints != expected:
            _refuse("revision 0023 analytics tables are non-canonical")
        return False

    if tuple(present) != DEV_0023_DRIFT_TABLES:
        _refuse(f"development 0023 drift has partial table presence: {present}")

    fingerprints = _table_fingerprints(connection, DEV_0023_DRIFT_TABLES)
    canonical = {
        table: FROZEN_FINGERPRINTS[table]
        for table in DEV_0023_DRIFT_TABLES
    }
    if fingerprints == canonical:
        # A fully canonical pre-applied future bundle can retain the existing
        # per-revision adoption behavior.
        return False
    if fingerprints != DEV_0023_DRIFT_FINGERPRINTS:
        _refuse("development 0023 drift is not the exact Task #58.5 fingerprint set")

    # Lock the complete owned bundle so predecessor and target contracts cannot
    # change between validation and teardown.
    _lock_owned_tables(connection, BUNDLE_TABLES)

    _require_exact_alembic_revision(connection, "0023")
    locked_present = _present_postgresql_tables(connection, BUNDLE_TABLES)
    if tuple(locked_present) != BUNDLE_TABLES:
        _refuse("development 0023 table presence changed while locking")

    _require_canonical_tables(connection, DEV_0023_PRESERVED_TABLES)
    _require_empty_tables(connection, DEV_0023_PRESERVED_TABLES)

    locked_fingerprints = _table_fingerprints(
        connection,
        DEV_0023_DRIFT_TABLES,
    )
    if locked_fingerprints != DEV_0023_DRIFT_FINGERPRINTS:
        _refuse("development 0023 drift fingerprints changed while locking")
    _require_empty_tables(connection, DEV_0023_DRIFT_TABLES)

    dependencies = _external_dependencies_for_tables(
        connection,
        DEV_0023_DRIFT_TABLES,
    )
    if dependencies:
        _refuse(
            "development 0023 drift has unexpected external dependencies: "
            + ", ".join(dependencies)
        )

    for table in DEV_0023_DROP_ORDER:
        connection.execute(sa.text(f'DROP TABLE "public"."{table}"'))

    remaining = _present_postgresql_tables(
        connection,
        DEV_0023_DRIFT_TABLES,
    )
    if remaining:
        _refuse(
            "development 0023 reconciliation left drift tables present: "
            + ", ".join(remaining)
        )

    connection.info[_DEV_0023_RECONCILIATION_INFO_KEY] = True
    return True


def verify_development_0023_analytics_rebuild(connection: Any) -> None:
    """Verify canonical recreation of the already-recorded revision 0023 tables."""
    if not connection.info.get(_DEV_0023_RECONCILIATION_INFO_KEY):
        return
    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("development 0023 reconciliation verification requires PostgreSQL")
    _require_canonical_tables(connection, OWNED_TABLES["0023"])
    _require_empty_tables(connection, OWNED_TABLES["0023"])
    _require_canonical_tables(connection, DEV_0023_PRESERVED_TABLES)
    _require_empty_tables(connection, DEV_0023_PRESERVED_TABLES)


def verify_development_reconciliation(connection: Any, revision: str) -> None:
    """Require a Task #58.5 reconciliation to finish 0027 fully canonical."""
    if revision != "0027":
        return
    if not connection.info.get(_DEV_0023_RECONCILIATION_INFO_KEY):
        return
    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("development reconciliation verification requires PostgreSQL")

    present = _present_postgresql_tables(connection, BUNDLE_TABLES)
    if tuple(present) != BUNDLE_TABLES:
        _refuse(f"development reconciliation was not fully recreated: {present}")
    fingerprints = _table_fingerprints(connection, BUNDLE_TABLES)
    expected = {
        table: FROZEN_FINGERPRINTS[table]
        for table in BUNDLE_TABLES
    }
    if fingerprints != expected:
        _refuse("development reconciliation did not produce frozen canonical contracts")
    _require_empty_tables(connection, BUNDLE_TABLES)
    connection.info.pop(_DEV_0023_RECONCILIATION_INFO_KEY, None)


def reconcile_post_publish_drift(connection: Any, revision: str) -> bool:
    """Validate the exact empty Task #58.3 drift and authorize its rebuild."""
    if revision != "0028":
        return False
    if getattr(connection.dialect, "name", None) != "postgresql":
        return False

    _require_exact_alembic_revision(connection, "0027")
    _require_canonical_tables(connection, POST_PUBLISH_PRESERVED_TABLES)

    present = _present_postgresql_tables(connection, POST_PUBLISH_DRIFT_TABLES)
    if tuple(present) != POST_PUBLISH_DRIFT_TABLES:
        _refuse(f"post-publish drift has partial table presence: {present}")

    fingerprints = _table_fingerprints(connection, POST_PUBLISH_DRIFT_TABLES)
    canonical = {
        table: FROZEN_FINGERPRINTS[table]
        for table in POST_PUBLISH_DRIFT_TABLES
    }
    if fingerprints == canonical:
        return False
    if fingerprints != POST_PUBLISH_DRIFT_FINGERPRINTS:
        _refuse("post-publish drift is not the exact Task #58.3 fingerprint set")

    _lock_owned_tables(connection, POST_PUBLISH_DRIFT_TABLES)

    locked_present = _present_postgresql_tables(
        connection,
        POST_PUBLISH_DRIFT_TABLES,
    )
    if tuple(locked_present) != POST_PUBLISH_DRIFT_TABLES:
        _refuse("post-publish drift table presence changed while locking")

    locked_fingerprints = _table_fingerprints(
        connection,
        POST_PUBLISH_DRIFT_TABLES,
    )
    if locked_fingerprints != POST_PUBLISH_DRIFT_FINGERPRINTS:
        _refuse("post-publish drift fingerprints changed while locking")

    _require_empty_tables(connection, POST_PUBLISH_DRIFT_TABLES)
    _require_canonical_tables(connection, POST_PUBLISH_PRESERVED_TABLES)
    _require_empty_tables(connection, POST_PUBLISH_PRESERVED_TABLES)

    dependencies = _external_dependencies_for_tables(
        connection,
        POST_PUBLISH_DRIFT_TABLES,
    )
    if dependencies:
        _refuse(
            "post-publish drift has unexpected external dependencies: "
            + ", ".join(dependencies)
        )
    return True


def verify_post_publish_repair(connection: Any) -> None:
    """Require the six repaired tables and preserved predecessors canonical."""
    if getattr(connection.dialect, "name", None) != "postgresql":
        return
    _require_canonical_tables(connection, POST_PUBLISH_PRESERVED_TABLES)
    _require_canonical_tables(connection, POST_PUBLISH_DRIFT_TABLES)
    _require_empty_tables(connection, POST_PUBLISH_PRESERVED_TABLES)
    _require_empty_tables(connection, POST_PUBLISH_DRIFT_TABLES)



def reconcile_head_execution_check_rendering(
    connection: Any,
    revision: str,
) -> bool:
    """Authorize only the exact empty 0028 check-rendering residual."""
    if revision != "0029":
        return False
    if getattr(connection.dialect, "name", None) != "postgresql":
        return False

    _require_exact_alembic_revision(connection, "0028")
    target = HEAD_EXECUTION_CHECK_RENDERING_TABLE
    preserved = tuple(table for table in BUNDLE_TABLES if table != target)
    _require_canonical_tables(connection, preserved)

    present = _present_postgresql_tables(connection, (target,))
    if tuple(present) != (target,):
        _refuse("head check-rendering target table is missing")

    fingerprint = _table_fingerprints(connection, (target,))[target]
    canonical = FROZEN_FINGERPRINTS[target]
    if fingerprint == canonical:
        return False
    if fingerprint != HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT:
        _refuse("head check-rendering drift is not the exact Task #58.6 fingerprint")

    _lock_owned_tables(connection, (target,))
    _require_exact_alembic_revision(connection, "0028")

    locked_present = _present_postgresql_tables(connection, (target,))
    if tuple(locked_present) != (target,):
        _refuse("head check-rendering target presence changed while locking")
    locked_fingerprint = _table_fingerprints(connection, (target,))[target]
    if locked_fingerprint != HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT:
        _refuse("head check-rendering fingerprint changed while locking")
    _require_empty_tables(connection, (target,))
    _require_canonical_tables(connection, preserved)
    return True


def verify_head_execution_check_repair(connection: Any) -> None:
    """Require Task #58.6 to finish at the pre-existing frozen contract."""
    if getattr(connection.dialect, "name", None) != "postgresql":
        return
    target = HEAD_EXECUTION_CHECK_RENDERING_TABLE
    _require_canonical_tables(connection, (target,))

LINEAGE_RUN_TABLES = (
    "pinterest_autonomous_destination_runs",
    "pinterest_autonomous_execution_runs",
    "pinterest_autonomous_generation_runs",
)
LINEAGE_RECONCILIATION_TABLE = "pinterest_autonomous_run_reconciliations"
LINEAGE_PRESERVED_TABLES = tuple(
    table for table in BUNDLE_TABLES if table not in LINEAGE_RUN_TABLES
)


# Frozen from the migration-created 0031 PostgreSQL public catalog. These
# tables changed in 0030, so their 0020-0027 fingerprints are not applicable.
FROZEN_0030_LINEAGE_FINGERPRINTS = {
    "pinterest_autonomous_destination_runs": "fbb5d6cf6742ae320f20726e3fcb7f53e30b2c808737665bdd10ab7b42de1f09",
    "pinterest_autonomous_execution_runs": "c5681bd4e0ccaea3deb40d1d227e9afd40282d9a80b807dd323dd76895b1cfb9",
    "pinterest_autonomous_generation_runs": "cee9e0728872f986b24bb48dc2d3e0a1d4eaf3c95947a646cf144cd36ad6110d",
    "pinterest_autonomous_run_reconciliations": "a62c06abc7eb2332cbbdecd779a1c5c659db0ea59c40d0ca60eba075eda431d4",
}


def _require_frozen_0030_lineage_catalog(connection: Any) -> None:
    for table, expected in FROZEN_0030_LINEAGE_FINGERPRINTS.items():
        if _fingerprint(_catalog_contract(connection, table)) != expected:
            _refuse(f"0030 frozen lineage contract mismatch: {table}")


def _require_0030_lineage_schema(connection: Any) -> None:
    inspector = sa.inspect(connection)
    present = set(inspector.get_table_names(schema="public"))
    required = set(LINEAGE_RUN_TABLES) | {LINEAGE_RECONCILIATION_TABLE}
    missing = sorted(required - present)
    if missing:
        _refuse("0030 lineage schema tables missing: " + ", ".join(missing))

    run_contracts = {
        "pinterest_autonomous_destination_runs": {
            "attempt_uq": "uq_pinterest_auto_destination_attempt",
            "supersedes_fk": "fk_pinterest_auto_destination_supersedes",
            "old_uqs": {
                "uq_pinterest_auto_destination_item",
                "uq_pinterest_auto_destination_fingerprint",
            },
            "expected_indexes": {
                "ix_pinterest_auto_destination_input_fingerprint",
                "ix_pinterest_auto_destination_plan_stage",
                "ix_pinterest_auto_destination_plan_id",
                "ix_pinterest_auto_destination_status",
                "ix_pinterest_auto_destination_stage",
                "ix_pinterest_auto_destination_status_stage",
                "ix_pinterest_auto_destination_provisioning_attempt",
                "ix_pinterest_auto_destination_board_record",
                "ix_pinterest_auto_destination_execution_run",
                "ix_pinterest_autonomous_destination_runs_supersedes_run_id",
            },
        },
        "pinterest_autonomous_execution_runs": {
            "attempt_uq": "uq_pinterest_auto_exec_attempt",
            "supersedes_fk": "fk_pinterest_auto_exec_supersedes",
            "old_uqs": {
                "uq_pinterest_auto_exec_portfolio_item",
                "uq_pinterest_auto_exec_input_fingerprint",
            },
            "expected_indexes": {
                "ix_pinterest_auto_exec_input_fingerprint",
                "ix_pinterest_auto_exec_plan_id",
                "ix_pinterest_auto_exec_optimizer_app",
                "ix_pinterest_auto_exec_status",
                "ix_pinterest_auto_exec_stage",
                "ix_pinterest_auto_exec_scheduled_for",
                "ix_pinterest_auto_exec_plan_stage",
                "ix_pinterest_auto_exec_seo_brief",
                "ix_pinterest_auto_exec_generation",
                "ix_pinterest_auto_exec_approval",
                "ix_pinterest_auto_exec_publication",
                "ix_pinterest_auto_exec_permit",
                "ix_pinterest_autonomous_execution_runs_supersedes_run_id",
            },
        },
        "pinterest_autonomous_generation_runs": {
            "attempt_uq": "uq_pinterest_autonomous_generation_attempt",
            "supersedes_fk": "fk_pinterest_autonomous_generation_supersedes",
            "old_uqs": {
                "uq_pinterest_autonomous_generation_item",
                "uq_pinterest_autonomous_generation_input_fp",
            },
            "expected_indexes": {
                "ix_pinterest_autonomous_generation_input_fingerprint",
                "ix_pinterest_generation_runs_portfolio_item",
                "ix_pinterest_generation_runs_seo_brief",
                "ix_pinterest_generation_runs_status",
                "ix_pinterest_generation_runs_concept",
                "ix_pinterest_generation_runs_draft",
                "ix_pinterest_generation_runs_creative",
                "ix_pinterest_autonomous_generation_runs_supersedes_run_id",
            },
        },
    }
    for table, contract in run_contracts.items():
        columns = {row["name"]: row for row in inspector.get_columns(table, schema="public")}
        attempt = columns.get("attempt_number")
        supersedes = columns.get("supersedes_run_id")
        if attempt is None or attempt.get("nullable") is not False:
            _refuse(f"{table} attempt_number contract mismatch")
        if supersedes is None or supersedes.get("nullable") is not True:
            _refuse(f"{table} supersedes_run_id contract mismatch")

        uniques = {
            row.get("name"): tuple(row.get("column_names") or ())
            for row in inspector.get_unique_constraints(table, schema="public")
        }
        if uniques.get(contract["attempt_uq"]) != (
            "portfolio_item_id",
            "attempt_number",
        ):
            _refuse(f"{table} attempt uniqueness contract mismatch")
        if contract["old_uqs"] & set(uniques):
            _refuse(f"{table} obsolete one-run uniqueness still present")

        foreign_keys = {
            row.get("name"): row
            for row in inspector.get_foreign_keys(table, schema="public")
        }
        fk = foreign_keys.get(contract["supersedes_fk"])
        if (
            fk is None
            or tuple(fk.get("constrained_columns") or ()) != ("supersedes_run_id",)
            or fk.get("referred_table") != table
            or tuple(fk.get("referred_columns") or ()) != ("id",)
        ):
            _refuse(f"{table} supersession foreign key contract mismatch")

        indexes = {
            row.get("name"): tuple(row.get("column_names") or ())
            for row in inspector.get_indexes(table, schema="public")
        }
        missing_indexes = sorted(contract["expected_indexes"] - set(indexes))
        if missing_indexes:
            _refuse(
                f"{table} index contract mismatch: "
                + ", ".join(missing_indexes)
            )
        input_indexes = [
            name for name, columns in indexes.items()
            if columns == ("input_fingerprint",)
        ]
        if not input_indexes:
            _refuse(f"{table} input fingerprint index contract mismatch")

    reconciliation_columns = {
        row["name"]: row
        for row in inspector.get_columns(LINEAGE_RECONCILIATION_TABLE, schema="public")
    }
    expected_columns = {
        "id",
        "portfolio_item_id",
        "failed_destination_run_id",
        "failed_execution_run_id",
        "failed_generation_run_id",
        "failed_destination_input_fingerprint",
        "failed_execution_input_fingerprint",
        "failed_generation_input_fingerprint",
        "retry_destination_input_fingerprint",
        "retry_execution_input_fingerprint",
        "retry_generation_input_fingerprint",
        "reconciliation_fingerprint",
        "status",
        "actor",
        "evidence",
        "created_at",
    }
    if set(reconciliation_columns) != expected_columns:
        _refuse("0030 reconciliation column contract mismatch")

    reconciliation_uniques = {
        row.get("name"): tuple(row.get("column_names") or ())
        for row in inspector.get_unique_constraints(LINEAGE_RECONCILIATION_TABLE, schema="public")
    }
    expected_uniques = {
        "uq_pinterest_auto_reconcile_destination": ("failed_destination_run_id",),
        "uq_pinterest_auto_reconcile_execution": ("failed_execution_run_id",),
        "uq_pinterest_auto_reconcile_generation": ("failed_generation_run_id",),
        "uq_pinterest_auto_reconcile_fingerprint": ("reconciliation_fingerprint",),
    }
    for name, columns in expected_uniques.items():
        if reconciliation_uniques.get(name) != columns:
            _refuse(f"0030 reconciliation unique contract mismatch: {name}")

    referred = {
        tuple(row.get("constrained_columns") or ()): row.get("referred_table")
        for row in inspector.get_foreign_keys(LINEAGE_RECONCILIATION_TABLE, schema="public")
    }
    expected_fks = {
        ("portfolio_item_id",): "pinterest_portfolio_plan_items",
        ("failed_destination_run_id",): "pinterest_autonomous_destination_runs",
        ("failed_execution_run_id",): "pinterest_autonomous_execution_runs",
        ("failed_generation_run_id",): "pinterest_autonomous_generation_runs",
    }
    if any(referred.get(columns) != table for columns, table in expected_fks.items()):
        _refuse("0030 reconciliation foreign key contract mismatch")

    checks = {
        row.get("name"): (row.get("sqltext") or "")
        for row in inspector.get_check_constraints(LINEAGE_RECONCILIATION_TABLE, schema="public")
    }
    status_check = checks.get(
        "ck_pinterest_autonomous_run_reconciliation_status",
        "",
    ).lower()
    normalized_status_check = re.sub(r"\s+", "", status_check)
    normalized_status_check = re.sub(
        r"::(?:character varying|varchar|text)",
        "",
        normalized_status_check,
    )
    normalized_status_check = normalized_status_check.replace("(status)", "status")
    while (
        normalized_status_check.startswith("(")
        and normalized_status_check.endswith(")")
    ):
        normalized_status_check = normalized_status_check[1:-1]
    if normalized_status_check not in {
        "status='reconciled'",
        "statusin('reconciled')",
    }:
        _refuse("0030 reconciliation status check contract mismatch")


SCHEDULED_QUOTA_RESERVATIONS_TABLE = "routine_scheduled_quota_reservations"
READINESS_ADMISSIONS_TABLE = "management_readiness_admissions"


def _require_0031_scheduled_quota_schema(connection: Any) -> None:
    """Require the exact table contract introduced by migration 0031."""
    table = SCHEDULED_QUOTA_RESERVATIONS_TABLE
    inspector = sa.inspect(connection)
    if table not in set(inspector.get_table_names(schema="public")):
        _refuse("0031 scheduled quota reservations table missing")

    expected_columns = {
        "id": (sa.String, 36, False, None),
        "publication_id": (sa.String, 36, False, None),
        "plan_id": (sa.String, 36, False, None),
        "plan_item_id": (sa.String, 36, False, None),
        "product_id": (sa.String, 36, False, None),
        "vendor_key": (sa.String, 255, False, None),
        "board_id": (sa.String, 36, False, None),
        "scheduled_for": (sa.Date, None, False, None),
        "month_start": (sa.Date, None, False, None),
        "reserved_at": (sa.DateTime, None, False, "now()"),
    }
    columns = {
        row["name"]: row
        for row in inspector.get_columns(table, schema="public")
    }
    if set(columns) != set(expected_columns):
        _refuse("0031 scheduled quota reservations column contract mismatch")
    for name, (type_class, length, nullable, default) in expected_columns.items():
        column = columns[name]
        column_type = column["type"]
        if not isinstance(column_type, type_class):
            _refuse(f"0031 scheduled quota reservations type mismatch: {name}")
        if length is not None and getattr(column_type, "length", None) != length:
            _refuse(f"0031 scheduled quota reservations length mismatch: {name}")
        if isinstance(column_type, sa.DateTime) and column_type.timezone is not True:
            _refuse("0031 scheduled quota reservations timezone mismatch: reserved_at")
        if bool(column.get("nullable")) != nullable:
            _refuse(f"0031 scheduled quota reservations nullability mismatch: {name}")
        if _normalize_default(column.get("default")) != default:
            _refuse(f"0031 scheduled quota reservations default mismatch: {name}")
        if (
            column.get("collation") is not None
            or column.get("identity") is not None
            or column.get("computed") is not None
        ):
            _refuse(f"0031 scheduled quota reservations column attributes mismatch: {name}")

    primary_key = inspector.get_pk_constraint(table, schema="public")
    if (
        primary_key.get("name") != f"{table}_pkey"
        or tuple(primary_key.get("constrained_columns") or ()) != ("id",)
    ):
        _refuse("0031 scheduled quota reservations primary key mismatch")

    expected_uniques = {
        "uq_routine_scheduled_quota_publication": ("publication_id",),
    }
    actual_uniques = {}
    for row in inspector.get_unique_constraints(table, schema="public"):
        actual_uniques[row.get("name")] = tuple(row.get("column_names") or ())
        dialect_options = row.get("dialect_options") or {}
        active_options = {
            key: value
            for key, value in dialect_options.items()
            if value not in (None, False, [], {})
        }
        if (
            row.get("deferrable") not in (None, False)
            or row.get("initially") not in (None, False)
            or active_options
        ):
            _refuse("0031 scheduled quota reservations unique contract mismatch")
    if actual_uniques != expected_uniques:
        _refuse("0031 scheduled quota reservations unique contract mismatch")

    expected_indexes = {
        "ix_routine_scheduled_quota_day": (
            ("scheduled_for", "product_id", "vendor_key", "board_id"),
            False,
        ),
        "ix_routine_scheduled_quota_month": (("month_start",), False),
    }
    actual_indexes = {}
    for row in inspector.get_indexes(table, schema="public"):
        # PostgreSQL may also report the backing index for the named unique
        # constraint; it is covered by the exact unique-constraint check.
        if row.get("duplicates_constraint"):
            continue
        actual_indexes[row.get("name")] = (
            tuple(row.get("column_names") or ()),
            bool(row.get("unique")),
        )
        dialect_options = row.get("dialect_options") or {}
        active_options = {
            key: value
            for key, value in dialect_options.items()
            if value not in (None, False, [], {})
            and not (key == "postgresql_using" and value == "btree")
        }
        if row.get("column_names") is None or active_options:
            _refuse("0031 scheduled quota reservations index definition mismatch")
    if actual_indexes != expected_indexes:
        _refuse("0031 scheduled quota reservations index contract mismatch")

    expected_foreign_keys = {
        "fk_routine_scheduled_quota_publication": (
            ("publication_id",), "pin_publications", ("id",)
        ),
        "fk_routine_scheduled_quota_plan": (
            ("plan_id",), "pinterest_portfolio_plans", ("id",)
        ),
        "fk_routine_scheduled_quota_plan_item": (
            ("plan_item_id",), "pinterest_portfolio_plan_items", ("id",)
        ),
        "fk_routine_scheduled_quota_product": (
            ("product_id",), "products", ("id",)
        ),
        "fk_routine_scheduled_quota_board": (
            ("board_id",), "boards", ("id",)
        ),
    }
    actual_foreign_keys = {}
    for row in inspector.get_foreign_keys(table, schema="public"):
        options = {
            key: str(value).upper()
            for key, value in (row.get("options") or {}).items()
        }
        actual_foreign_keys[row.get("name")] = (
            tuple(row.get("constrained_columns") or ()),
            row.get("referred_schema") or "public",
            row.get("referred_table"),
            tuple(row.get("referred_columns") or ()),
            options,
        )
    expected_foreign_keys = {
        name: (contract[0], "public", contract[1], contract[2], {"ondelete": "RESTRICT"})
        for name, contract in expected_foreign_keys.items()
    }
    if actual_foreign_keys != expected_foreign_keys:
        _refuse("0031 scheduled quota reservations foreign key contract mismatch")
    if inspector.get_check_constraints(table, schema="public"):
        _refuse("0031 scheduled quota reservations check constraint mismatch")


def _normalize_readiness_check(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"^\s*check\s*", "", text)
    text = re.sub(r"::(?:text|character varying|varchar)\b", "", text)
    return re.sub(r"[\s()]", "", text)


def _normalize_trigger_body(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _require_0032_readiness_admissions_schema(connection: Any) -> None:
    """Require the exact frozen management coordinator table and guards."""
    table = READINESS_ADMISSIONS_TABLE
    inspector = sa.inspect(connection)
    if table not in set(inspector.get_table_names(schema="public")):
        _refuse("0032 readiness admissions table missing")

    from sqlalchemy.dialects import postgresql

    expected_columns = {
        "operation": (sa.String, 64, False, None),
        "release_commit_sha": (sa.String, 64, False, None),
        "release_tree_sha": (sa.String, 64, False, None),
        "descriptor_sha256": (sa.String, 64, False, None),
        "grant_id": (sa.String, 255, False, None),
        "actor_hash": (sa.String, 64, False, None),
        "consumed_at": (sa.DateTime, None, False, "now()"),
        "outcome": (sa.String, 16, False, "'ADMITTED'"),
        "exit_code": (sa.Integer, None, True, None),
        "receipt": (postgresql.JSONB, None, True, None),
        "finished_at": (sa.DateTime, None, True, None),
    }
    columns = {
        row["name"]: row
        for row in inspector.get_columns(table, schema="public")
    }
    if set(columns) != set(expected_columns):
        _refuse("0032 readiness admissions column contract mismatch")
    for name, (type_class, length, nullable, default) in expected_columns.items():
        column = columns[name]
        column_type = column["type"]
        if not isinstance(column_type, type_class):
            _refuse(f"0032 readiness admissions type mismatch: {name}")
        if length is not None and getattr(column_type, "length", None) != length:
            _refuse(f"0032 readiness admissions length mismatch: {name}")
        if isinstance(column_type, sa.DateTime) and column_type.timezone is not True:
            _refuse(f"0032 readiness admissions timezone mismatch: {name}")
        if bool(column.get("nullable")) != nullable:
            _refuse(f"0032 readiness admissions nullability mismatch: {name}")
        if _normalize_default(column.get("default")) != default:
            _refuse(f"0032 readiness admissions default mismatch: {name}")
        if (
            column.get("collation") is not None
            or column.get("identity") is not None
            or column.get("computed") is not None
        ):
            _refuse(f"0032 readiness admissions column attributes mismatch: {name}")

    primary_key = inspector.get_pk_constraint(table, schema="public")
    if (
        primary_key.get("name") != "pk_management_readiness_admissions"
        or tuple(primary_key.get("constrained_columns") or ())
        != ("operation", "release_commit_sha", "release_tree_sha")
    ):
        _refuse("0032 readiness admissions primary key mismatch")

    uniques = {
        row.get("name"): tuple(row.get("column_names") or ())
        for row in inspector.get_unique_constraints(table, schema="public")
    }
    if uniques != {
        "uq_management_readiness_admissions_grant": ("grant_id",),
    }:
        _refuse("0032 readiness admissions unique contract mismatch")
    if inspector.get_foreign_keys(table, schema="public"):
        _refuse("0032 readiness admissions must not reference business tables")

    checks = {
        row.get("name"): _normalize_readiness_check(row.get("sqltext"))
        for row in inspector.get_check_constraints(table, schema="public")
    }
    expected_checks = {
        "ck_management_readiness_admissions_operation":
            "operation='object_storage_readiness_v1'",
        "ck_management_readiness_admissions_descriptor_hash":
            "descriptor_sha256~'^[0-9a-f]{64}$'",
        "ck_management_readiness_admissions_actor_hash":
            "actor_hash~'^[0-9a-f]{64}$'",
        "ck_management_readiness_admissions_outcome":
            "outcome='admitted'oroutcome='pass'or"
            "outcome='failed'oroutcome='unknown'",
        "ck_management_readiness_admissions_outcome_evidence":
            "outcome='admitted'andexit_codeisnullandreceiptisnull"
            "andfinished_atisnullor"
            "outcome='pass'andexit_code=0andreceiptisnotnulland"
            "jsonb_typeofreceipt='object'andnotreceipt->>'final_status'"
            "isdistinctfrom'pass'andfinished_atisnotnullor"
            "outcome='failed'andexit_codeisnotnulland"
            "exit_code=anyarray[1,2]"
            "andreceiptisnotnullandjsonb_typeofreceipt='object'and"
            "exit_code=1andnotreceipt->>'final_status'isdistinctfrom"
            "'failed'or"
            "exit_code=2andnotreceipt->>'final_status'isdistinctfrom"
            "'blocked'andfinished_atisnotnullor"
            "outcome='unknown'andfinished_atisnotnulland"
            "receiptisnullorjsonb_typeofreceipt='object'and"
            "exit_codeisnotnulland"
            "exit_code=0andnotreceipt->>'final_status'isdistinctfrom"
            "'pass'or"
            "exit_code=1andnotreceipt->>'final_status'isdistinctfrom"
            "'failed'or"
            "exit_code=2andnotreceipt->>'final_status'isdistinctfrom"
            "'blocked'",
    }
    if checks != expected_checks:
        _refuse("0032 readiness admissions check contract mismatch")
    constraint_rows = _canonical_constraint_rows(connection, table)
    actual_constraints = {
        str(row["conname"]): (
            str(row["contype"]),
            bool(row["condeferrable"]),
            bool(row["condeferred"]),
            bool(row["convalidated"]),
        )
        for row in constraint_rows
    }
    if actual_constraints != {
        "pk_management_readiness_admissions": ("p", False, False, True),
        "uq_management_readiness_admissions_grant": ("u", False, False, True),
        "ck_management_readiness_admissions_operation": ("c", False, False, True),
        "ck_management_readiness_admissions_descriptor_hash":
            ("c", False, False, True),
        "ck_management_readiness_admissions_actor_hash": ("c", False, False, True),
        "ck_management_readiness_admissions_outcome": ("c", False, False, True),
        "ck_management_readiness_admissions_outcome_evidence":
            ("c", False, False, True),
    }:
        _refuse("0032 readiness admissions constraint catalog mismatch")

    # Constraint backing indexes are required; no standalone index, partial
    # index, invalid index or additional access path is accepted.
    standalone_indexes = [
        row for row in inspector.get_indexes(table, schema="public")
        if not row.get("duplicates_constraint")
    ]
    if standalone_indexes:
        _refuse("0032 readiness admissions index contract mismatch")
    index_rows = connection.execute(sa.text(
        """SELECT idx.relname AS name, i.indisunique AS is_unique,
                  i.indisvalid AS is_valid, i.indisready AS is_ready,
                  i.indnkeyatts AS key_count, i.indnatts AS column_count,
                  i.indnullsnotdistinct AS nulls_not_distinct,
                  am.amname AS access_method,
                  pg_get_expr(i.indpred, i.indrelid) AS predicate,
                  pg_get_indexdef(i.indexrelid) AS definition
           FROM pg_index i
           JOIN pg_class tbl ON tbl.oid = i.indrelid
           JOIN pg_class idx ON idx.oid = i.indexrelid
           JOIN pg_am am ON am.oid = idx.relam
           JOIN pg_namespace n ON n.oid = tbl.relnamespace
           WHERE n.nspname = 'public' AND tbl.relname = :table
           ORDER BY idx.relname"""
    ), {"table": table}).mappings().all()
    actual_indexes = {
        str(row["name"]): (
            bool(row["is_unique"]),
            bool(row["is_valid"]),
            bool(row["is_ready"]),
            int(row["key_count"]),
            int(row["column_count"]),
            bool(row["nulls_not_distinct"]),
            str(row["access_method"]),
            row["predicate"],
            _normalize_sql(row["definition"]),
        )
        for row in index_rows
    }
    if actual_indexes != {
        "pk_management_readiness_admissions":
            (
                True, True, True, 3, 3, False, "btree", None,
                "CREATE UNIQUE INDEX pk_management_readiness_admissions ON "
                "public.management_readiness_admissions USING btree "
                "(operation, release_commit_sha, release_tree_sha)",
            ),
        "uq_management_readiness_admissions_grant":
            (
                True, True, True, 1, 1, False, "btree", None,
                "CREATE UNIQUE INDEX uq_management_readiness_admissions_grant "
                "ON public.management_readiness_admissions USING btree (grant_id)",
            ),
    }:
        _refuse("0032 readiness admissions index catalog mismatch")

    table_state = connection.execute(sa.text(
        """SELECT c.relkind, c.relpersistence, c.relispartition,
                  c.relrowsecurity, c.relforcerowsecurity
           FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relname = :table"""
    ), {"table": table}).mappings().one()
    if (
        table_state["relkind"] != "r"
        or table_state["relpersistence"] != "p"
        or table_state["relispartition"]
        or table_state["relrowsecurity"]
        or table_state["relforcerowsecurity"]
    ):
        _refuse("0032 readiness admissions relation attributes mismatch")

    inheritance_count = connection.scalar(sa.text(
        """SELECT count(*)
           FROM pg_inherits i
           JOIN pg_class parent ON parent.oid = i.inhparent
           JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
           JOIN pg_class child ON child.oid = i.inhrelid
           JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
           WHERE (parent_ns.nspname = 'public' AND parent.relname = :table)
              OR (child_ns.nspname = 'public' AND child.relname = :table)"""
    ), {"table": table})
    if inheritance_count:
        _refuse("0032 readiness admissions inheritance mismatch")
    rules = connection.scalar(sa.text(
        """SELECT count(*)
           FROM pg_rewrite r
           JOIN pg_class c ON c.oid = r.ev_class
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relname = :table
             AND r.rulename <> '_RETURN'"""
    ), {"table": table})
    if rules:
        _refuse("0032 readiness admissions rewrite-rule mismatch")

    guard_body = _normalize_trigger_body("""
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.outcome <> 'ADMITTED'
                   OR NEW.finished_at IS NOT NULL
                   OR NEW.exit_code IS NOT NULL
                   OR NEW.receipt IS NOT NULL THEN
                    RAISE EXCEPTION 'readiness admission must start ADMITTED'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'readiness admissions cannot be deleted'
                    USING ERRCODE = '55000';
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.outcome <> 'ADMITTED' THEN
                    RAISE EXCEPTION 'terminal readiness outcomes are immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF ROW(
                    NEW.operation,
                    NEW.release_commit_sha,
                    NEW.release_tree_sha,
                    NEW.descriptor_sha256,
                    NEW.grant_id,
                    NEW.actor_hash,
                    NEW.consumed_at
                ) IS DISTINCT FROM ROW(
                    OLD.operation,
                    OLD.release_commit_sha,
                    OLD.release_tree_sha,
                    OLD.descriptor_sha256,
                    OLD.grant_id,
                    OLD.actor_hash,
                    OLD.consumed_at
                ) THEN
                    RAISE EXCEPTION 'readiness admission binding is immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome NOT IN ('PASS', 'FAILED', 'UNKNOWN')
                   OR NEW.finished_at IS NULL THEN
                    RAISE EXCEPTION 'readiness outcome must transition once to terminal'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome = 'PASS' AND (
                    NEW.exit_code IS DISTINCT FROM 0
                    OR NEW.receipt IS NULL
                    OR jsonb_typeof(NEW.receipt) IS DISTINCT FROM 'object'
                    OR NEW.receipt ->> 'final_status' IS DISTINCT FROM 'PASS'
                ) THEN
                    RAISE EXCEPTION 'PASS readiness requires a valid PASS receipt'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome = 'FAILED' AND (
                    NEW.receipt IS NULL
                    OR jsonb_typeof(NEW.receipt) IS DISTINCT FROM 'object'
                    OR NEW.exit_code IS NULL
                    OR NOT (
                        (NEW.exit_code = 1 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'FAILED')
                        OR
                        (NEW.exit_code = 2 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'BLOCKED')
                    )
                ) THEN
                    RAISE EXCEPTION 'FAILED readiness requires matching failed receipt'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.outcome = 'UNKNOWN' AND NEW.receipt IS NOT NULL AND (
                    jsonb_typeof(NEW.receipt) IS DISTINCT FROM 'object'
                    OR NEW.exit_code IS NULL
                    OR NOT (
                        (NEW.exit_code = 0 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'PASS')
                        OR
                        (NEW.exit_code = 1 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'FAILED')
                        OR
                        (NEW.exit_code = 2 AND
                         NEW.receipt ->> 'final_status' IS NOT DISTINCT FROM 'BLOCKED')
                    )
                ) THEN
                    RAISE EXCEPTION 'UNKNOWN readiness receipt and exit code mismatch'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'unsupported readiness admission operation'
                USING ERRCODE = '55000';
        END;
    """)
    truncate_body = _normalize_trigger_body("""
        BEGIN
            RAISE EXCEPTION 'readiness admissions cannot be truncated'
                USING ERRCODE = '55000';
        END;
    """)
    trigger_rows = connection.execute(sa.text(
        """SELECT t.tgname, t.tgenabled, t.tgtype, t.tgisinternal,
                  t.tgattr::text <> '' AS has_update_columns,
                  t.tgqual IS NOT NULL AS has_when,
                  t.tgnargs, t.tgoldtable, t.tgnewtable,
                  p.proname, p.prosrc, p.prosecdef, p.proconfig,
                  p.pronargs, l.lanname, pn.nspname AS function_schema,
                  p.provolatile, p.proisstrict, p.proleakproof, p.proparallel,
                  p.prokind, p.proretset, rt.typname AS return_type
           FROM pg_trigger t
           JOIN pg_class c ON c.oid = t.tgrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
           JOIN pg_proc p ON p.oid = t.tgfoid
           JOIN pg_namespace pn ON pn.oid = p.pronamespace
           JOIN pg_language l ON l.oid = p.prolang
           JOIN pg_type rt ON rt.oid = p.prorettype
           WHERE n.nspname = 'public' AND c.relname = :table
           ORDER BY t.tgname"""
    ), {"table": table}).mappings().all()
    actual_triggers = {}
    for row in trigger_rows:
        actual_triggers[str(row["tgname"])] = (
            str(row["tgenabled"]),
            int(row["tgtype"]),
            bool(row["tgisinternal"]),
            bool(row["has_update_columns"]),
            bool(row["has_when"]),
            int(row["tgnargs"]),
            bool(row["tgoldtable"]),
            bool(row["tgnewtable"]),
            str(row["function_schema"]),
            str(row["proname"]),
            _normalize_trigger_body(row["prosrc"]),
            bool(row["prosecdef"]),
            row["proconfig"],
            int(row["pronargs"]),
            str(row["lanname"]),
            str(row["provolatile"]),
            bool(row["proisstrict"]),
            bool(row["proleakproof"]),
            str(row["proparallel"]),
            str(row["prokind"]),
            bool(row["proretset"]),
            str(row["return_type"]),
        )
    if actual_triggers != {
        "management_readiness_admissions_immutable": (
            "O", 31, False, False, False, 0, False, False,
            "public", "management_readiness_admission_guard",
            guard_body, False, None, 0, "plpgsql",
            "v", False, False, "u", "f", False, "trigger",
        ),
        "management_readiness_admissions_no_truncate": (
            "O", 34, False, False, False, 0, False, False,
            "public", "management_readiness_admission_truncate_guard",
            truncate_body, False, None, 0, "plpgsql",
            "v", False, False, "u", "f", False, "trigger",
        ),
    }:
        _refuse("0032 readiness admissions trigger contract mismatch")

    function_names = connection.execute(sa.text(
        """SELECT p.proname, count(*) AS count
           FROM pg_proc p
           JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'public'
             AND p.proname IN (
                 'management_readiness_admission_guard',
                 'management_readiness_admission_truncate_guard'
             )
           GROUP BY p.proname"""
    )).mappings().all()
    if {row["proname"]: int(row["count"]) for row in function_names} != {
        "management_readiness_admission_guard": 1,
        "management_readiness_admission_truncate_guard": 1,
    }:
        _refuse("0032 readiness admissions function catalog mismatch")


def verify_frozen_schema_at_head(connection: Any, revision: str = "0031") -> None:
    """Read-only production startup guard for canonical migration contracts."""
    if getattr(connection.dialect, "name", None) != "postgresql":
        return
    _require_exact_alembic_revision(connection, revision)

    if revision == "0029":
        present = _present_postgresql_tables(connection, BUNDLE_TABLES)
        if tuple(present) != BUNDLE_TABLES:
            _refuse(f"canonical schema table presence mismatch: {present}")
        fingerprints = _table_fingerprints(connection, BUNDLE_TABLES)
        canonical = {
            table: FROZEN_FINGERPRINTS[table]
            for table in BUNDLE_TABLES
        }
        if fingerprints != canonical:
            _refuse("canonical schema fingerprint verification failed")
        return

    if revision not in {"0030", "0031", "0032", "0033", "0034"}:
        _refuse(f"unsupported canonical head revision: {revision}")

    present = _present_postgresql_tables(connection, LINEAGE_PRESERVED_TABLES)
    if tuple(present) != LINEAGE_PRESERVED_TABLES:
        _refuse(f"0030 preserved schema table presence mismatch: {present}")
    fingerprints = _table_fingerprints(connection, LINEAGE_PRESERVED_TABLES)
    canonical = {
        table: FROZEN_FINGERPRINTS[table]
        for table in LINEAGE_PRESERVED_TABLES
    }
    if fingerprints != canonical:
        _refuse("0030 preserved schema fingerprint verification failed")
    _require_0030_lineage_schema(connection)
    if revision in {"0031", "0032", "0033", "0034"}:
        _require_frozen_0030_lineage_catalog(connection)
        _require_0031_scheduled_quota_schema(connection)
    if revision == "0032":
        _require_0032_readiness_admissions_schema(connection)
    if revision in {"0033", "0034"}:
        from app.db.readiness_schema_0033 import verify
        verify(connection)
    if revision == "0034":
        from app.db.bounded_batch_schema_0034 import verify
        verify(connection)


def adopt_managed_preapplied_0033(connection: Any) -> bool:
    """Bookkeeping only: exact empty managed schema, never DDL or repair.

    Caller owns the transaction and commit. Recorded 0033 is verify-only.
    The historical 0031 adopter remains unchanged for historical callers.
    """
    from app.db.readiness_schema_0033 import TABLES, verify
    from app.db.migration_lock import MIGRATION_ADVISORY_LOCK_KEY

    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("0033 adoption requires PostgreSQL")
    if not connection.in_transaction() or connection.get_isolation_level() != "READ COMMITTED":
        _refuse("0033 adoption requires a READ COMMITTED transaction")
    connection.execute(sa.text("SET LOCAL search_path TO public, pg_catalog"))
    connection.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    connection.execute(sa.text("SET LOCAL statement_timeout = '8s'"))
    if not connection.scalar(sa.text("SELECT pg_try_advisory_xact_lock(:key)"),
                             {"key": MIGRATION_ADVISORY_LOCK_KEY}):
        _refuse("0033 adoption migration lock unavailable")
    tables = (
        "alembic_version", *TABLES, *LINEAGE_PRESERVED_TABLES,
        *LINEAGE_RUN_TABLES, LINEAGE_RECONCILIATION_TABLE,
        SCHEDULED_QUOTA_RESERVATIONS_TABLE, "routine_publishing_control",
        "pin_publications", "routine_publishing_runs", "routine_dispatch_permits",
    )
    try:
        # Version first, matching the coordinator's lock order.
        for table in ("alembic_version", *sorted(set(tables) - {"alembic_version"})):
            connection.execute(sa.text(
                f'LOCK TABLE "public"."{table}" IN SHARE ROW EXCLUSIVE MODE NOWAIT'
            ))
    except sa.exc.DBAPIError:
        _refuse("0033 adoption lock or table availability failed")
    revisions = connection.execute(sa.text(
        'SELECT version_num FROM public.alembic_version ORDER BY version_num'
    )).scalars().all()
    if revisions == ["0033"]:
        verify_frozen_schema_at_head(connection, revision="0033")
        return False
    if revisions != ["0031"]:
        _refuse("0033 managed adoption requires exactly one Alembic revision 0031")
    verify_frozen_schema_at_head(connection, revision="0031")
    verify(connection)
    _require_empty_tables(connection, TABLES)
    if connection.execute(sa.text(
        "SELECT id, state FROM public.routine_publishing_control ORDER BY id"
    )).all() != [("default", "PAUSED")]:
        _refuse("0033 adoption requires exactly one PAUSED default routine control")
    for table, predicate in (
        ("pin_publications", "status IN ('PUBLISHING', 'PUBLISH_UNKNOWN')"),
        ("routine_publishing_runs", "status = 'RUNNING'"),
        ("routine_dispatch_permits", "status = 'ACTIVE'"),
    ):
        if connection.scalar(sa.text(f'SELECT count(*) FROM public."{table}" WHERE {predicate}')):
            _refuse("0033 adoption refuses live state")
    updated = connection.execute(sa.text(
        "UPDATE public.alembic_version SET version_num='0033' "
        "WHERE version_num='0031' RETURNING version_num"
    )).scalars().all()
    if updated != ["0033"]:
        _refuse("0033 bookkeeping update was not exactly once")
    verify_frozen_schema_at_head(connection, revision="0033")
    return True


def adopt_managed_preapplied_0031(connection: Any) -> bool:
    """Adopt a complete, empty pre-applied 0031 schema; verify 0032 as a no-op.

    The caller owns a transaction and must commit only after this returns.
    NOWAIT relation locks prevent both competing adopters and concurrent
    changes to the catalog/data examined before the bookkeeping update.
    This function never runs migrations or advances an already-bookkept 0031
    database; production startup's separate 0032 guard refuses that state.
    """
    if getattr(connection.dialect, "name", None) != "postgresql":
        _refuse("0031 managed adoption requires PostgreSQL")
    if not connection.in_transaction():
        _refuse("0031 managed adoption requires a transaction")
    if connection.get_isolation_level() != "READ COMMITTED":
        _refuse("0031 managed adoption requires READ COMMITTED")

    # Existing frozen inspectors and bookkeeping queries resolve unqualified
    # names. Bind them to public for this entire transaction.
    connection.execute(sa.text("SET LOCAL search_path TO public, pg_catalog"))
    tables = (
        "alembic_version",
        *LINEAGE_PRESERVED_TABLES,
        *LINEAGE_RUN_TABLES,
        LINEAGE_RECONCILIATION_TABLE,
        SCHEDULED_QUOTA_RESERVATIONS_TABLE,
        "routine_publishing_control",
        "pin_publications",
        "routine_publishing_runs",
        "routine_dispatch_permits",
    )
    try:
        for table in sorted(set(tables)):
            connection.execute(sa.text(
                f'LOCK TABLE "public"."{table}" IN SHARE ROW EXCLUSIVE MODE NOWAIT'
            ))
    except sa.exc.DBAPIError as exc:
        _refuse(f"0031 managed adoption lock or table availability failed: {exc.__class__.__name__}")

    revisions = connection.execute(sa.text(
        'SELECT version_num FROM "public"."alembic_version" ORDER BY version_num'
    )).scalars().all()
    if revisions == ["0032"]:
        admissions_table_exists = connection.scalar(sa.text(
            "SELECT to_regclass('public.management_readiness_admissions') "
            "IS NOT NULL"
        ))
        if not admissions_table_exists:
            _refuse(
                "0032 managed schema verification failed: "
                "0032 readiness admissions table missing"
            )
        try:
            connection.execute(sa.text(
                'LOCK TABLE "public"."management_readiness_admissions" '
                "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
            ))
        except sa.exc.DBAPIError as exc:
            _refuse(
                "0032 managed schema verification lock or table availability "
                f"failed: {exc.__class__.__name__}"
            )
        verify_frozen_schema_at_head(connection, revision="0032")
        return False
    if revisions == ["0031"]:
        verify_frozen_schema_at_head(connection, revision="0031")
        return False
    if revisions != ["0030"]:
        _refuse("0031 managed adoption requires exactly one Alembic revision 0030")

    verify_frozen_schema_at_head(connection, revision="0030")
    _require_frozen_0030_lineage_catalog(connection)
    _require_0031_scheduled_quota_schema(connection)
    if connection.scalar(sa.text(
        'SELECT count(*) FROM "public"."routine_scheduled_quota_reservations"'
    )) != 0:
        _refuse("0031 scheduled quota reservations must be empty")

    controls = connection.execute(sa.text(
        'SELECT id, state FROM "public"."routine_publishing_control" ORDER BY id'
    )).all()
    if controls != [("default", "PAUSED")]:
        _refuse("0031 managed adoption requires exactly one PAUSED default routine control")
    for table, predicate, label in (
        ("pin_publications", "status = 'PUBLISH_UNKNOWN'", "PUBLISH_UNKNOWN"),
        ("pin_publications", "status = 'PUBLISHING'", "PUBLISHING"),
        ("routine_publishing_runs", "status = 'RUNNING'", "running routine runs"),
        ("routine_dispatch_permits", "status = 'ACTIVE'", "active permits"),
    ):
        # The table and predicate are fixed constants, not caller-provided SQL.
        if connection.scalar(sa.text(
            f'SELECT count(*) FROM "public"."{table}" WHERE {predicate}'
        )) != 0:
            _refuse(f"0031 managed adoption requires zero {label}")

    updated = connection.execute(sa.text(
        'UPDATE "public"."alembic_version" SET version_num=\'0031\' '
        "WHERE version_num='0030' RETURNING version_num"
    )).scalars().all()
    if updated != ["0031"]:
        _refuse("0031 managed adoption bookkeeping update was not exactly once")
    verify_frozen_schema_at_head(connection, revision="0031")
    return True


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