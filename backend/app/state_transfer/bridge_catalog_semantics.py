"""Narrow SELECT-only catalog equivalences; frozen evidence remains unchanged.

Only enumerated cast renderings and names of structurally matched UNIQUE
constraints/backing indexes are canonicalized. No live SQL text is parsed,
executed, stripped of casts, reordered, or generally regex-normalized.
"""
from __future__ import annotations

import re

from .bridge_catalog_sql import descriptor
from .bridge_json import literal


def cast_renderings(definition):
    """Derive the proven alternative from trusted pinned text, not live text."""
    array = r"ARRAY\[('[A-Z_]+'::character varying(?:, '[A-Z_]+'::character varying)*)\]"
    check = re.fullmatch(r"CHECK \(([a-z_]+)::text = ANY \(" + array + r"::text\[\]\)\)", definition)
    if check:
        column, elements = check.groups()
        rendered = elements.replace("::character varying", "::character varying::text")
        return [f"CHECK ({column}::text = ANY (ARRAY[{rendered}]))"]
    partial = re.fullmatch(
        r"\(\(status\)::text = ANY \(\(" + array + r"\)::text\[\]\)\)", definition)
    if partial:
        # Each element, not the complete array, receives the same text cast.
        rendered = ", ".join(f"({element})::text" for element in partial[1].split(", "))
        return [f"((status)::text = ANY (ARRAY[{rendered}]))"]
    return []


def _case(expression, pairs):
    cases = " ".join(
        f"WHEN {literal(alternate)} THEN {literal(expected)}"
        for alternate, expected in sorted(set(pairs)))
    return f"CASE ({expression}) {cases} ELSE ({expression}) END" if cases else expression


def semantic_descriptor(catalog):
    """Return pinned-shaped evidence only after exact structural matching.

    e.name and e.expected_catalog are the bridge's fixed expected relation.
    Ambiguous matches preserve the unmatched live object and fail comparison.
    Arrays retain multiplicity and are sorted only by their canonical names.
    """
    check_pairs, predicate_pairs, index_pairs = [], [], []
    for table in catalog.values():
        for constraint in table["catalog"]["constraints"]:
            if constraint["kind"] == "c":
                check_pairs.extend((s, constraint["definition"])
                                   for s in cast_renderings(constraint["definition"]))
    partial = next(i for i in catalog["catalog_sync_jobs"]["catalog"]["indexes"]
                   if i["name"] == "uq_catalog_sync_active")
    for alternate in cast_renderings(partial["predicate"]):
        predicate_pairs.append((alternate, partial["predicate"]))
        index_pairs.append((partial["definition"].replace(partial["predicate"], alternate),
                            partial["definition"]))
    check_definition = _case("a.value->>'definition'", check_pairs)
    predicate = _case("a.value->>'predicate'", predicate_pairs)
    index_definition = _case("a.value->>'definition'", index_pairs)
    # Every other descriptor field survives untouched. Enforcement is checked
    # separately by the existing constraints_guard, including PostgreSQL 18.
    return f"""(
      WITH raw AS MATERIALIZED (SELECT {descriptor()} value),
      constraint_inputs AS MATERIALIZED (
        SELECT CASE WHEN (a.value->>'kind')='c' THEN
          jsonb_set(a.value,'{{definition}}',to_jsonb({check_definition}))
          ELSE a.value END value
        FROM raw,jsonb_array_elements(raw.value->'catalog'->'constraints') a
      ),
      constraints AS MATERIALIZED (
        SELECT COALESCE(jsonb_agg(mapped.value ORDER BY mapped.value->>'name' COLLATE "C"),
                        '[]'::jsonb) value
        FROM constraint_inputs a CROSS JOIN LATERAL (
          SELECT CASE WHEN count(*)=1 THEN jsonb_agg(b.value)->0 ELSE a.value END value
          FROM jsonb_array_elements(e.expected_catalog->'catalog'->'constraints') b
          WHERE (a.value->>'kind')='u' AND (b.value->>'kind')='u'
            AND (a.value-'name')=(b.value-'name')
        ) mapped
      ),
      index_inputs AS MATERIALIZED (
        SELECT CASE WHEN e.name='catalog_sync_jobs'
          AND (a.value->>'name')='uq_catalog_sync_active' THEN
          jsonb_set(jsonb_set(a.value,'{{definition}}',to_jsonb({index_definition})),
                    '{{predicate}}',COALESCE(to_jsonb({predicate}),'null'::jsonb))
          ELSE a.value END value
        FROM raw,jsonb_array_elements(raw.value->'catalog'->'indexes') a
      ),
      indexes AS MATERIALIZED (
        SELECT COALESCE(jsonb_agg(mapped.value ORDER BY mapped.value->>'name' COLLATE "C"),
                        '[]'::jsonb) value
        FROM index_inputs a CROSS JOIN LATERAL (
          SELECT CASE WHEN count(*)=1 THEN jsonb_agg(b.value)->0 ELSE a.value END value
          FROM jsonb_array_elements(e.expected_catalog->'catalog'->'indexes') b
          WHERE (a.value-'name'-'definition')=(b.value-'name'-'definition')
            AND (a.value->>'definition')=('CREATE UNIQUE INDEX ' ||
                quote_ident(a.value->>'name') ||
                substr(b.value->>'definition',
                       length('CREATE UNIQUE INDEX ' || quote_ident(b.value->>'name'))+1))
            AND (b.value->>'definition') LIKE 'CREATE UNIQUE INDEX %'
            AND EXISTS (
              SELECT 1 FROM pg_constraint c
              CROSS JOIN LATERAL jsonb_array_elements(
                e.expected_catalog->'catalog'->'constraints') expected_unique
              WHERE c.conrelid=to_regclass('public.'||e.name) AND c.contype='u'
                AND c.conindid=to_regclass('public.'||quote_ident(a.value->>'name'))
                AND (expected_unique.value->>'name')=(b.value->>'name')
                AND EXISTS (SELECT 1 FROM constraint_inputs original
                  WHERE (original.value->>'name')=c.conname
                    AND (original.value-'name')=(expected_unique.value-'name'))
            )
        ) mapped
      )
      SELECT jsonb_set(jsonb_set(raw.value,'{{catalog,constraints}}',constraints.value),
                                '{{catalog,indexes}}',indexes.value)
      FROM raw,constraints,indexes
    )"""