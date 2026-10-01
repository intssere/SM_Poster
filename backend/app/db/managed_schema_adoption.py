"""Bookkeeping-only adoption of managed schema pre-applied through 0031.

No DDL, providers, permits, publishing, or application processes are started.
An already-recorded 0032 schema is verified without mutation; 0031 is never
automatically migrated at startup. The separate canonicality guard is read-only.
"""
from __future__ import annotations

from app.db.migration_adoption import adopt_managed_preapplied_0031
from app.db.session import engine


def main() -> int:
    with engine.begin() as connection:
        adopt_managed_preapplied_0031(connection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())