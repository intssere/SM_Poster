import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.domain import (
    Product,
    ProductImage,
    ProductIntelligence,
    ProductVariant,
    Store,
)
from app.services.product_intelligence import (
    CATEGORY_BATH_BODY,
    CATEGORY_BEAUTY,
    CATEGORY_FRAGRANCE,
    CATEGORY_GIFT_SET,
    CATEGORY_HOME_FRAGRANCE,
    assemble_bulk_products,
    normalize_shopify_product,
    stream_bulk_products,
)
from app.services.shopify_sync import CatalogSyncService, SyncAlreadyRunning


@pytest.fixture
def catalog_filter_client(monkeypatch):
    from app.api.routes import catalog

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(catalog, "SessionLocal", factory)

    def forbidden(*args, **kwargs):
        pytest.fail("Catalog filters must not invoke Shopify or synchronization")

    for name in ("_gateway", "ShopifyGraphQLGateway", "ShopifyTokenProvider", "CatalogSyncService"):
        monkeypatch.setattr(catalog, name, forbidden)
    with factory() as db:
        store = Store(name="Filter test", shop_domain="filters.example.test")
        db.add(store)
        db.flush()
        rows = [
            ("Adidas", "Fragrance", 8, 25, "ELIGIBLE", "COMPLETE"),
            (" adidas ", " fragrance ", 0, 80, "INELIGIBLE", "PARTIAL"),
            ("Other", "Hair Tools", 3, 30, "ELIGIBLE", "COMPLETE"),
            (None, None, 0, 0, "INELIGIBLE", "UNKNOWN"),
            ("", "", 0, 0, "INELIGIBLE", "UNKNOWN"),
            (" \t\n", " \t\n", 0, 0, "INELIGIBLE", "UNKNOWN"),
            ("A_%", "Type_%", 1, 10, "ELIGIBLE", "COMPLETE"),
        ]
        for index, (vendor, kind, stock, price, eligibility, normalization) in enumerate(rows):
            product = Product(id=f"filter-{index}", store_id=store.id,
                              shopify_product_id=str(index), title=f"Product {index}",
                              handle=f"item-{index}", product_url="https://example.test/product",
                              vendor=vendor, product_type=kind, inventory_total=stock, price_min=price)
            db.add(product)
            db.flush()
            db.add(ProductIntelligence(product_id=product.id, eligibility_status=eligibility,
                                       normalization_status=normalization))
        db.commit()
    application = FastAPI()
    application.include_router(catalog.router, prefix="/api")
    try:
        with TestClient(application) as client:
            yield client, factory
    finally:
        engine.dispose()


@pytest.mark.parametrize("value", ["Adidas", "adidas", "ADIDAS", " Adidas ", "\tAdidas\n"])
def test_catalog_vendor_normalized_exact(catalog_filter_client, value):
    client, _ = catalog_filter_client
    response = client.get("/api/catalog/products", params={"vendor": value})
    assert response.status_code == 200
    assert response.json()["total"] == 2


@pytest.mark.parametrize("value", ["Fragrance", "fragrance", "FRAGRANCE", " Fragrance "])
def test_catalog_product_type_normalized_exact(catalog_filter_client, value):
    client, _ = catalog_filter_client
    assert client.get("/api/catalog/products", params={"product_type": value}).json()["total"] == 2


@pytest.mark.parametrize("field,value", [
    ("vendor", "ADDIDAS"), ("vendor", "Adi"), ("vendor", "%"), ("vendor", "_"),
    ("product_type", "Fragrnce"), ("product_type", "Frag"), ("product_type", "%"),
    ("product_type", "_"),
])
def test_catalog_structured_filters_do_not_fuzzy_or_wildcard_match(catalog_filter_client, field, value):
    client, _ = catalog_filter_client
    assert client.get("/api/catalog/products", params={field: value}).json()["total"] == 0


def test_catalog_literal_typo_and_wildcards_only_match_persisted_values(catalog_filter_client):
    client, factory = catalog_filter_client
    with factory() as db:
        db.get(Product, "filter-2").vendor = "ADDIDAS"
        db.commit()
    assert client.get("/api/catalog/products", params={"vendor": "addidas"}).json()["total"] == 1
    assert client.get("/api/catalog/products", params={"vendor": "A_%", "product_type": "Type_%"}).json()["total"] == 1


