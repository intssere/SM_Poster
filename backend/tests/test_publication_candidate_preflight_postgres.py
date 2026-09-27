"""Isolated PostgreSQL regression; skipped without the existing CI test-DB URL."""
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.models.domain import (
    ContentAngle, CreativeTemplate, DraftStatus, PinApproval, PinConcept,
    PinCreative, PinDraft, PinPublication, PinterestBoard, PinterestConnection,
    Product, ProductImage, PublicationStatus, Store,
)
from app.services.publication_candidate_preflight import preflight_candidate
from test_publication_candidate_preflight import DIGEST, PNG, FakeMedia
from test_routine_canary_fixture_postgres import isolated_postgres  # noqa: F401


def test_postgres_transient_preflight_never_excludes_a_real_row(isolated_postgres, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("PUBLIC_MEDIA_BASE_URL", "https://media.example.com")
    get_settings.cache_clear()
    engine = sa.create_engine(isolated_postgres)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        now = datetime.now(timezone.utc)
        rows = [
            Store(id="store", name="Test", shop_domain="test.example.com"),
            Product(
                id="product", store_id="store", shopify_product_id="test-product",
                handle="item", title="Item",
                product_url="https://diamondshelf.us/products/item",
            ),
            ContentAngle(id="angle", key="gift", name="Gift"),
            PinConcept(
                id="concept", store_id="store", product_id="product",
                content_angle_id="angle", fingerprint="a" * 64,
            ),
            PinDraft(
                id="draft", concept_id="concept", version=1,
                title="Fragrance gift pick",
                description="Explore this fragrance gift pick for a polished scent routine.",
                alt_text="A verified product creative for a fragrance gift pick.",
                destination_url="https://diamondshelf.us/products/item",
                utm_url="https://diamondshelf.us/products/item?utm_source=pinterest&utm_medium=social",
                text_fingerprint="b" * 64, status=DraftStatus.APPROVED,
            ),
            ProductImage(
                id="source", product_id="product",
                source_url="https://media.example.com/source.jpg",
                width=1200, height=1600, editorial_eligible=True,
            ),
            CreativeTemplate(id="template", key="gift", version=1, name="Gift"),
            PinCreative(
                id="creative", draft_id="draft", template_id="template",
                source_image_id="source", creative_fingerprint="c" * 64,
                rendered_url="/api/pins/creatives/creative/image",
                sha256=DIGEST, size_bytes=len(PNG), render_status="RENDERED",
                width=1000, height=1500,
            ),
            PinApproval(
                id="approval", draft_id="draft", creative_id="creative",
                approved_version_id="original", decision="APPROVED", decided_by="test",
            ),
            PinterestConnection(
                id="connection", external_user_id="test-user",
                access_token_ciphertext="synthetic", refresh_token_ciphertext="synthetic",
                status="CONNECTED", boards_last_synced_at=now,
            ),
            PinterestBoard(
                id="board", connection_id="connection", external_board_id="external",
                name="Test board", is_active=True, is_eligible=True,
                last_synced_at=now,
            ),
        ]
        # Explicit flushes are needed for ID-only fixtures: the ORM cannot
        # infer insert ordering from relationships that were never assigned.
        for parent_types in (
            (Store, ContentAngle, CreativeTemplate, PinterestConnection),
            (Product,),
            (PinConcept, ProductImage),
            (PinDraft, PinterestBoard),
            (PinCreative,),
            (PinApproval,),
        ):
            db.add_all([row for row in rows if type(row) in parent_types])
            db.flush()
        db.commit()

        def check():
            return preflight_candidate(
                db, approval_id="approval", pinterest_board_record_id="board",
                storage=FakeMedia(),
            )

        assert check()["status"] == "ELIGIBLE"
        assert db.scalar(sa.select(sa.func.count()).select_from(PinPublication)) == 0
        assert not db.new and not db.dirty
        # The candidate has no DB identity. Even a historical row sharing all
        # fingerprint inputs must be included in the duplicate scan.
        from app.services.publication_identity import build_publication_candidate
        transient = build_publication_candidate(
            db, approval_id="approval", board_id=None,
            pinterest_connection_id="connection", pinterest_board_record_id="board",
        )
        db.add(PinPublication(
            draft_id="draft", creative_id="creative",
            publication_fingerprint=transient.publication_fingerprint,
            status=PublicationStatus.APPROVED,
        ))
        db.commit()
        result = check()
        assert result["duplicate"] == "DUPLICATE_PUBLICATION"
        assert result["status"] == "BLOCKED"
        assert db.scalar(sa.select(sa.func.count()).select_from(PinPublication)) == 1
        assert not db.new and not db.dirty
    finally:
        db.close()
        engine.dispose()
        get_settings.cache_clear()