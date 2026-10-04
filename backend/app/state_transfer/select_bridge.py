"""One SELECT statement snapshot -> sensitive capsule -> offline v1 bundle.

No execution callback, DB URL, provider client, settings, crypto key or logging.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .bridge_catalog_sql import constraints_guard, descriptor
from .bridge_json import ascii_quote, literal, pg_json, strict_json
from .catalog import Refused, canonical, digest
from .policy import AUTHORIZATION_TABLES, FORMAT, PRESERVED, SOURCE, TARGET_ONLY

BRIDGE_FORMAT = "closed-state-single-select-v1"
SNAPSHOT_MODE = "single_statement_mvcc"
HASH_MARKER = "__QUERY_TEMPLATE_SHA256__"


def frozen():
    data = strict_json(Path(__file__).with_name("schema_bridge_0031.json").read_text())
    pins = json.loads(Path(__file__).with_name("schema_0031.json").read_text())
    if set(data["catalog"]) != set(SOURCE) or {
        n: digest(c) for n, c in data["catalog"].items()
    } != pins:
        raise Refused("Bridge frozen source catalog differs")
    return data


def sha_sql(expression):
    return f"encode(pg_catalog.sha256(convert_to({expression},'UTF8')),'hex')"


def qualify_builtins(sql):
    """Qualify code, never literals, so public overloads cannot replace helpers."""
    functions = (
        "ascii|substr|length|to_json|to_jsonb|generate_series|string_agg|lpad|to_hex|"
        "encode|convert_to|regexp_replace|btrim|left|right|jsonb_build_object|jsonb_build_array|"
        "jsonb_agg|jsonb_object_agg|count|array_to_json|array_remove|cardinality|to_regclass|"
        "pg_get_constraintdef|pg_get_indexdef|pg_get_expr|pg_get_triggerdef|pg_get_functiondef|"
        "current_setting|current_schemas|pg_current_snapshot|transaction_timestamp|statement_timestamp"
    )
    relations = ("pg_class|pg_namespace|pg_constraint|pg_attribute|pg_enum|pg_index|pg_am|"
                 "pg_trigger|pg_rewrite|pg_type|pg_cast|pg_proc")
    def code(text):
        text = re.sub(r"(?<![\w.])(" + functions + r")(?=\s*\()", r"pg_catalog.\1", text)
        text = re.sub(r"(?<![\w.])(" + relations + r")\b", r"pg_catalog.\1", text)
        # Domains from information_schema can otherwise select a public
        # exact-type operator overload. Do not replace arithmetic operators:
        # OPERATOR(...) has different arithmetic precedence. Their operands
        # are already primitive builtin integers, with pg_catalog first.
        return re.sub(r"(?<![-<>=!|])(\|\||!~|<>|=|>)(?![>=])",
                      r" OPERATOR(pg_catalog.\1) ", text)
    parts, start, i = [], 0, 0
    while i < len(sql):
        if sql[i] != "'":
            i += 1
            continue
        parts.append(code(sql[start:i]))
        quote_start = i
        escaped = i > 0 and sql[i - 1] == "E"
        i += 1
        while i < len(sql):
            if escaped and sql[i] == "\\":
                i += 2
            elif sql[i:i + 2] == "''":
                i += 2
            elif sql[i] == "'":
                i += 1
                break
            else:
                i += 1
        parts.append(sql[quote_start:i])
        start = i
    parts.append(code(sql[start:]))
    return "".join(parts)


def _sql():
    data = frozen()
    type_shapes = {}
    for n, table in data["catalog"].items():
        enum_columns = {name for name, _ in table.get("enum_labels", [])}
        type_shapes[n] = {
            c["name"]: {"name": c["udt"], "namespace": "public" if c["name"] in enum_columns else "pg_catalog",
                        "kind": "e" if c["name"] in enum_columns else "b", "generated": ""}
            for c in table["catalog"]["columns"]
        }
    type_shapes["alembic_version"] = {
        "version_num": {"name": "varchar", "namespace": "pg_catalog", "kind": "b", "generated": ""}}
    expected = ",\n".join(
        f"({literal(n)},{rule.count},{literal(canonical(data['catalog'][n]))}::jsonb)"
        for n, rule in sorted(SOURCE.items())
    )
    captures = []
    for n in sorted(SOURCE):
        columns = sorted(c["name"] for c in data["catalog"][n]["catalog"]["columns"])
        masks = ",".join(f"CASE WHEN t.\"{col}\" IS NULL THEN {literal(col)} END" for col in columns)
        null_json = "array_to_json(array_remove(ARRAY[" + masks + "],NULL))::text"
        # Column names are pinned ASCII identifiers, so array_to_json has the
        # same compact encoding as Python. values is escaped to Python ASCII.
        wire = f"""'{{"sql_nulls":' || {null_json} || ',"values":' ||
                   {ascii_quote('to_jsonb(t)::text')} || '}}'"""
        captures.append(f"""SELECT {literal(n)} AS name, count(*) AS source_count,
          '[' || COALESCE(string_agg(wire,',' ORDER BY wire COLLATE "C"),'') || ']' AS wire
          FROM catalog_gate cg CROSS JOIN LATERAL (
            SELECT {wire} AS wire FROM public."{n}" t WHERE cg.ok=1 OFFSET 0) r""")
    auth = " AND ".join(
        f"""NOT EXISTS(SELECT 1 FROM public."{n}" CROSS JOIN catalog_gate cg WHERE cg.ok=1 AND (
          status::text NOT IN ('CONSUMED','EXPIRED','REVOKED')
          OR (status::text='CONSUMED' AND consumed_at IS NULL)
          OR (status::text='REVOKED' AND revoked_at IS NULL)))"""
        for n in AUTHORIZATION_TABLES
    )
    work = " AND ".join(
        f"""NOT EXISTS(SELECT 1 FROM public."{n}" CROSS JOIN catalog_gate cg WHERE cg.ok=1 AND
          status::text IN ('RUNNING','STARTED','QUEUED','PENDING'))"""
        for n in ("routine_publishing_runs", "catalog_sync_jobs",
                  "pinterest_autonomous_generation_runs", "pinterest_autonomous_execution_runs",
                  "pinterest_autonomous_destination_runs")
    )
    names = literal(canonical(sorted((*SOURCE, "alembic_version")))) + "::jsonb"
    dep = {k: data[k] for k in ("dependency_order", "dependencies", "self_dependencies")}
    pins = {n: digest(c) for n, c in data["catalog"].items()}
    return qualify_builtins(f"""WITH expected(name,expected_count,expected_catalog) AS (VALUES {expected}),
