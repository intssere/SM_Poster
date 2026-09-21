---
name: Alembic table adoption
description: Fail-closed rule for migrations when ORM metadata has already created a pending table.
---

A migration may encounter its target table before the Alembic revision has advanced. Adopt that table only after validating every security-relevant schema property: column types and nullability, defaults, primary and foreign keys, checks, indexes, uniqueness, and partial-index predicates. For multi-table revisions, require all-or-none presence. Lock every adopted table against concurrent DML/DDL through Alembic's bookkeeping commit, and require every adopted table to be empty.

**Why:** ORM metadata initialization can create newly modeled tables before their migration runs. Checking only existence can silently accept malformed or partial control-plane schema, while validating without locks permits rows or drift to appear before Alembic advances its revision.

**How to apply:** Keep the normal fresh-create path unchanged. For pre-existing targets, use frozen revision-specific contracts, fail closed on unsupported backends or any mismatch, and test partial presence, structural drift, non-empty tables, and concurrent-mutation exclusion.