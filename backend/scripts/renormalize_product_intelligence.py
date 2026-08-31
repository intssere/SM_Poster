"""Re-normalize ProductIntelligence from local catalog facts only."""

from __future__ import annotations

import json

from app.db.session import SessionLocal
from app.services.intelligence_reporting import build_intelligence_report
from app.services.shopify_sync import CatalogSyncService


def report(sample_limit: int = 0) -> dict:
    db = SessionLocal()
    try:
        return build_intelligence_report(db, sample_limit=sample_limit)
    finally:
        db.close()


def main() -> None:
    before = report()
    service = CatalogSyncService()
    first_pass = service.renormalize_existing_products()
    after = report(sample_limit=25)
    second_pass = service.renormalize_existing_products()
    print(json.dumps({
        "before": before,
        "first_pass": first_pass,
        "after": after,
        "idempotence_check": second_pass,
        "shopify_sync_called": False,
    }, indent=2))


if __name__ == "__main__":
    main()