from __future__ import annotations

import hashlib

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.integrations.shopify.auth import (
    REQUIRED_SHOPIFY_SCOPES,
    ShopifyTokenProvider,
    normalize_shop_domain,
)
from app.integrations.shopify.gateway import ShopifyConfig, ShopifyGraphQLGateway
from app.models.domain import CatalogSyncJob, Product, ProductImage, ProductIntelligence, Store
from app.services.intelligence_reporting import build_intelligence_report
from app.services.shopify_sync import CatalogSyncService, SyncAlreadyRunning

router = APIRouter(prefix="/catalog", tags=["catalog"])
_cached_gateway: ShopifyGraphQLGateway | None = None
_cached_gateway_fingerprint: str | None = None


def _gateway(settings: Settings) -> ShopifyGraphQLGateway | None:
    global _cached_gateway, _cached_gateway_fingerprint

    domain = normalize_shop_domain(settings.shopify_shop)
    provider = ShopifyTokenProvider(
        shop_domain=domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        access_token=settings.shopify_access_token,
    )
    if not provider.authentication_method:
        return None
    fingerprint = hashlib.sha256(
        "\0".join([
            domain or "",
            settings.shopify_api_version,
            settings.shopify_client_id or "",
            settings.shopify_client_secret or "",
            settings.shopify_access_token or "",
        ]).encode()
    ).hexdigest()
    if _cached_gateway is None or _cached_gateway_fingerprint != fingerprint:
        _cached_gateway = ShopifyGraphQLGateway(ShopifyConfig(
            shop_domain=domain or "",
            api_version=settings.shopify_api_version,
            access_token=settings.shopify_access_token,
            client_id=settings.shopify_client_id,
            client_secret=settings.shopify_client_secret,
            token_provider=provider,
        ))
        _cached_gateway_fingerprint = fingerprint
    return _cached_gateway


async def _shopify_connection_payload(settings: Settings) -> dict:
    domain = normalize_shop_domain(settings.shopify_shop)
    provider = ShopifyTokenProvider(
        shop_domain=domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        access_token=settings.shopify_access_token,
    )
    gateway = _gateway(settings)
    if gateway:
        connection = await gateway.connection_status()
    else:
        connection = await provider.connection_status()
    payload = connection.as_dict()
    payload.update({
        "provider": "shopify",
        "shop_domain": domain,
        "api_version": settings.shopify_api_version,
        "missing": provider.missing_configuration(),
        "required_scopes": sorted(REQUIRED_SHOPIFY_SCOPES),
        "guidance": (
            "Configure SHOPIFY_SHOP with SHOPIFY_CLIENT_ID and "
            "SHOPIFY_CLIENT_SECRET, or use SHOPIFY_ACCESS_TOKEN as a fallback."
        ),
    })
    return payload


def _job_payload(job: CatalogSyncJob | None) -> dict:
    if not job:
        return {
            "status": "NOT_STARTED",
            "total_seen": 0,
            "products_imported": 0,
            "products_updated": 0,
            "products_failed": 0,
            "last_error": None,
        }
    return {
        "id": job.id,
        "status": job.status,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "total_seen": job.total_seen,
        "products_imported": job.products_imported,
        "products_updated": job.products_updated,
        "products_failed": job.products_failed,
        "last_error": job.last_error,
    }


@router.get("/shopify/status")
async def shopify_status(settings: Settings = Depends(get_settings)):
    payload = await _shopify_connection_payload(settings)
    db = SessionLocal()
    try:
        latest = db.scalar(select(CatalogSyncJob).order_by(CatalogSyncJob.created_at.desc()))
        latest_success = db.scalar(
            select(CatalogSyncJob)
            .where(CatalogSyncJob.status == "SUCCEEDED")
            .order_by(CatalogSyncJob.completed_at.desc())
        )
        payload["last_sync"] = _job_payload(latest)
        payload["last_successful_sync_at"] = (
            latest_success.completed_at if latest_success else None
        )
        return payload
    finally:
        db.close()


@router.get("/sync/status")
def sync_status():
    db = SessionLocal()
    try:
        latest = db.scalar(select(CatalogSyncJob).order_by(CatalogSyncJob.created_at.desc()))
        return _job_payload(latest)
    finally:
        db.close()


@router.get("/intelligence/summary")
def intelligence_summary():
    db = SessionLocal()
    try:
        return build_intelligence_report(db)
    finally:
        db.close()