@pytest.mark.parametrize("extra,expected", [
    ({"product_type": "hair tools"}, 0), ({"product_type": " FRAGRANCE "}, 2),
    ({"stock_status": "in_stock"}, 1), ({"eligibility": "eligible"}, 1),
    ({"min_price": 50}, 1), ({"max_price": 20}, 0),
    ({"normalization_status": "COMPLETE"}, 1),
    ({"stock_status": "in_stock", "eligibility": "eligible", "max_price": 30, "product_type": "fragrance"}, 1),
])
def test_catalog_filters_preserve_and_semantics(catalog_filter_client, extra, expected):
    client, _ = catalog_filter_client
    assert client.get("/api/catalog/products", params={"vendor": "adidas", **extra}).json()["total"] == expected


def test_catalog_general_search_and_pagination_unchanged(catalog_filter_client):
    client, _ = catalog_filter_client
    for search in (" PRODUCT ", "ITEM-", " ADID "):
        expected = 2 if "ADID" in search else 7
        assert client.get("/api/catalog/products", params={"search": search}).json()["total"] == expected
    page = client.get("/api/catalog/products", params={"vendor": "adidas", "offset": 1, "limit": 1}).json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["offset"] == 1


def test_catalog_search_matches_exact_product_id_with_existing_eligibility_filters(catalog_filter_client):
    client, _ = catalog_filter_client

    response = client.get(
        "/api/catalog/products",
        params={
            "search": "filter-0",
            "stock_status": "in_stock",
            "eligibility": "eligible",
            "offset": 0,
            "limit": 20,
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == ["filter-0"]
    assert response.json()["limit"] == 20


def test_catalog_filter_options_clean_sorted_bounded_read_only(catalog_filter_client, monkeypatch):
    from app.api.routes import catalog
    client, factory = catalog_filter_client
    with factory() as db:
        before = db.execute(select(Product.__table__).order_by(Product.id)).all()
    response = client.get("/api/catalog/filter-options")
    assert response.status_code == 200
    assert response.json() == {"vendors": ["A_%", "Adidas", "Other"], "product_types": ["Fragrance", "Hair Tools", "Type_%"]}
    assert client.get("/api/catalog/filter-options").json() == response.json()
    monkeypatch.setattr(catalog, "FILTER_OPTIONS_LIMIT", 2)
    assert client.get("/api/catalog/filter-options").json() == {"vendors": ["A_%", "Adidas"], "product_types": ["Fragrance", "Hair Tools"]}
    with factory() as db:
        assert db.execute(select(Product.__table__).order_by(Product.id)).all() == before
        db.get(Product, "filter-2").vendor = "New Brand"
        db.commit()
    monkeypatch.setattr(catalog, "FILTER_OPTIONS_LIMIT", 1000)
    assert "New Brand" in client.get("/api/catalog/filter-options").json()["vendors"]


def catalog_product(**overrides):
    product = {
        "id": "gid://shopify/Product/1",
        "handle": "sample-fragrance",
        "title": "Sample Fragrance",
        "vendor": "Sample House",
        "productType": "Fragrance",
        "status": "ACTIVE",
        "tags": ["unisex", "niche"],
        "onlineStoreUrl": "https://example.myshopify.com/products/sample-fragrance",
        "totalInventory": 8,
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-02T00:00:00Z",
        "collections": [{"id": "gid://shopify/Collection/1", "title": "Fragrance"}],
        "metafields": [
            {"namespace": "custom", "key": "fragrance_family", "value": "Woody"},
            {"namespace": "custom", "key": "fragrance_notes", "value": "Cedar, Amber"},
            {"namespace": "custom", "key": "concentration", "value": "Eau de Parfum"},
        ],
        "variants": [{
            "id": "gid://shopify/ProductVariant/1",
            "sku": "SAMPLE-1",
            "title": "100ml",
            "price": "89.00",
            "compareAtPrice": "99.00",
            "inventoryQuantity": 8,
        }],
        "media": [{
            "id": "gid://shopify/MediaImage/1",
            "image": {
                "url": "https://cdn.shopify.com/sample.jpg",
                "width": 1200,
                "height": 1500,
                "altText": "Sample Fragrance bottle",
            },
        }],
    }
    product.update(overrides)
    return product


def test_normalization_uses_only_catalog_supported_values():
    normalized = normalize_shopify_product(catalog_product())

    assert normalized.brand == "Sample House"
    assert normalized.audience == "unisex"
    assert normalized.niche is None
    assert normalized.fragrance_family == "Woody"
    assert normalized.fragrance_notes == ["Cedar", "Amber"]
    assert normalized.concentration == "Eau de Parfum"
    assert normalized.size == "100 ml"
    assert normalized.size_source == "100ml"
    assert normalized.price_band == "50_to_99"
    assert normalized.eligibility_status == "ELIGIBLE"
    assert normalized.normalization_status == "COMPLETE"
    assert normalized.normalization_category == CATEGORY_FRAGRANCE


def test_missing_fields_remain_unknown_instead_of_being_invented():
    normalized = normalize_shopify_product(catalog_product(
        vendor=None,
        productType=None,
        tags=[],
        metafields=[],
        variants=[],
        media=[],
        totalInventory=0,
        createdAt=None,
    ))

    assert normalized.brand is None
    assert normalized.audience is None
    assert normalized.fragrance_family is None
    assert normalized.fragrance_notes == []
    assert normalized.concentration is None
    assert normalized.season is None
    assert normalized.occasion is None
    assert normalized.eligibility_status == "INELIGIBLE"
    assert normalized.normalization_status == "PARTIAL"


@pytest.mark.parametrize(
    ("product_type", "collections", "expected_category"),
    [
        ("Candles", [{"title": "Home Fragrance"}], CATEGORY_HOME_FRAGRANCE),
        ("Skin Care", [{"title": "Beauty"}], CATEGORY_BEAUTY),
        ("Bath & Body", [{"title": "Bath & Body"}], CATEGORY_BATH_BODY),
    ],
)
def test_non_fragrance_categories_do_not_require_fragrance_fields(
    product_type,
    collections,
    expected_category,
):
    normalized = normalize_shopify_product(catalog_product(
        productType=product_type,
        collections=collections,
        tags=[],
        metafields=[],
        title="Explicit catalog item",
    ))

    assert normalized.normalization_category == expected_category
    assert normalized.required_fields == ["brand", "price_band"]
    assert normalized.normalization_status == "COMPLETE"


@pytest.mark.parametrize(
    ("title", "expected_size", "expected_unit"),
    [
        ("Perfume – 3.4 oz", "3.4 oz", "oz"),
        ("Perfume – 1.7 fl oz", "1.7 fl oz", "fl oz"),
        ("Perfume 100ml", "100 ml", "ml"),
        ("Soap 200 g", "200 g", "g"),
        ("Gift Set – 3-Piece", "3 pieces", "pieces"),
    ],
)
def test_size_is_extracted_only_from_explicit_values(title, expected_size, expected_unit):
    product = catalog_product(title=title, variants=[{
        **catalog_product()["variants"][0],
        "title": "Default Title",
    }])
    normalized = normalize_shopify_product(product)

    assert normalized.size == expected_size
    assert normalized.size_unit == expected_unit
    assert normalized.size_source


def test_size_remains_unknown_without_supported_explicit_value():
    normalized = normalize_shopify_product(catalog_product(
        title="Sample fragrance",
        variants=[{**catalog_product()["variants"][0], "title": "Default Title"}],
        metafields=[],
    ))

    assert normalized.size is None
    assert normalized.size_source is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Eau de Parfum for Women", "women"),
        ("Cologne for Men", "men"),
        ("Parfum for Unisex", "unisex"),
        ("Character Set for Kids", "kids"),
    ],
)
def test_audience_uses_explicit_title_evidence(title, expected):
    normalized = normalize_shopify_product(catalog_product(title=title, tags=[]))
    assert normalized.audience == expected


