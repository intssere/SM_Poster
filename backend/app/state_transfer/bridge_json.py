"""Exact PostgreSQL JSONB wire encoding and Python-canonical SQL strings."""
from __future__ import annotations

import json


def pg_json(value):
    """JSONB text uses UTF-8 byte-length/key ordering and separator spaces.

    Capsule numbers are only catalog/count integers. Business JSON/numerics
    remain inside opaque values strings, never decoded/re-encoded here.
    """
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (len(k.encode("utf-8")), k.encode("utf-8")))
        return "{" + ", ".join(pg_json(k) + ": " + pg_json(value[k]) for k in keys) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(map(pg_json, value)) + "]"
    if value is not None and not isinstance(value, (str, bool, int)):
        raise ValueError("Noninteger capsule numeric value")
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def literal(value):
    # E strings are explicit: independent of standard_conforming_strings.
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def ascii_quote(expression):
    """SQL expression equivalent to json.dumps(text, ensure_ascii=True)."""
    ascii_pattern = literal(r"[^\x01-\x7e]")
    unicode_prefix = literal("\\u")
    return f"""(WITH quoted_input AS MATERIALIZED (SELECT {expression} AS value)
      SELECT CASE WHEN value !~ {ascii_pattern} THEN to_json(value)::text ELSE
      (SELECT '"' || COALESCE(string_agg(
      CASE WHEN ascii(ch)>65535 THEN
        {unicode_prefix} || lpad(to_hex(55296+(ascii(ch)-65536)/1024),4,'0') ||
        {unicode_prefix} || lpad(to_hex(56320+(ascii(ch)-65536)%1024),4,'0')
      WHEN ascii(ch)>126 THEN {unicode_prefix} || lpad(to_hex(ascii(ch)),4,'0')
      ELSE substr(to_json(ch)::text,2,length(to_json(ch)::text)-2) END,
      '' ORDER BY i),'') || '"'
      FROM generate_series(1,length(value)) i
      CROSS JOIN LATERAL (SELECT substr(value,i,1) ch) chars)
      END FROM quoted_input)"""


def strict_json(text):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Nonfinite JSON")

    return json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)