@router.post("/sync", status_code=202)
async def start_catalog_sync(
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
):
    gateway = _gateway(settings)
    if gateway is None:
        return {
            "accepted": False,
            "status": "NOT_CONNECTED",
            "message": "Shopify not configured.",
        }
    connection = await gateway.connection_status()
    if not connection.connected:
        return {
            "accepted": False,
            "status": connection.status,
            "message": connection.message,
        }
    service = CatalogSyncService(settings=settings)
    try:
        job = service.create_job(normalize_shop_domain(settings.shopify_shop) or "")
    except SyncAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(service.run, job.id, gateway)
    return {
        "accepted": True,
        "status": job.status,
        "job_id": job.id,
        "message": "Shopify catalog sync queued.",
    }


def _decimal_value(value):
    return float(value) if value is not None else None


@router.get("/products")
def list_products(
    search: str | None = None,
    vendor: str | None = None,
    product_type: str | None = None,
    stock_status: str | None = Query(default=None, pattern="^(in_stock|out_of_stock)$"),
    eligibility: str | None = Query(default=None, pattern="^(eligible|ineligible)$"),
    normalization_status: str | None = Query(default=None, pattern="^(COMPLETE|PARTIAL|UNKNOWN)$"),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    db = SessionLocal()
    try:
        image_url = (
            select(ProductImage.source_url)
            .where(ProductImage.product_id == Product.id)
            .order_by(ProductImage.is_primary.desc(), ProductImage.id)
            .limit(1)
            .scalar_subquery()
        )
        filters = []
        if search:
            term = f"%{search.strip()}%"
            filters.append(or_(
                Product.title.ilike(term),
                Product.handle.ilike(term),
                Product.vendor.ilike(term),
            ))
        if vendor:
            filters.append(Product.vendor == vendor)
        if product_type:
            filters.append(Product.product_type == product_type)
        if stock_status == "in_stock":
            filters.append(Product.inventory_total > 0)
        elif stock_status == "out_of_stock":
            filters.append(Product.inventory_total <= 0)
        if eligibility == "eligible":
            filters.append(ProductIntelligence.eligibility_status == "ELIGIBLE")
        elif eligibility == "ineligible":
            filters.append(ProductIntelligence.eligibility_status != "ELIGIBLE")
        if normalization_status:
            filters.append(ProductIntelligence.normalization_status == normalization_status)
        if min_price is not None:
            filters.append(Product.price_min >= min_price)
        if max_price is not None:
            filters.append(Product.price_min <= max_price)

        base = (
            select(Product, ProductIntelligence, image_url.label("image_url"))
            .outerjoin(ProductIntelligence, ProductIntelligence.product_id == Product.id)
            .where(*filters)
            .order_by(Product.title.asc())
        )
        total = db.scalar(
            select(func.count(Product.id))
            .outerjoin(ProductIntelligence, ProductIntelligence.product_id == Product.id)
            .where(*filters)
        ) or 0
        rows = db.execute(base.offset(offset).limit(limit)).all()
        items = []
        for product, intelligence, image in rows:
            normalized = intelligence.normalized_data if intelligence else {}
            items.append({
                "id": product.id,
                "shopify_product_id": product.shopify_product_id,
                "title": product.title,
                "handle": product.handle,
                "product_url": product.product_url,
                "image_url": image,
                "vendor": product.vendor,
                "product_type": product.product_type,
                "status": product.status,
                "tags": product.tags or [],
                "collections": product.collections or [],
                "price": _decimal_value(product.price_min),
                "compare_at_price": _decimal_value(product.compare_at_min),
                "inventory_total": product.inventory_total,
                "inventory_status": "IN_STOCK" if product.inventory_total > 0 else "OUT_OF_STOCK",
                "eligibility_score": _decimal_value(intelligence.eligibility_score) if intelligence else 0,
                "eligibility_status": intelligence.eligibility_status if intelligence else "UNKNOWN",
                "eligibility_reasons": intelligence.eligibility_reasons if intelligence else [],
                "eligibility_positive_reasons": normalized.get(
                    "eligibility_positive_reasons", []
                ),
                "eligibility_blocking_reasons": normalized.get(
                    "eligibility_blocking_reasons", []
                ),
                "normalization_status": intelligence.normalization_status if intelligence else "UNKNOWN",
                "normalization_category": normalized.get("normalization_category", "other"),
                "normalization_required_fields": normalized.get("required_fields", []),
                "normalization_missing_fields": normalized.get("missing_required_fields", []),
                "qa_warnings": normalized.get("qa_warnings", []),
                "normalized": normalized,
                "synced_at": product.synced_at,
            })
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    finally:
        db.close()