def test_audience_is_not_inferred_from_brand():
    normalized = normalize_shopify_product(catalog_product(
        title="Signature Scent",
        vendor="Brand Associated With Women",
        tags=[],
        collections=[{"title": "Fragrance"}],
        metafields=[],
    ))
    assert normalized.audience is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Scent Extrait de Parfum", "Extrait de Parfum"),
        ("Scent EDP", "Eau de Parfum"),
        ("Scent Eau de Toilette", "Eau de Toilette"),
        ("Scent EDC", "Eau de Cologne"),
        ("Scent Parfum", "Parfum"),
        ("Scent Cologne", "Cologne"),
        ("Fresh Body Spray", "Body Spray"),
        ("Fresh Body Mist", "Body Mist"),
        ("Perfumed Deodorant", "Perfumed Deodorant"),
        ("Roll-On Perfume Oil", "Perfume Oil"),
        ("Cooling After Shave", "After Shave"),
    ],
)
def test_concentration_uses_explicit_terms(title, expected):
    normalized = normalize_shopify_product(catalog_product(title=title, metafields=[]))
    assert normalized.concentration == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"productType": "Gift Set", "title": "Two products"},
        {"productType": "Fragrance", "collections": [{"title": "Fragrance Gift Sets"}]},
        {"productType": "Fragrance", "title": "Discovery Set for Men"},
    ],
)
def test_gift_suitability_requires_explicit_set_evidence(overrides):
    normalized = normalize_shopify_product(catalog_product(**overrides))
    assert normalized.gift_suitability == "gift_set"
    assert normalized.normalization_category == CATEGORY_GIFT_SET


