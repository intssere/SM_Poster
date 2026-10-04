"""Closed-state export/import/certification. No provider clients, settings or ORM."""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from functools import wraps
import json
import os
from pathlib import Path
import re

import sqlalchemy as sa

from .catalog import (
    Refused, canonical, dependencies, digest, ordered_rows, validate_catalog,
    validate_references,
)
from .policy import AUTHORIZATION_TABLES, FORMAT, PRESERVED, SOURCE, TARGET_ONLY


def safe_errors(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Refused:
            raise
        except Exception:
            # DB errors can include offending row values, even with hidden SQL
            # parameters. Public entry points never propagate those messages.
            raise Refused("State transfer operation refused; transaction rolled back") from None
    return run


@contextmanager
def transaction(engine, *, readonly=True):
    if engine.dialect.name != "postgresql":
        raise Refused("PostgreSQL required")
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
        with c.begin():
            if readonly:
                c.execute(sa.text("SET TRANSACTION READ ONLY"))
            c.execute(sa.text("SET LOCAL timezone TO 'UTC'"))
            c.execute(sa.text("SET LOCAL lock_timeout TO '5s'"))
            c.execute(sa.text("SET LOCAL statement_timeout TO '120s'"))
            yield c


def count(c, name):
    # Only fixed, reviewed identifiers are used.
    if name not in SOURCE and name not in TARGET_ONLY:
        raise Refused("Unreviewed identifier")
    return c.scalar(sa.text(f'SELECT count(*) FROM public."{name}"'))


def closed_state(c):
    for name in AUTHORIZATION_TABLES:
        # Expired/revoked historical permissions need not have consumed_at.
        # Every other unconsumed/open state is refused, even if time-expired.
        if c.scalar(sa.text(
            f'SELECT count(*) FROM public."{name}" WHERE '
            "status NOT IN ('CONSUMED','EXPIRED','REVOKED') OR "
            "(status='CONSUMED' AND consumed_at IS NULL) OR "
            "(status='REVOKED' AND revoked_at IS NULL)"
        )):
            raise Refused(f"Open or inconsistent authorization: {name}")
    publications = dict(c.execute(sa.text(
        "SELECT status::text,count(*) FROM public.pin_publications GROUP BY status"
    )).all())
    if publications != {"PUBLISHED": 4, "CANCELLED": 5}:
        raise Refused("Publication history must be exactly 4 PUBLISHED / 5 CANCELLED")
    for name in ("routine_publishing_runs", "catalog_sync_jobs",
                 "pinterest_autonomous_generation_runs",
                 "pinterest_autonomous_execution_runs",
                 "pinterest_autonomous_destination_runs"):
        if c.scalar(sa.text(
            f'SELECT count(*) FROM public."{name}" '
            "WHERE status IN ('RUNNING','STARTED','QUEUED','PENDING')"
        )):
            raise Refused(f"Unfinished runtime work: {name}")
    if c.scalar(sa.text(
        "SELECT count(*) FROM public.routine_publishing_control"
    )) != 1 or c.scalar(sa.text(
        "SELECT count(*) FROM public.routine_publishing_control WHERE state='PAUSED'"
    )) != 1:
        raise Refused("Exactly one PAUSED routine control required")
    return publications


def records(c, name, table):
    """Canonical PostgreSQL JSON plus SQL-null mask: no numeric decoding loss.

    SQL NULL and JSON literal null are distinct and must survive the transfer.
    The DB JSONB renderer normalizes JSON object key order on every version.
    """
    columns = sorted(col.name for col in table.columns)
    masks = ",".join(
        f"CASE WHEN t.\"{col}\" IS NULL THEN '{col}'::text END" for col in columns
    )
    rows = c.execute(sa.text(
        f'SELECT to_jsonb(t)::text AS values,'
        f'array_remove(ARRAY[{masks}],NULL) AS sql_nulls '
        f'FROM public."{name}" t'
    )).mappings()
    return sorted((dict(row) for row in rows), key=canonical)


def media_manifest(rows):
    # Sensitive bundle only: URLs may contain signed parameters. Never stdout.
    return {
        "phase": "OUT_OF_BAND_NOT_CERTIFIED",
        "creatives": [
            {k: json.loads(r["values"])[k] for k in ("id", "rendered_url", "sha256")}
            for r in rows["pin_creatives"]
        ],
        "publications": [
            {k: json.loads(r["values"])[k] for k in ("id", "media_url_snapshot")}
            for r in rows["pin_publications"]
        ],
        "object_bytes_copied": False,
    }


@safe_errors
def export_source(engine):
    with transaction(engine) as c:
        hashes = validate_catalog(c, "0031")
        metadata, order, parents, self_refs = dependencies(c)
        observed = {n: count(c, n) for n in SOURCE}
        if observed != {n: rule.count for n, rule in SOURCE.items()}:
            raise Refused("Source row counts differ from reviewed inventory")
        publications = closed_state(c)
        snapshot = dict(c.execute(sa.text(
            "SELECT current_setting('server_version') AS postgres_version,"
            "current_setting('server_version_num') AS postgres_version_num,"
            "pg_current_snapshot()::text AS transaction_snapshot,"
            "transaction_timestamp()::text AS transaction_time,"
            "current_setting('transaction_isolation') AS isolation,"
            "current_setting('transaction_read_only') AS read_only"
        )).mappings().one())
        source_rows = {n: records(c, n, metadata.tables[f"public.{n}"]) for n in SOURCE}
        rows = {n: source_rows[n] if n in PRESERVED else [] for n in SOURCE}
        manifest = {
            "format": FORMAT, "source_revision": "0031", "target_revision": "0034",
            "snapshot": snapshot, "dependency_order": order, "dependencies": parents,
            "self_dependencies": self_refs, "source_schema_fingerprints": hashes,
            "tables": {
                n: {"source_count": observed[n], "exported_count": len(rows[n]),
                    "content_sha256": digest(rows[n]),
                    "source_content_sha256": digest(source_rows[n]),
                    "category": SOURCE[n].category,
                    "action": SOURCE[n].action}
                for n in SOURCE
            },
            "publication_status_counts": publications,
            "target_only_policy": TARGET_ONLY,
            "media": media_manifest(rows),
        }
        manifest["manifest_sha256"] = digest(manifest)
        return {"manifest": manifest, "rows": rows}


def verify_bundle(bundle, expected_sha256):
    try:
        if set(bundle) != {"manifest", "rows"}:
            raise Refused("Invalid bundle structure")
        m, rows = bundle["manifest"], bundle["rows"]
        unsigned = {k: v for k, v in m.items() if k != "manifest_sha256"}
        if not expected_sha256 or m["manifest_sha256"] != expected_sha256:
            raise Refused("Expected manifest fingerprint differs")
        if digest(unsigned) != expected_sha256:
            raise Refused("Manifest fingerprint differs")
        if m["format"] != FORMAT or m["source_revision"] != "0031" or m["target_revision"] != "0034":
            raise Refused("Unsupported transfer contract")
        if set(rows) != set(SOURCE) or set(m["tables"]) != set(SOURCE):
            raise Refused("Bundle table inventory differs")
        frozen = json.loads(Path(__file__).with_name("schema_0031.json").read_text())
        if m["source_schema_fingerprints"] != frozen or m["target_only_policy"] != TARGET_ONLY:
            raise Refused("Bundle schema/policy differs")
        if m["publication_status_counts"] != {"PUBLISHED": 4, "CANCELLED": 5}:
            raise Refused("Publication history differs")
        snapshot = m["snapshot"]
        if "capture_mode" in snapshot:
            from .select_bridge import snapshot_valid
            if not snapshot_valid(snapshot):
                raise Refused("Source statement snapshot evidence differs")
        elif snapshot["read_only"] != "on" or snapshot["isolation"] != "repeatable read":
            raise Refused("Source transaction evidence differs")
        if len(m["dependency_order"]) != len(SOURCE) or set(m["dependency_order"]) != set(SOURCE):
            raise Refused("Dependency inventory differs")
        for name, rule in SOURCE.items():
            info = m["tables"][name]
            if any(not isinstance(info[k], str) or not re.fullmatch("[0-9a-f]{64}", info[k])
                   for k in ("content_sha256", "source_content_sha256")):
                raise Refused("Invalid content fingerprint")
            if info["source_count"] != rule.count or info["action"] != rule.action or info["category"] != rule.category:
                raise Refused("Bundle reviewed inventory differs")
            expected_count = rule.count if name in PRESERVED else 0
            if info["exported_count"] != expected_count or len(rows[name]) != expected_count:
                raise Refused("Bundle row count differs")
            if rows[name] != sorted(rows[name], key=canonical) or digest(rows[name]) != info["content_sha256"]:
                raise Refused("Bundle content fingerprint differs")
            if name in PRESERVED and info["source_content_sha256"] != info["content_sha256"]:
                raise Refused("Preserved source fingerprint differs")
            for row in rows[name]:
                if set(row) != {"values", "sql_nulls"} or not isinstance(row["values"], str):
                    raise Refused("Invalid row encoding")
                if row["sql_nulls"] != sorted(set(row["sql_nulls"])):
                    raise Refused("Invalid SQL null mask")
                if not isinstance(json.loads(row["values"]), dict):
                    raise Refused("Invalid row values")
        if m["media"] != media_manifest(rows):
            raise Refused("Media reference manifest differs")
        closed_bundle(rows)
        if "capture_mode" in snapshot:
            from .select_bridge import verify_bundle_capture
            verify_bundle_capture(bundle)
    except Refused:
        raise
    except Exception:
        raise Refused("Invalid transfer bundle") from None
    return m


def closed_bundle(rows):
    values = {name: [json.loads(r["values"]) for r in entries] for name, entries in rows.items()}
    if Counter(r["status"] for r in values["pin_publications"]) != {"PUBLISHED": 4, "CANCELLED": 5}:
        raise Refused("Bundle publication history is not closed")
    for name in AUTHORIZATION_TABLES:
        for r in values[name]:
            if r["status"] not in {"CONSUMED", "EXPIRED", "REVOKED"}:
                raise Refused("Bundle contains open authorization")
            if r["status"] == "CONSUMED" and r["consumed_at"] is None:
                raise Refused("Bundle contains inconsistent authorization")
            if r["status"] == "REVOKED" and r["revoked_at"] is None:
                raise Refused("Bundle contains inconsistent authorization")
    if len(values["routine_publishing_control"]) != 1 or values["routine_publishing_control"][0]["state"] != "PAUSED":
        raise Refused("Bundle control must be PAUSED")
    for name in ("routine_publishing_runs", "catalog_sync_jobs",
                 "pinterest_autonomous_generation_runs",
                 "pinterest_autonomous_execution_runs",
                 "pinterest_autonomous_destination_runs"):
        if any(r["status"] in {"RUNNING", "STARTED", "QUEUED", "PENDING"} for r in values[name]):
            raise Refused("Bundle contains unfinished runtime work")


def target_preflight(c):
    validate_catalog(c, "0034")
    for name in (*SOURCE, *TARGET_ONLY):
        if name == "routine_publishing_control":
            # 0031 creates this singleton even in a fresh database. Only the
            # untouched PAUSED scaffold is eligible for transactional replacement.
            values = c.execute(sa.text(
                "SELECT id,state,pause_reason,paused_at,paused_by,last_unknown_publication_id "
                "FROM public.routine_publishing_control"
            )).all()
            if values not in ([], [("default", "PAUSED", None, None, None, None)]):
                raise Refused("Target routine control is not an untouched PAUSED scaffold")
        elif count(c, name):
            raise Refused(f"Target must be empty: {name}")


def certify_connection(c, bundle):
    m = bundle["manifest"]
    validate_catalog(c, "0034")
    metadata, order, parents, refs = dependencies(c)
    if (order, parents, refs) != (
        m["dependency_order"], m["dependencies"], m["self_dependencies"]
    ):
        raise Refused("Dependency contract differs")
    validate_references(bundle["rows"], metadata)
    for name in SOURCE:
        actual = records(c, name, metadata.tables[f"public.{name}"])
        if len(actual) != m["tables"][name]["exported_count"] or digest(actual) != m["tables"][name]["content_sha256"]:
            raise Refused(f"Post-import content differs: {name}")
    for name in TARGET_ONLY:
        if count(c, name):
            raise Refused("Target-only execution evidence must remain empty")
    closed_state(c)
    return {"database_certification": "PASS", "schema_canonicality": "PASS",
            "manifest_sha256": m["manifest_sha256"],
            "media_certification": "NOT_PERFORMED", "provider_readiness": "NOT_PERFORMED"}


@safe_errors
def import_target(engine, bundle, expected_sha256, *, plan=False):
    m = verify_bundle(bundle, expected_sha256)
    with transaction(engine, readonly=plan) as c:
        if not plan:
            # Serialize imports and prevent app writes until certification/commit.
            # No trigger disabling, bookkeeping edits, runtime receipt or DDL.
            names = sorted(set(SOURCE) | set(TARGET_ONLY) | {"alembic_version"})
            c.execute(sa.text(
                "LOCK TABLE " + ",".join(f'public."{n}"' for n in names) +
                " IN ACCESS EXCLUSIVE MODE"
            ))
        target_preflight(c)
        metadata, order, parents, refs = dependencies(c)
        if (order, parents, refs) != (m["dependency_order"], m["dependencies"], m["self_dependencies"]):
            raise Refused("Dependency contract differs")
        # Explicit columns and exact JSON/SQL NULL distinction. All canonical
        # application IDs are varchar; no sequence/identity repair is necessary.
        for name in SOURCE:
            cols = metadata.tables[f"public.{name}"].columns
            for row in bundle["rows"][name]:
                values = json.loads(row["values"])
                if set(values) != set(cols.keys()) or not set(row["sql_nulls"]) <= set(cols.keys()):
                    raise Refused("Row columns differ")
                if any(values[k] is not None for k in row["sql_nulls"]):
                    raise Refused("SQL null mask differs")
        validate_references(bundle["rows"], metadata)
        if plan:
            return {**safe_plan(bundle), "target_preflight": "PASS", "writes": 0}
        c.execute(sa.text("DELETE FROM public.routine_publishing_control"))
        for name in order:
            if name not in PRESERVED:
                continue
            table = metadata.tables[f"public.{name}"]
            cols = sorted(col.name for col in table.columns)
            expressions = []
            for col in cols:
                if isinstance(table.c[col].type, sa.JSON):
                    raw = f"(CAST(:payload AS jsonb) -> '{col}')"
                    # JSON, not JSONB: preserve JSON literal null rather than SQL NULL.
                    raw += "::json" if table.c[col].type.__class__.__name__ == "JSON" else ""
                else:
                    raw = f'x."{col}"'
                expressions.append(
                    f'CASE WHEN \'{col}\'=ANY(CAST(:sql_nulls AS text[])) '
                    f'THEN NULL ELSE {raw} END'
                )
            statement = sa.text(
                f'INSERT INTO public."{name}" (' + ",".join(f'"{col}"' for col in cols) +
                ") SELECT " + ",".join(expressions) +
                f' FROM jsonb_populate_record(NULL::public."{name}",CAST(:payload AS jsonb)) x'
            )
            for row in ordered_rows(bundle["rows"][name], refs.get(name, [])):
                c.execute(statement, {"payload": row["values"], "sql_nulls": row["sql_nulls"]})
        return certify_connection(c, bundle)


@safe_errors
def certify_target(engine, bundle, expected_sha256):
    verify_bundle(bundle, expected_sha256)
    with transaction(engine) as c:
        return certify_connection(c, bundle)


def safe_plan(bundle):
    m = bundle["manifest"]
    return {
        "format": m["format"], "source_revision": m["source_revision"],
        "target_revision": m["target_revision"], "manifest_sha256": m["manifest_sha256"],
        "dependency_order": m["dependency_order"],
        "tables": {n: {k: v for k, v in info.items() if k in (
            "source_count", "exported_count", "content_sha256", "source_content_sha256",
            "action", "category"
        )} for n, info in m["tables"].items()},
        "media_certification": "NOT_PERFORMED", "provider_readiness": "NOT_PERFORMED",
    }


def write_bundle(bundle, path):
    """Create exclusively, owner-only; never overwrite or leave a partial export."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(canonical(bundle) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise