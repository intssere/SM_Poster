"""Frozen management-only relational contract; never initializes application DBs.

Insert-only coordinator code is not protection against unrestricted SQL
UPDATE/DELETE/TRUNCATE. This revision deliberately makes no such claim.
"""
from __future__ import annotations

import re
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

TABLES = ("management_readiness_admissions", "management_readiness_outcomes")
SCOPE = ("operation", "release_commit_sha", "release_tree_sha")
LEGACY_FUNCTIONS = (
    "management_readiness_admission_guard",
    "management_readiness_admission_truncate_guard",
)
HISTORICAL_0032_SHA256 = "fac94a530f1ed98e8e862baa84fdacf31505c0f9aa29186898148f25479fbd14"

# Reviewed PostgreSQL catalog fingerprints, independent of application metadata.
FINGERPRINTS = {
    "management_readiness_admissions": "0bc0fb3196d337f8b465135f180c1866f3595fae61bffbf999361e06e34a358c",
    "management_readiness_outcomes": "e1a2461c0e977f46d63a5a70d1916824b63a8aeece5b578dc493998cb21fd8db",
}

EVIDENCE = """(
 (outcome = 'PASS' AND exit_code = 0 AND receipt IS NOT NULL
  AND jsonb_typeof(receipt) = 'object'
  AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'PASS')
 OR
 (outcome = 'FAILED' AND exit_code IS NOT NULL AND receipt IS NOT NULL
  AND jsonb_typeof(receipt) = 'object' AND (
   (exit_code = 1 AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'FAILED')
   OR (exit_code = 2 AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'BLOCKED')))
 OR
 (outcome = 'UNKNOWN' AND (receipt IS NULL OR (
  jsonb_typeof(receipt) = 'object' AND exit_code IS NOT NULL AND (
   (exit_code = 0 AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'PASS')
   OR (exit_code = 1 AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'FAILED')
   OR (exit_code = 2 AND receipt ->> 'final_status' IS NOT DISTINCT FROM 'BLOCKED')))))
) IS TRUE"""


