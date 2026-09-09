"""Manual one-time migration of legacy media into App Storage.

This module is intentionally not imported by application startup.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import AIGeneratedAsset, PinCreative
from app.services.media_storage import PNGMediaStorage, StorageCorrupt, StorageUnavailable


def backfill(session, *, creative_storage=None, asset_storage=None, root: Path | None = None) -> dict[str, int]:
    root = root or Path(__file__).resolve().parents[2]
    creative_storage = creative_storage or PNGMediaStorage("creative")
    asset_storage = asset_storage or PNGMediaStorage("ai-asset")
    counts = {"creative_uploaded": 0, "asset_uploaded": 0, "skipped": 0, "missing": 0, "corrupt": 0}

    for row in session.scalars(select(PinCreative)).all():
        if row.render_status != "RENDERED" or not row.sha256:
            continue
        key = creative_storage.key(row.id, row.sha256)
        if creative_storage.backend.exists(key):
            creative_storage.read(row.id, row.sha256)
            counts["skipped"] += 1
            continue
        source = root / "generated-creatives" / f"{row.id}.png"
        if not source.is_file():
            counts["missing"] += 1
            continue
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != row.sha256:
            counts["corrupt"] += 1
            continue
        creative_storage.write(row.id, data)
        counts["creative_uploaded"] += 1

    for row in session.scalars(select(AIGeneratedAsset)).all():
        if not row.sha256:
            continue
        key = asset_storage.key(row.id, row.sha256)
        if asset_storage.backend.exists(key):
            asset_storage.read(row.id, row.sha256)
            row.storage_path = key
            counts["skipped"] += 1
            continue
        source = Path(row.storage_path) if row.storage_path else root / "generated-ai-assets" / f"{row.id}.png"
        if not source.is_file():
            counts["missing"] += 1
            continue
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != row.sha256:
            counts["corrupt"] += 1
            continue
        asset_storage.write(row.id, data)
        row.storage_path = key
        counts["asset_uploaded"] += 1
    session.commit()
    return counts


def main() -> None:
    with SessionLocal() as session:
        print(backfill(session))


if __name__ == "__main__":
    main()