def test_curated_vendor_taxonomy_classifies_known_brands_only():
    niche = normalize_shopify_product(catalog_product(vendor="Mancera"))
    arabian = normalize_shopify_product(catalog_product(vendor="Lattafa"))
    designer = normalize_shopify_product(catalog_product(vendor="Gucci"))
    unknown = normalize_shopify_product(catalog_product(vendor="Unknown House"))

    assert niche.niche == "Mancera"
    assert arabian.arabian_classification == "arabian"
    assert designer.designer == "Gucci"
    assert unknown.designer is None
    assert unknown.niche is None
    assert unknown.arabian_classification is None


def test_unreviewed_classification_metafields_cannot_bypass_vendor_taxonomy():
    normalized = normalize_shopify_product(catalog_product(
        vendor="Unknown House",
        metafields=[
            {"namespace": "custom", "key": "designer", "value": "Designer"},
            {"namespace": "custom", "key": "niche", "value": "Niche"},
            {"namespace": "custom", "key": "arabian", "value": "Arabian"},
        ],
    ))

    assert normalized.designer is None
    assert normalized.niche is None
    assert normalized.arabian_classification is None


def test_fragrance_family_uses_collection_but_not_product_title():
    from_collection = normalize_shopify_product(catalog_product(
        metafields=[],
        collections=[{"title": "Amber Fragrance"}],
    ))
    title_only = normalize_shopify_product(catalog_product(
        title="Amber Dream Eau de Parfum for Women",
        metafields=[],
        collections=[{"title": "Fragrance"}],
    ))

    assert from_collection.fragrance_family == "Amber"
    assert title_only.fragrance_family is None


def test_unknown_optional_fields_are_not_fabricated():
    normalized = normalize_shopify_product(catalog_product(metafields=[]))
    assert normalized.fragrance_notes == []
    assert normalized.season is None
    assert normalized.occasion is None


def test_source_taxonomy_warnings_do_not_rewrite_category():
    bag = normalize_shopify_product(catalog_product(
        title="Marvel Spiderman Duffle Bag for Kids",
        productType="Perfume & Cologne",
    ))
    hair = normalize_shopify_product(catalog_product(
        title="Advanced Clean Dry Shampoo",
        productType="Skin Care",
        collections=[{"title": "Hair"}],
    ))

    assert bag.normalization_category == CATEGORY_FRAGRANCE
    assert bag.qa_warnings
    assert hair.normalization_category == CATEGORY_BEAUTY
    assert hair.qa_warnings