type_shapes AS MATERIALIZED (
  SELECT c.relname name,jsonb_object_agg(a.attname,jsonb_build_object(
    'name',t.typname,'namespace',tn.nspname,'kind',t.typtype,'generated',a.attgenerated)) shape
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
  JOIN pg_type t ON t.oid=a.atttypid JOIN pg_namespace tn ON tn.oid=t.typnamespace
  WHERE n.nspname='public' AND (c.relname IN (SELECT name FROM expected) OR c.relname='alembic_version')
  GROUP BY c.relname
),
type_guard AS MATERIALIZED (SELECT
  (SELECT jsonb_object_agg(name,shape) FROM type_shapes)={literal(canonical(type_shapes))}::jsonb
  AND NOT EXISTS(SELECT 1 FROM pg_cast k JOIN pg_proc p ON p.oid=k.castfunc
    JOIN pg_namespace pn ON pn.oid=p.pronamespace
    WHERE pn.nspname<>'pg_catalog' AND k.castsource IN (
      SELECT a.atttypid FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
      AND (c.relname IN (SELECT name FROM expected) OR c.relname='alembic_version')
      AND a.attnum>0 AND NOT a.attisdropped))
  AS passed),
type_gate AS MATERIALIZED (SELECT 1 / CASE WHEN passed IS TRUE THEN 1 ELSE 0 END AS ok FROM type_guard),
catalogs AS MATERIALIZED (
  SELECT e.name,{descriptor()} AS actual FROM expected e CROSS JOIN type_gate tg WHERE tg.ok=1
),
catalog_guard AS MATERIALIZED (SELECT
  (SELECT jsonb_agg(version_num::text ORDER BY version_num::text)
    FROM public.alembic_version CROSS JOIN type_gate tg WHERE tg.ok=1)='["0031"]'::jsonb
  AND (SELECT jsonb_agg(c.relname ORDER BY c.relname COLLATE "C")
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S'))={names}
  AND NOT EXISTS(SELECT 1 FROM catalogs c JOIN expected e USING(name)
    WHERE c.actual IS DISTINCT FROM e.expected_catalog)
  AND {constraints_guard()}
  AND NOT EXISTS(SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public')
  AND current_setting('transaction_read_only')='on'
  AND current_setting('standard_conforming_strings')='on'
  AND (current_schemas(true))[1]='pg_catalog'
  AND current_setting('TimeZone') IN (
    'UTC','Etc/UTC','GMT','Etc/GMT','UCT','Etc/UCT','Universal','Etc/Universal','Zulu','Etc/Zulu'
  ) AS passed),
catalog_gate AS MATERIALIZED (SELECT 1 / CASE WHEN passed IS TRUE THEN 1 ELSE 0 END AS ok FROM catalog_guard),
captures AS MATERIALIZED ({' UNION ALL '.join(captures)}),
publications AS MATERIALIZED (
  SELECT COALESCE(jsonb_object_agg(status,n),'{{}}'::jsonb) counts FROM (
    SELECT status::text status,count(*) n FROM public.pin_publications
    CROSS JOIN catalog_gate cg WHERE cg.ok=1 GROUP BY status::text) p
),
guards AS MATERIALIZED (SELECT cg.ok=1
  AND NOT EXISTS(SELECT 1 FROM captures c JOIN expected e USING(name)
    WHERE c.source_count<>e.expected_count)
  AND (SELECT counts FROM publications)='{{"PUBLISHED":4,"CANCELLED":5}}'::jsonb
  AND {auth} AND {work}
  AND (SELECT count(*) FROM public.routine_publishing_control CROSS JOIN catalog_gate cg WHERE cg.ok=1)=1
  AND (SELECT count(*) FROM public.routine_publishing_control CROSS JOIN catalog_gate cg
       WHERE cg.ok=1 AND state::text='PAUSED')=1 AS passed FROM catalog_gate cg),
gate AS MATERIALIZED (SELECT 1 / CASE WHEN passed IS TRUE THEN 1 ELSE 0 END AS ok FROM guards),
payload AS MATERIALIZED (SELECT jsonb_build_object(
  'source_revision','0031','bridge_format',{literal(BRIDGE_FORMAT)},
  'catalog',(SELECT jsonb_object_agg(name,actual) FROM catalogs),
  'source_schema_fingerprints',{literal(canonical(pins))}::jsonb,
  'dependency_metadata',{literal(canonical(dep))}::jsonb,
  'snapshot',jsonb_build_object(
    'postgres_version',current_setting('server_version'),
    'postgres_version_num',current_setting('server_version_num'),
    'transaction_snapshot',pg_current_snapshot()::text,
    'transaction_time',transaction_timestamp()::text,
    'statement_time',statement_timestamp()::text,
    'isolation',current_setting('transaction_isolation'),
    'read_only',current_setting('transaction_read_only'),
    'capture_mode',{literal(SNAPSHOT_MODE)},'statement_count',1,
    'query_template_sha256',{literal(HASH_MARKER)}),
  'publication_status_counts',(SELECT counts FROM publications),
  'tables',(SELECT jsonb_object_agg(name,jsonb_build_object(
    'source_count',source_count,'source_content_sha256',{sha_sql('wire')},
    'rows',CASE WHEN name='pinterest_oauth_states' THEN '[]'::jsonb ELSE wire::jsonb END,
    'content_sha256',{sha_sql("CASE WHEN name='pinterest_oauth_states' THEN '[]' ELSE wire END")}))
    FROM captures)) AS value FROM gate WHERE gate.ok=1)
SELECT jsonb_build_object('payload',value,'capsule_sha256',{sha_sql('value::text')}) AS capsule
FROM payload""")


def source_sql():
    template = _sql()
    return template.replace(HASH_MARKER, hashlib.sha256(template.encode()).hexdigest())


def snapshot_valid(snapshot):
    expected = hashlib.sha256(_sql().encode()).hexdigest()
    return (
        snapshot.get("capture_mode") == SNAPSHOT_MODE
        and type(snapshot.get("statement_count")) is int and snapshot["statement_count"] == 1
        and snapshot.get("read_only") == "on"
        and snapshot.get("isolation") in {"read committed", "repeatable read", "serializable"}
        and snapshot.get("query_template_sha256") == expected
        and all(isinstance(snapshot.get(k), str) and snapshot[k] for k in (
            "postgres_version", "postgres_version_num", "transaction_snapshot",
            "transaction_time", "statement_time"))
    )


def verify_bundle_capture(bundle):
    """Bind ordinary bundle rows/evidence back to the original SQL capsule."""
    m = bundle["manifest"]
    data = frozen()
    p = {
        "source_revision": m["source_revision"], "bridge_format": BRIDGE_FORMAT,
        "catalog": data["catalog"], "source_schema_fingerprints": m["source_schema_fingerprints"],
        "dependency_metadata": {k: m[k] for k in (
            "dependency_order", "dependencies", "self_dependencies")},
        "snapshot": m["snapshot"], "publication_status_counts": m["publication_status_counts"],
        "tables": {n: {
            "source_count": m["tables"][n]["source_count"],
            "source_content_sha256": m["tables"][n]["source_content_sha256"],
            "content_sha256": m["tables"][n]["content_sha256"], "rows": bundle["rows"][n],
        } for n in SOURCE},
    }
    sha = m.get("source_capsule_sha256")
    if not isinstance(sha, str) or hashlib.sha256(pg_json(p).encode()).hexdigest() != sha:
        raise Refused("Source capsule evidence differs")


def verify_source_references(rows, data):
    values = {n: [strict_json(r["values"]) for r in records] for n, records in rows.items()}
    for n in SOURCE:
        ids = [r["id"] for r in values[n]]  # All pinned application PKs are varchar id.
        if any(not isinstance(v, str) or not v for v in ids) or len(ids) != len(set(ids)):
            raise ValueError()
        for constraint in data["catalog"][n]["catalog"]["constraints"]:
            if constraint["kind"] != "f":
                continue
            match = re.fullmatch(
                r"FOREIGN KEY \((.*?)\) REFERENCES ([a-z_]+)\((.*?)\).*", constraint["definition"])
            if match is None:
                raise ValueError()
            local, parent, remote = match.groups()
            local, remote = local.split(", "), remote.split(", ")
            keys = {tuple(r[k] for k in remote) for r in values[parent]}
            for row in values[n]:
                key = tuple(row[k] for k in local)
                if not any(v is None for v in key) and key not in keys:
                    raise ValueError()


def wrap_source_result(result, expected_capsule_sha256=None):
    """Validate privately retrieved one-row JSON, then create ordinary v1 bundle."""
    from .transfer import closed_bundle, media_manifest, verify_bundle
    try:
        if isinstance(result, dict) and set(result) == {"success", "output", "exitCode", "exitReason"}:
            if (result["success"] is not True or type(result["exitCode"]) is not int
                    or result["exitCode"] != 0 or result["exitReason"] is not None
                    or not isinstance(result["output"], str)):
                raise ValueError()
            result = strict_json(result["output"])
        if isinstance(result, list):
            if len(result) != 1:
                raise ValueError()
            result = result[0]
        if isinstance(result, dict) and set(result) == {"capsule"}:
            result = result["capsule"]
        if isinstance(result, str):
            result = strict_json(result)
        if not isinstance(result, dict) or set(result) != {"payload", "capsule_sha256"}:
            raise ValueError()
        p, sha = result["payload"], result["capsule_sha256"]
        if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
            raise ValueError()
        if hashlib.sha256(pg_json(p).encode()).hexdigest() != sha:
            raise ValueError()
        if expected_capsule_sha256 is not None and sha != expected_capsule_sha256:
            raise ValueError()
        data = frozen()
        pins = {n: digest(c) for n, c in data["catalog"].items()}
        if (set(p) != {"source_revision", "bridge_format", "catalog", "source_schema_fingerprints",
                       "dependency_metadata", "snapshot", "publication_status_counts", "tables"}
                or p["bridge_format"] != BRIDGE_FORMAT or p["source_revision"] != "0031"
                or p["catalog"] != data["catalog"] or p["source_schema_fingerprints"] != pins
                or p["dependency_metadata"] != {k: data[k] for k in (
                    "dependency_order", "dependencies", "self_dependencies")}
                or not snapshot_valid(p["snapshot"]) or set(p["tables"]) != set(SOURCE)
                or p["publication_status_counts"] != {"PUBLISHED": 4, "CANCELLED": 5}):
            raise ValueError()
        rows, infos = {}, {}
        for n, rule in SOURCE.items():
            table = p["tables"][n]
            if set(table) != {"rows", "source_count", "content_sha256", "source_content_sha256"}:
                raise ValueError()
            if type(table["source_count"]) is not int or table["source_count"] != rule.count:
                raise ValueError()
            rows[n] = table["rows"]
            columns = {c["name"] for c in data["catalog"][n]["catalog"]["columns"]}
            for row in rows[n]:
                values = strict_json(row["values"])
                if set(values) != columns or not set(row["sql_nulls"]) <= columns:
                    raise ValueError()
                if any(values[k] is not None for k in row["sql_nulls"]):
                    raise ValueError()
            infos[n] = {k: table[k] for k in (
                "source_count", "content_sha256", "source_content_sha256")}
            infos[n].update(exported_count=len(rows[n]), action=rule.action, category=rule.category)
        closed_bundle(rows)
        verify_source_references(rows, data)
        manifest = {
            "format": FORMAT, "source_revision": "0031", "target_revision": "0034",
            "snapshot": p["snapshot"], **p["dependency_metadata"],
            "source_schema_fingerprints": pins, "tables": infos,
            "publication_status_counts": p["publication_status_counts"],
            "target_only_policy": TARGET_ONLY, "media": media_manifest(rows),
            "source_capsule_sha256": sha,
        }
        manifest["manifest_sha256"] = digest(manifest)
        bundle = {"manifest": manifest, "rows": rows}
        verify_bundle(bundle, manifest["manifest_sha256"])
        return bundle
    except Exception:
        # Includes JSON/driver-shaped errors: never quote untrusted input.
        raise Refused("Source capsule verification refused") from None