from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.session import SessionLocal
from app.models.domain import (
    CatalogSyncJob,
    Product,
    ProductImage,
    ProductIntelligence,
    ProductVariant,
    Store,
)
from app.services.product_intelligence import normalize_shopify_product
from app.services.product_intelligence import stream_bulk_products

logger = logging.getLogger(__name__)


class SyncAlreadyRunning(RuntimeError):
    pass


class SyncCoordinator:
    """Process-local guard; the database partial unique index covers other workers."""

    def __init__(self):
        self._lock = asyncio.Lock()

    async def claim(self) -> None:
        if self._lock.locked():
            raise SyncAlreadyRunning("A catalog sync is already running.")
        await self._lock.acquire()

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()


sync_coordinator = SyncCoordinator()


def _children(record: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = record.get(key, record.get(f"_{key}", []))
    if isinstance(value, dict):
        value = value.get("edges", value.get("nodes", []))
    return [
        item.get("node", item) if isinstance(item, dict) else item
        for item in (value or [])
    ]


def _money(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _shop_domain(value: str) -> str:
    return value.removeprefix("https://").removeprefix("http://").rstrip("/")


def _metafield_dict(fields: list[dict[str, Any]]) -> dict[str, str]:
    result = {}
    for field in fields:
        namespace = str(field.get("namespace", "")).strip()
        key = str(field.get("key", "")).strip()
        value = field.get("value")
        if key and value is not None:
            result[f"{namespace}.{key}".strip(".")] = str(value)
    return result


def _product_url(record: dict[str, Any], domain: str) -> str:
    return (
        str(record.get("onlineStoreUrl") or "").strip()
        or f"https://{domain}/products/{record.get('handle', '')}"
    )


def _persisted_product_record(
    product: Product,
    variants: list[ProductVariant],
    images: list[ProductImage],
) -> dict[str, Any]:
    """Rebuild normalizer input from local catalog facts without Shopify access."""
    shopify_data = product.shopify_data or {}
    return {
        "id": product.shopify_product_id,
        "handle": product.handle,
        "title": product.title,
        "vendor": product.vendor,
        "productType": product.product_type,
        "status": product.status,
        "onlineStoreUrl": product.product_url,
        "tags": product.tags or [],
        "collections": product.collections or [],
        "metafields": shopify_data.get("metafields", []),
        "totalInventory": product.inventory_total,
        "price": str(product.price_min) if product.price_min is not None else None,
        "createdAt": product.shopify_created_at.isoformat() if product.shopify_created_at else None,
        "updatedAt": product.shopify_updated_at.isoformat() if product.shopify_updated_at else None,
        "variants": [
            {
                "id": variant.shopify_variant_id,
                "sku": variant.sku,
                "title": variant.title,
                "price": str(variant.price) if variant.price is not None else None,
                "compareAtPrice": (
                    str(variant.compare_at_price)
                    if variant.compare_at_price is not None
                    else None
                ),
                "inventoryQuantity": variant.inventory_quantity,
            }
            for variant in variants
        ],
        "media": [
            {
                "id": image.shopify_media_id,
                "image": {
                    "url": image.source_url,
                    "altText": image.alt_text,
                    "width": image.width,
                    "height": image.height,
                },
            }
            for image in images
        ],
    }


def _apply_normalized_intelligence(
    db: Any,
    product: Product,
    normalized: Any,
) -> tuple[ProductIntelligence, bool]:
    intelligence = db.scalar(
        select(ProductIntelligence).where(ProductIntelligence.product_id == product.id)
    )
    if not intelligence:
        intelligence = ProductIntelligence(product_id=product.id)
        changed = True
    else:
        changed = False

    values = normalized.as_dict()
    model_columns = set(ProductIntelligence.__table__.columns.keys())
    for key, value in values.items():
        if key in model_columns and key not in {"id", "product_id"}:
            if getattr(intelligence, key) != value:
                setattr(intelligence, key, value)
                changed = True
    if intelligence.eligibility_reasons != normalized.eligibility_reasons:
        intelligence.eligibility_reasons = normalized.eligibility_reasons
        changed = True
    if intelligence.normalized_data != values:
        intelligence.normalized_data = values
        changed = True
    db.add(intelligence)
    db.flush()
    return intelligence, changed


class CatalogSyncService:
    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        settings: Settings | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings

    def create_job(self, shop_domain: str) -> CatalogSyncJob:
        domain = _shop_domain(shop_domain)
        db = self.session_factory()
        try:
            store = db.scalar(select(Store).where(Store.shop_domain == domain))
            if not store:
                store = Store(name=domain, shop_domain=domain)
                db.add(store)
                db.flush()
            active = db.scalar(
                select(CatalogSyncJob).where(
                    CatalogSyncJob.store_id == store.id,
                    CatalogSyncJob.status.in_(("QUEUED", "RUNNING")),
                )
            )
            if active:
                raise SyncAlreadyRunning("A catalog sync is already running.")
            job = CatalogSyncJob(store_id=store.id, status="QUEUED")
            db.add(job)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise SyncAlreadyRunning("A catalog sync is already running.") from exc
            db.refresh(job)
            return job
        finally:
            db.close()

    async def run(self, job_id: str, gateway: Any) -> None:
        await sync_coordinator.claim()
        db = self.session_factory()
        try:
            job = db.get(CatalogSyncJob, job_id)
            if not job:
                raise RuntimeError("Catalog sync job was not found.")
            job.status = "RUNNING"
            job.started_at = datetime.now(timezone.utc)
            db.commit()

            operation_id = await gateway.start_catalog_bulk_export()
            job.provider_operation_id = operation_id
            db.commit()
            operation = await self._wait_for_operation(gateway, operation_id)
            url = operation.get("url")
            if not url:
                raise RuntimeError("Shopify bulk export completed without a result URL.")

            index = 0
            async for raw_product in stream_bulk_products(
                gateway.download_bulk_result(url)
            ):
                index += 1
                job.total_seen = index
                try:
                    with db.begin_nested():
                        imported = self._upsert_product(db, raw_product, job.store_id)
                    if imported:
                        job.products_imported += 1
                    else:
                        job.products_updated += 1
                except Exception as exc:
                    job.products_failed += 1
                    job.last_error = f"Product import failed: {type(exc).__name__}: {exc}"
                    logger.exception("Shopify product import failed for sync job %s", job.id)
                if index % 50 == 0:
                    db.commit()

            job.status = "SUCCEEDED"
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
            logger.info(
                "Shopify catalog sync completed: job=%s seen=%s imported=%s updated=%s failed=%s",
                job.id, job.total_seen, job.products_imported,
                job.products_updated, job.products_failed,
            )
        except Exception as exc:
            db.rollback()
            job = db.get(CatalogSyncJob, job_id)
            if job:
                job.status = "FAILED"
                job.completed_at = datetime.now(timezone.utc)
                job.last_error = f"{type(exc).__name__}: {exc}"
                db.commit()
            logger.exception("Shopify catalog sync failed for job %s", job_id)
        finally:
            db.close()
            sync_coordinator.release()

    async def _wait_for_operation(self, gateway: Any, operation_id: str) -> dict[str, Any]:
        for _ in range(900):
            operation = await gateway.get_bulk_operation(operation_id)
            status = str(operation.get("status", "")).upper()
            if status == "COMPLETED":
                return operation
            if status in {"FAILED", "CANCELED", "CANCELLED", "EXPIRED"}:
                raise RuntimeError(
                    f"Shopify bulk export ended with status {status}: "
                    f"{operation.get('errorCode', 'unknown error')}"
                )
            await asyncio.sleep(2)
        raise TimeoutError("Shopify bulk export did not complete within 30 minutes.")

    def renormalize_existing_products(self, batch_size: int = 250) -> dict[str, int]:
        """Re-normalize local catalog rows without creating a sync job or API call."""
        db = self.session_factory()
        stats = {"seen": 0, "changed": 0, "unchanged": 0, "created": 0}
        try:
            products = list(db.scalars(select(Product).order_by(Product.id)))
            variants_by_product: dict[str, list[ProductVariant]] = defaultdict(list)
            images_by_product: dict[str, list[ProductImage]] = defaultdict(list)
            for variant in db.scalars(select(ProductVariant).order_by(ProductVariant.id)):
                variants_by_product[variant.product_id].append(variant)
            for image in db.scalars(
                select(ProductImage).order_by(ProductImage.is_primary.desc(), ProductImage.id)
            ):
                images_by_product[image.product_id].append(image)
            existing_ids = set(db.scalars(select(ProductIntelligence.product_id)))

            for product in products:
                stats["seen"] += 1
                record = _persisted_product_record(
                    product,
                    variants_by_product[product.id],
                    images_by_product[product.id],
                )
                normalized = normalize_shopify_product(record)
                _, changed = _apply_normalized_intelligence(db, product, normalized)
                if product.id not in existing_ids:
                    stats["created"] += 1
                if changed:
                    stats["changed"] += 1
                else:
                    stats["unchanged"] += 1
                if stats["seen"] % batch_size == 0:
                    db.commit()
            db.commit()
            return stats
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _upsert_product(
        self,
        db: Any,
        record: dict[str, Any],
        store_id: str,
    ) -> bool:
        shopify_id = str(record.get("id", "")).strip()
        if not shopify_id:
            raise ValueError("Shopify product is missing its ID.")
        existing = db.scalar(
            select(Product).where(
                Product.store_id == store_id,
                Product.shopify_product_id == shopify_id,
            )
        )
        product = existing or Product(store_id=store_id, shopify_product_id=shopify_id)
        variants = _children(record, "variants")
        images = _children(record, "media") or _children(record, "images")
        collections = _children(record, "collections")
        metafields = _children(record, "metafields")
        prices = [price for price in (_money(v.get("price")) for v in variants) if price is not None]
        compare_at_prices = [
            price for price in (_money(v.get("compareAtPrice")) for v in variants)
            if price is not None
        ]
        inventory_total = record.get("totalInventory")
        if inventory_total is None:
            inventory_total = sum(int(v.get("inventoryQuantity") or 0) for v in variants)

        product.handle = str(record.get("handle") or "")
        product.title = str(record.get("title") or "Untitled Shopify product")
        product.vendor = record.get("vendor")
        product.product_type = record.get("productType")
        product.status = str(record.get("status") or "UNKNOWN").upper()
        product.product_url = _product_url(record, db.get(Store, store_id).shop_domain)
        product.tags = list(record.get("tags") or [])
        product.collections = collections
        product.shopify_data = {"metafields": metafields}
        product.inventory_total = int(inventory_total or 0)
        product.price_min = min(prices) if prices else _money(record.get("price"))
        product.compare_at_min = min(compare_at_prices) if compare_at_prices else None
        product.shopify_created_at = _datetime(record.get("createdAt"))
        product.shopify_updated_at = _datetime(record.get("updatedAt"))
        product.synced_at = datetime.now(timezone.utc)
        db.add(product)
        db.flush()

        db.execute(delete(ProductVariant).where(ProductVariant.product_id == product.id))
        for variant in variants:
            variant_id = str(variant.get("id", "")).strip()
            if variant_id:
                db.add(ProductVariant(
                    product_id=product.id,
                    shopify_variant_id=variant_id,
                    sku=variant.get("sku"),
                    title=variant.get("title"),
                    price=_money(variant.get("price")),
                    compare_at_price=_money(variant.get("compareAtPrice")),
                    inventory_quantity=int(variant.get("inventoryQuantity") or 0),
                    available=int(variant.get("inventoryQuantity") or 0) > 0,
                ))

        db.execute(delete(ProductImage).where(ProductImage.product_id == product.id))
        for index, media in enumerate(images):
            image = media.get("image", media)
            url = str(image.get("url") or "").strip()
            if url:
                db.add(ProductImage(
                    product_id=product.id,
                    shopify_media_id=media.get("id"),
                    source_url=url,
                    alt_text=image.get("altText"),
                    width=image.get("width"),
                    height=image.get("height"),
                    is_primary=index == 0,
                    editorial_eligible=True,
                ))

        normalized = normalize_shopify_product(record)
        _apply_normalized_intelligence(db, product, normalized)
        return existing is None