def test_eligibility_separates_positive_evidence_from_blockers():
    normalized = normalize_shopify_product(catalog_product(
        status="DRAFT",
        totalInventory=0,
    ))

    assert normalized.eligibility_status == "INELIGIBLE"
    assert "Product has an authentic catalog image." in normalized.eligibility_positive_reasons
    assert "Product has no available inventory." in normalized.eligibility_blocking_reasons
    assert "Product is not active in Shopify." in normalized.eligibility_blocking_reasons
    assert set(normalized.eligibility_reasons) == {
        *normalized.eligibility_positive_reasons,
        *normalized.eligibility_blocking_reasons,
    }


def test_empty_source_is_unknown():
    normalized = normalize_shopify_product({
        "title": "",
        "tags": [],
        "collections": [],
        "metafields": [],
        "variants": [],
        "media": [],
    })
    assert normalized.normalization_status == "UNKNOWN"


def test_flattened_bulk_rows_are_attached_to_their_product():
    product = catalog_product()
    rows = [
        {key: value for key, value in product.items() if key not in {"variants", "media", "collections", "metafields"}},
        {**product["variants"][0], "__typename": "ProductVariant", "__parentId": product["id"]},
        {**product["media"][0], "__typename": "MediaImage", "__parentId": product["id"]},
        {**product["collections"][0], "__typename": "Collection", "__parentId": product["id"]},
        {**product["metafields"][0], "__typename": "Metafield", "__parentId": product["id"]},
    ]

    assembled = assemble_bulk_products(rows)

    assert len(assembled) == 1
    assert assembled[0]["variants"][0]["sku"] == "SAMPLE-1"
    assert assembled[0]["media"][0]["image"]["width"] == 1200
    assert assembled[0]["collections"][0]["title"] == "Fragrance"
    assert assembled[0]["metafields"][0]["value"] == "Woody"


def test_grouped_bulk_rows_stream_one_product_at_a_time():
    first = catalog_product()
    second = catalog_product(
        id="gid://shopify/Product/2",
        handle="second",
        title="Second",
    )

    async def rows():
        yield {key: value for key, value in first.items() if key not in {"variants", "media", "collections", "metafields"}}
        yield {**first["variants"][0], "__typename": "ProductVariant", "__parentId": first["id"]}
        yield {key: value for key, value in second.items() if key not in {"variants", "media", "collections", "metafields"}}

    async def collect():
        return [product async for product in stream_bulk_products(rows())]

    streamed = asyncio.run(collect())
    assert [product["id"] for product in streamed] == [first["id"], second["id"]]
    assert streamed[0]["variants"][0]["sku"] == "SAMPLE-1"
    assert streamed[1]["variants"] == []


def test_product_upsert_is_idempotent():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    store = Store(name="Example", shop_domain="example.myshopify.com")
    db.add(store)
    db.commit()

    service = CatalogSyncService(session_factory=factory)
    assert service._upsert_product(db, catalog_product(), store.id) is True
    db.commit()
    changed = catalog_product(title="Updated Sample", totalInventory=4)
    changed["variants"][0]["inventoryQuantity"] = 4
    assert service._upsert_product(db, changed, store.id) is False
    db.commit()

    assert db.scalar(select(func.count(Product.id))) == 1
    assert db.scalar(select(func.count(ProductVariant.id))) == 1
    assert db.scalar(select(func.count(ProductImage.id))) == 1
    assert db.scalar(select(func.count(ProductIntelligence.id))) == 1
    saved = db.scalar(select(Product))
    assert saved.title == "Updated Sample"
    assert saved.inventory_total == 4
    intelligence = db.scalar(select(ProductIntelligence))
    intelligence.normalized_data = {}
    db.commit()

    first_pass = service.renormalize_existing_products()
    second_pass = service.renormalize_existing_products()
    assert first_pass["seen"] == 1
    assert first_pass["changed"] == 1
    assert second_pass["seen"] == 1
    assert second_pass["changed"] == 0
    assert second_pass["unchanged"] == 1
    assert db.scalar(select(func.count(ProductIntelligence.id))) == 1
    db.close()


def test_second_active_sync_is_rejected():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = CatalogSyncService(session_factory=factory)

    first = service.create_job("example.myshopify.com")
    assert first.status == "QUEUED"

    try:
        service.create_job("example.myshopify.com")
    except SyncAlreadyRunning:
        pass
    else:
        raise AssertionError("Expected a concurrent sync to be rejected")
