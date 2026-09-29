"""Bookkeeping-only adoption of a fully pre-applied managed 0031 schema.

No DDL, providers, permits, publishing, or application processes are started.
The separate schema canonicality guard remains read-only.
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