def tables(metadata: sa.MetaData) -> tuple[sa.Table, sa.Table]:
    """Frozen revision DDL, separate from business ORM and live models."""
    admissions = sa.Table(
        TABLES[0], metadata,
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("release_commit_sha", sa.String(64), nullable=False),
        sa.Column("release_tree_sha", sa.String(64), nullable=False),
        sa.Column("descriptor_sha256", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(255), nullable=False),
        sa.Column("actor_hash", sa.String(64), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint(*SCOPE, name="pk_management_readiness_admissions"),
        sa.UniqueConstraint("grant_id", name="uq_management_readiness_admissions_grant"),
        sa.CheckConstraint("operation = 'object_storage_readiness_v1'",
                           name="ck_management_readiness_admissions_operation"),
        sa.CheckConstraint("descriptor_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_management_readiness_admissions_descriptor_hash"),
        sa.CheckConstraint("actor_hash ~ '^[0-9a-f]{64}$'",
                           name="ck_management_readiness_admissions_actor_hash"),
        schema="public",
    )
    outcomes = sa.Table(
        TABLES[1], metadata,
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("release_commit_sha", sa.String(64), nullable=False),
        sa.Column("release_tree_sha", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("receipt", JSONB(none_as_null=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint(*SCOPE, name="pk_management_readiness_outcomes"),
        sa.ForeignKeyConstraint(
            SCOPE, [f"public.{TABLES[0]}.{column}" for column in SCOPE],
            name="fk_management_readiness_outcomes_admission",
            ondelete="NO ACTION", onupdate="NO ACTION",
        ),
        sa.CheckConstraint(
            "outcome = 'PASS' OR outcome = 'FAILED' OR outcome = 'UNKNOWN'",
            name="ck_management_readiness_outcomes_outcome",
        ),
        sa.CheckConstraint(EVIDENCE, name="ck_management_readiness_outcomes_evidence"),
        schema="public",
    )
    return admissions, outcomes


def validate_terminal(outcome, exit_code, receipt):
    """Provider-free evidence validation, also used by the migration."""
    if (
        outcome not in {"PASS", "FAILED", "UNKNOWN"}
        or (exit_code is not None and type(exit_code) is not int)
        or (receipt is not None and not isinstance(receipt, dict))
    ):
        raise ValueError("invalid terminal evidence")
    safe = None
    if receipt is not None:
        from app.services.readiness_execution_runner import validate_receipt
        safe = validate_receipt(receipt, exit_code)
    if outcome == "PASS" and (
        exit_code != 0 or safe is None or safe["final_status"] != "PASS"
    ):
        raise ValueError("invalid PASS evidence")
    if outcome == "FAILED" and (
        safe is None or safe["final_status"] not in {"FAILED", "BLOCKED"}
    ):
        raise ValueError("invalid FAILED evidence")
    return safe


def validate_consumption(row):
    if (
        row["operation"] != "object_storage_readiness_v1"
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", row["release_commit_sha"])
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", row["release_tree_sha"])
        or not re.fullmatch(r"[0-9a-f]{64}", row["descriptor_sha256"])
        or not re.fullmatch(r"[0-9a-f]{64}", row["actor_hash"])
        or not isinstance(row["grant_id"], str)
        or not 1 <= len(row["grant_id"]) <= 255
    ):
        raise ValueError("invalid consumption binding")


def verify(connection, *, evidence=True):
    """Exact, read-only verification; expected catalogs never come from ORM."""
    from app.db.migration_adoption import (
        _catalog_contract, _fingerprint, _refuse,
    )
    for table, expected in FINGERPRINTS.items():
        if _fingerprint(_catalog_contract(connection, table)) != expected:
            _refuse(f"0033 {table} catalog mismatch")
        state = connection.execute(sa.text("""
            SELECT c.relkind, c.relpersistence, c.relispartition,
                   c.relrowsecurity, c.relforcerowsecurity, c.relreplident,
                   c.reloptions, am.amname
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_am am ON am.oid=c.relam
            WHERE n.nspname='public' AND c.relname=:table
        """), {"table": table}).mappings().one()
        if tuple(state.values()) != ("r", "p", False, False, False, "d", None, "heap"):
            _refuse("0033 relation attributes mismatch")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND left(p.proname, 21) = 'management_readiness_'
    """)):
        _refuse("0033 legacy readiness functions present")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname = ANY(:tables)
          AND (NOT t.tgisinternal OR t.tgenabled <> 'O'
               OR NOT EXISTS (
                   SELECT 1 FROM pg_constraint k WHERE k.oid=t.tgconstraint
                   AND k.conname='fk_management_readiness_outcomes_admission'
                   AND k.contype='f'))
    """), {"tables": list(TABLES)}):
        _refuse("0033 unexpected or disabled readiness triggers")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_inherits
        WHERE inhparent = ANY(CAST(:relations AS text[])::regclass[])
           OR inhrelid = ANY(CAST(:relations AS text[])::regclass[])
    """),
        {"relations": [f"public.{t}" for t in TABLES]}):
        _refuse("0033 inheritance mismatch")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_rewrite r
        WHERE r.ev_class = ANY(CAST(:relations AS text[])::regclass[])
    """), {"relations": [f"public.{t}" for t in TABLES]}):
        _refuse("0033 rewrite rules present")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relname = ANY(:tables) AND n.nspname <> 'public'
    """), {"tables": list(TABLES)}):
        _refuse("0033 readiness relation exists outside public")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM pg_constraint c
        WHERE c.contype='f'
          AND c.confrelid = ANY(CAST(:relations AS text[])::regclass[])
          AND NOT (c.conrelid='public.management_readiness_outcomes'::regclass
                   AND c.conname='fk_management_readiness_outcomes_admission')
    """), {"relations": [f"public.{t}" for t in TABLES]}):
        _refuse("0033 unexpected incoming foreign key")
    if connection.scalar(sa.text("""
        SELECT count(*) FROM public.management_readiness_outcomes o
        LEFT JOIN public.management_readiness_admissions a
        USING (operation, release_commit_sha, release_tree_sha)
        WHERE a.operation IS NULL
    """)):
        _refuse("0033 orphan outcome")
    if evidence:
        for row in connection.execute(sa.text(
            "SELECT * FROM public.management_readiness_admissions"
        )).mappings():
            try:
                validate_consumption(row)
            except Exception:
                _refuse("0033 invalid consumption binding")
        for row in connection.execute(sa.text(
            "SELECT outcome, exit_code, receipt FROM public.management_readiness_outcomes"
        )).mappings():
            try:
                validate_terminal(row["outcome"], row["exit_code"], row["receipt"])
            except Exception:
                _refuse("0033 invalid terminal evidence")