---
name: Historical migration baseline
description: Why the initial schema migration must be fully frozen before later model changes.
---

The initial migration currently excludes the catalog-sync additions and has a verified clean upgrade path, but most older tables are still copied from live ORM metadata. Convert the complete Phase 0 schema to explicit historical declarations before making future changes to those older models.

**Why:** A migration that imports evolving ORM metadata can create future columns or tables too early, causing later revisions to fail on a clean database even when upgrades of existing databases work.

**How to apply:** Before adding another schema revision that modifies a pre-catalog model, first replace the remaining dynamic metadata copies in the initial revision with a complete immutable snapshot, then test both a clean PostgreSQL upgrade and an upgrade from the prior revision.