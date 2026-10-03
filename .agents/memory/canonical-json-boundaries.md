---
name: Canonical JSON boundaries
description: PostgreSQL JSONB text and Python ASCII-canonical JSON are different encodings, including DEL and Unicode handling.
---

Treat PostgreSQL JSONB wire text and Python ASCII-canonical JSON as separate
explicit codecs, not interchangeable string serialization.

**Why:** PostgreSQL retains DEL and non-ASCII Unicode in JSON strings, while
Python's ensure_ascii encoding escapes DEL as well as non-ASCII characters
and uses surrogate pairs above the BMP. JSONB also orders object keys by UTF-8
byte length and then bytes, unlike Python's lexical sort. An ASCII fast path
ending at 127 breaks digest equivalence; repeated full-record JSON conversion
inside a character loop can make a bounded inventory export impractically slow.

**How to apply:** Test DEL, BMP/non-BMP Unicode, control characters and empty text
against both codecs. Materialize each original rendered string once and use a
fast path only where encodings truly agree. Keep business numeric JSON inside
opaque row strings rather than rounding it through a Python float serializer.