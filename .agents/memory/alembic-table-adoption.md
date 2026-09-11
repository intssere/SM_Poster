---
name: Alembic table adoption
description: Fail-closed rule for migrations when ORM metadata has already created a pending table.
---

A migration may encounter its target table before the Alembic revision has advanced. Adopt that table only after validating every security-relevant schema property: column types and nullability, defaults, primary and foreign keys, checks, indexes, uniqueness, and partial-index predicates.

**Why:** ORM metadata initialization can create a newly modeled table before its migration runs. Checking only that a table or its column names exist can silently accept a malformed control-plane schema.

**How to apply:** Keep the normal fresh-create path. For a pre-existing target, compare the full dialect-reflected contract and fail on any mismatch. Add adversarial adoption tests, including partial predicates and timestamp defaults, for each supported dialect.