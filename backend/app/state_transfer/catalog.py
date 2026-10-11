"""Pinned catalog validation and deterministic FK ordering. No application settings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import sqlalchemy as sa

from app.db.migration_adoption import _catalog_contract, verify_frozen_schema_at_head
from .policy import FOUNDATION_FKS, SOURCE, TARGET_ONLY


class Refused(RuntimeError):
    """Safe diagnostic only: never attach a driver error, row or parameter."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def contract(connection, name):
    security = connection.execute(sa.text(
        "SELECT c.relkind, c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname=:name"
    ), {"name": name}).mappings().one()
    triggers = connection.execute(sa.text(
        "SELECT t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) AS definition,"
        "pg_get_functiondef(t.tgfoid) AS function_definition "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname=:name AND NOT t.tgisinternal "
        "ORDER BY t.tgname"
    ), {"name": name}).mappings().all()
    result = {"catalog": _catalog_contract(connection, name),
              "security": dict(security), "triggers": [dict(r) for r in triggers]}
    enums = connection.execute(sa.text(
        "SELECT a.attname,e.enumlabel FROM pg_attribute a "
        "JOIN pg_enum e ON e.enumtypid=a.atttypid "
        "WHERE a.attrelid=CAST(:relation AS regclass) AND NOT a.attisdropped "
        "ORDER BY a.attname,e.enumsortorder"
    ), {"relation": f"public.{name}"}).all()
    if enums:
        result["enum_labels"] = [list(r) for r in enums]
    return result


def validate_catalog(connection, revision):
    if connection.dialect.name != "postgresql":
        raise Refused("PostgreSQL required")
    if revision not in {"0031", "0034", "0035"}:
        raise Refused("Unsupported revision")
    try:
        verify_frozen_schema_at_head(connection, revision=revision)
        present = set(connection.execute(sa.text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S')"
        )).scalars())
        expected = set(SOURCE) | {"alembic_version"}
        if revision in {"0034", "0035"}:
            expected |= set(TARGET_ONLY)
        if revision == "0035":
            expected.add("routine_one_shot_preparation_operations")
        if present != expected:
            raise Refused("Public table/relation inventory differs")
        frozen = json.loads(Path(__file__).with_name("schema_0031.json").read_text())
        if set(frozen) != set(SOURCE):
            raise Refused("Frozen contract inventory differs")
        if revision in {"0034", "0035"}:
            frozen.update(json.loads(
                Path(__file__).with_name("schema_0034_foundation.json").read_text()))
        for name in SOURCE:
            if digest(contract(connection, name)) != frozen[name]:
                raise Refused(f"Table contract differs: {name}")
        # No hidden writes in rules, and no external FK dependencies.
        if connection.scalar(sa.text(
            "SELECT count(*) FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'"
        )):
            raise Refused("Unexpected public rewrite rules")
    except Refused:
        raise
    except Exception:
        raise Refused("Schema canonicality refused") from None
    return frozen


def dependencies(connection):
    metadata = sa.MetaData()
    metadata.reflect(connection, schema="public", only=list(SOURCE))
    parents = {name: set() for name in SOURCE}
    self_refs = {}
    for name in SOURCE:
        table = metadata.tables[f"public.{name}"]
        for fk in table.foreign_key_constraints:
            remote = fk.referred_table
            if remote.schema != "public" or remote.name not in SOURCE:
                raise Refused("External foreign-key dependency")
            if remote.name == name:
                self_refs.setdefault(name, []).append(
                    [[e.parent.name, e.column.name] for e in fk.elements])
            else:
                parents[name].add(remote.name)
    for child, _, parent, _, _ in FOUNDATION_FKS:
        parents[child].add(parent)
    order, pending = [], dict(parents)
    while pending:
        ready = sorted(n for n, deps in pending.items() if not deps & pending.keys())
        if not ready:
            raise Refused("Cyclic table dependency")
        order.extend(ready)
        for name in ready:
            del pending[name]
    return metadata, order, {n: sorted(p) for n, p in parents.items()}, self_refs


def validate_references(rows, metadata):
    """Validate full source integrity, including FKs absent from canonical target."""
    values = {name: [json.loads(r["values"]) for r in entries] for name, entries in rows.items()}
    references = []
    for name in SOURCE:
        for fk in metadata.tables[f"public.{name}"].foreign_key_constraints:
            references.append((name, fk.referred_table.name,
                               [e.parent.name for e in fk.elements],
                               [e.column.name for e in fk.elements]))
    references.extend((child, parent, [column], ["id"])
                      for child, column, parent, _, _ in FOUNDATION_FKS)
    for child, parent, local, remote in references:
        parent_keys = {tuple(v[k] for k in remote) for v in values[parent]}
        for row in values[child]:
            key = tuple(row[k] for k in local)
            if not any(v is None for v in key) and key not in parent_keys:
                raise Refused("Bundle foreign-key reference missing")


def ordered_rows(records, refs):
    """Parent-before-child ordering for self FKs without disabling constraints."""
    remaining = {canonical(r): r for r in records}
    if len(remaining) != len(records):
        raise Refused("Duplicate records")
    rows = []
    values = {key: json.loads(row["values"]) for key, row in remaining.items()}
    while remaining:
        ready = []
        for key in sorted(remaining):
            child = values[key]
            blocked = False
            for pairs in refs:
                wanted = tuple(child[a] for a, _ in pairs)
                if any(v is None for v in wanted):
                    continue
                for other in remaining:
                    if other != key and tuple(values[other][b] for _, b in pairs) == wanted:
                        blocked = True
                        break
                if blocked:
                    break
            if not blocked:
                ready.append(key)
        if not ready:
            raise Refused("Cyclic self-reference")
        for key in ready:
            rows.append(remaining.pop(key))
    return rows