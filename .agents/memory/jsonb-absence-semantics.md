---
name: JSONB absence semantics
description: Why absent evidence must distinguish SQL NULL from JSON literal null.
---

Use SQL NULL for absent JSON evidence when database guards distinguish absence
from a JSON value. Do not assume Python `None` necessarily produces SQL NULL.

**Why:** SQLAlchemy JSONB defaults can serialize Python `None` as JSON literal
`null`. Guards accepting absent evidence but requiring any present evidence to
be an object then reject a legitimate unknown outcome.

**How to apply:** Specify SQL-null serialization or bind SQL NULL explicitly.
Test absent evidence against real PostgreSQL and assert `column IS NULL`; mocked
stores and Python readback alone do not establish the database representation.