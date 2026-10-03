---
name: PostgreSQL catalog versions
description: Cross-version frozen-schema verification must preserve enforcement while normalizing redundant catalog representation.
---

Reproduce a frozen-fingerprint failure on the relevant PostgreSQL major version before changing a migration or adoption allowlist. A successful clean upgrade on an older server is not evidence that a newer server has the same catalog representation.

**Why:** PostgreSQL 18 adds named NOT NULL entries to pg_constraint, while earlier servers represented nullability through column metadata. Hashing both representations can reject a genuinely canonical migration chain. The difference affects historical predecessor checks and later management-schema verification, not just the initially failing revision.

**How to apply:** Keep frozen hashes and exact historical drift allowlists unchanged. Normalize only redundant, validated, enforced NOT NULL catalog representation whose column nullability is already verified; refuse weaker enforcement, missing nullability, and unrelated catalog differences. Test fresh upgrades and historical repair/adoption paths across server major versions.