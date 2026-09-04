from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import CreativeTemplate, PinCreative, PinPublication, PinterestBoard, ProductImage, PublicationStatus
from app.services.pinterest_publication_quality import (
    PIN_ALT_TEXT_MAX,
    PIN_DESCRIPTION_MAX,
    PIN_TITLE_MAX,
    PINTEREST_QUALITY_V1,
    validate_publication_quality,
)


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _publication(db, **overrides):
    template = CreativeTemplate(
        id="template-1",
        key="product_classification",
        version=1,
        name="Product classification",
        active=True,
    )
    source_image = ProductImage(
        id="source-image-1",
        product_id="product-1",
        source_url="https://cdn.shopify.com/s/files/source.jpg",
        width=1200,
        height=1600,
        editorial_eligible=True,
    )
    creative = PinCreative(
        id="creative-1",
        draft_id="draft-1",
        template_id="template-1",
        source_image_id="source-image-1",
        rendered_url="https://cdn.shopify.com/s/files/creative.jpg",
        creative_fingerprint="c" * 64,
        width=1000,
        height=1500,
        render_status="COMPLETE",
    )
    board = PinterestBoard(
        id="board-record-1",
        connection_id="connection-1",
        external_board_id="board-1",
        name="Fragrance Finds",
        is_active=True,
        is_eligible=True,
        routing_label="fragrance",
    )
    publication = PinPublication(
        id="publication-1",
        draft_id="draft-1",
        creative_id=creative.id,
        source_image_id=creative.source_image_id,
        template_id=creative.template_id,
        template_key="product_classification",
        template_version=1,
        creative_fingerprint=creative.creative_fingerprint,
        pinterest_connection_id="connection-1",
        pinterest_board_record_id=board.id,
        pinterest_board_id_snapshot=board.external_board_id,
        title_snapshot="Fragrance gift pick",
        description_snapshot="Explore this fragrance gift pick for a polished scent routine.",
        alt_text_snapshot="A verified product creative for a fragrance gift pick.",
        destination_url="https://diamondshelf.us/products/fragrance-pick",
        utm_url="https://diamondshelf.us/products/fragrance-pick?utm_source=pinterest&utm_medium=social",
        media_url_snapshot="https://cdn.shopify.com/s/files/creative.jpg",
        publication_fingerprint="p" * 64,
        status=PublicationStatus.APPROVED,
    )
    for key, value in overrides.items():
        if key.startswith("creative__"):
            setattr(creative, key.split("__", 1)[1], value)
        elif key.startswith("board__"):
            setattr(board, key.split("__", 1)[1], value)
        elif key.startswith("template__"):
            setattr(template, key.split("__", 1)[1], value)
        elif key.startswith("source_image__"):
            setattr(source_image, key.split("__", 1)[1], value)
        else:
            setattr(publication, key, value)
    db.add_all([template, source_image, creative, board, publication])
    db.commit()
    return publication


def _result(**overrides):
    db = _db()
    publication = _publication(db, **overrides)
    return validate_publication_quality(db, publication)


def _codes(result, *, failed=True):
    return {check["code"] for check in result["checks"] if check["passed"] is not (not failed)}


def _failed_codes(result):
    return {check["code"] for check in result["checks"] if not check["passed"] and check["severity"] == "FAIL"}


def _warning_codes(result):
    return {check["code"] for check in result["checks"] if not check["passed"] and check["severity"] == "WARNING"}


def test_valid_immutable_snapshot_passes_quality_policy():
    result = _result()
    assert result["policy_version"] == PINTEREST_QUALITY_V1
    assert result["status"] == "PASS"
    assert _failed_codes(result) == set()


def test_title_description_and_alt_provider_limits_are_hard_failures():
    assert "TITLE_REQUIRED" in _failed_codes(_result(title_snapshot=""))
    assert "TITLE_WITHIN_PROVIDER_LIMIT" in _failed_codes(_result(title_snapshot="T" * (PIN_TITLE_MAX + 1)))
    assert "DESCRIPTION_REQUIRED" in _failed_codes(_result(description_snapshot=""))
    assert "DESCRIPTION_WITHIN_PROVIDER_LIMIT" in _failed_codes(_result(description_snapshot="D" * (PIN_DESCRIPTION_MAX + 1)))
    assert "ALT_REQUIRED" in _failed_codes(_result(alt_text_snapshot=""))
    assert "ALT_WITHIN_PROVIDER_LIMIT" in _failed_codes(_result(alt_text_snapshot="A" * (PIN_ALT_TEXT_MAX + 1)))


def test_text_quality_rejects_control_characters_keyword_stuffing_and_unsupported_claims():
    result = _result(title_snapshot="Fragrance fragrance fragrance fragrance fragrance fragrance gift pick")
    assert "TITLE_NO_KEYWORD_STUFFING" in _failed_codes(result)
    assert "DESCRIPTION_NO_CONTROL_CHARACTERS" in _failed_codes(_result(description_snapshot="Good copy\x01bad"))
    assert "DESCRIPTION_NO_UNSUPPORTED_CLAIMS" in _failed_codes(_result(description_snapshot="This fragrance is guaranteed to cure every concern."))
    assert "ALT_NOT_URL_ONLY" in _failed_codes(_result(alt_text_snapshot="https://example.test/image.jpg"))
    assert "ALT_NO_INTERNAL_PROMPT_METADATA" in _failed_codes(_result(alt_text_snapshot="Prompt metadata for internal id 123"))
    assert "ALT_NOT_KEYWORD_DUMP" in _failed_codes(_result(alt_text_snapshot="fragrance, perfume, scent, aroma, gift, luxury, women, perfume"))


def test_destination_url_structural_gate_is_offline_and_canonical():
    assert "DESTINATION_URL_HTTPS" in _failed_codes(_result(destination_url="http://diamondshelf.us/products/a"))
    assert "DESTINATION_URL_PUBLIC_HOST" in _failed_codes(_result(destination_url="https://localhost/products/a"))
    assert "DESTINATION_URL_PUBLIC_HOST" in _failed_codes(_result(destination_url="https://10.0.0.1/products/a"))
    assert "DESTINATION_URL_PUBLIC_HOST" in _failed_codes(_result(destination_url="https://diamondshelf.test/products/a"))
    assert "DESTINATION_URL_PUBLIC_HOST" in _failed_codes(_result(destination_url="https://shop.example/products/a"))
    assert "DESTINATION_URL_HTTPS" in _failed_codes(_result(destination_url="javascript:alert(1)"))
    assert "DESTINATION_URL_CANONICAL_DIAMOND_SHELF_HOST" in _failed_codes(_result(destination_url="https://example.test/products/a"))
    assert "DESTINATION_URL_UTM_KEYS_UNIQUE" in _failed_codes(_result(destination_url="https://diamondshelf.us/products/a?utm_source=pinterest&utm_source=pinterest"))
    assert "DESTINATION_URL_NO_SHORTENER" in _failed_codes(_result(destination_url="https://bit.ly/abc"))


def test_media_url_structural_gate_rejects_non_public_or_non_https_sources():
    assert "MEDIA_URL_HTTPS" in _failed_codes(_result(media_url_snapshot="http://cdn.shopify.com/creative.jpg"))
    assert "MEDIA_URL_PUBLIC_HOST" in _failed_codes(_result(media_url_snapshot="https://localhost/creative.jpg"))
    assert "MEDIA_URL_PUBLIC_HOST" in _failed_codes(_result(media_url_snapshot="https://192.168.1.10/creative.jpg"))
    assert "MEDIA_URL_PUBLIC_HOST" in _failed_codes(_result(media_url_snapshot="https://cdn.example.test/creative.jpg"))
    assert "MEDIA_URL_HTTPS" in _failed_codes(_result(media_url_snapshot="file:///tmp/creative.jpg"))
    assert "MEDIA_URL_HTTPS" in _failed_codes(_result(media_url_snapshot="data:image/png;base64,abc"))


def test_creative_provenance_and_dimensions_are_validated_from_immutable_identity():
    assert "CREATIVE_DRAFT_MATCH" in _failed_codes(_result(creative__draft_id="different-draft"))
    assert "SOURCE_IMAGE_MATCH" in _failed_codes(_result(creative__source_image_id="different-source"))
    assert "CREATIVE_FINGERPRINT_MATCH" in _failed_codes(_result(creative__creative_fingerprint="x" * 64))
    assert "CREATIVE_MEDIA_URL_PRESENT" in _failed_codes(_result(creative__rendered_url=""))
    assert "CREATIVE_MEDIA_URL_MATCH" in _failed_codes(_result(creative__rendered_url="https://cdn.shopify.com/s/files/other.jpg"))
    assert "CREATIVE_TEMPLATE_ID_MATCH" in _failed_codes(_result(creative__template_id="template-other"))
    assert "CREATIVE_RENDER_COMPLETE" in _failed_codes(_result(creative__render_status="PENDING"))
    assert "CREATIVE_DIMENSIONS_VALID" in _failed_codes(_result(creative__width=0))
    result = _result(creative__width=1200, creative__height=1200)
    assert result["status"] == "WARNING"
    assert "CREATIVE_ASPECT_RATIO_RECOMMENDED" in _warning_codes(result)


def test_template_and_source_image_provenance_are_required():
    assert "PUBLICATION_TEMPLATE_PRESENT" in _failed_codes(_result(template_version=None))
    assert "TEMPLATE_PRESENT" in _failed_codes(_result(template_id="missing-template"))
    assert "TEMPLATE_KEY_MATCH" in _failed_codes(_result(template__key="wrong_key"))
    assert "TEMPLATE_VERSION_MATCH" in _failed_codes(_result(template__version=2))
    assert "SOURCE_IMAGE_PRESENT" in _failed_codes(_result(source_image_id="missing-source"))


def test_utm_url_required_and_must_match_destination_target():
    assert "UTM_URL_REQUIRED" in _failed_codes(_result(utm_url=None))
    assert "UTM_URL_TARGET_MATCH" in _failed_codes(
        _result(utm_url="https://diamondshelf.us/collections/sale?utm_source=pinterest")
    )
    assert _failed_codes(
        _result(utm_url="https://diamondshelf.us/products/fragrance-pick/?utm_source=pinterest")
    ) == set()


def test_utm_structure_requires_unique_nonempty_pinterest_source():
    assert "UTM_URL_UTM_KEYS_UNIQUE" in _failed_codes(
        _result(utm_url="https://diamondshelf.us/products/fragrance-pick?utm_source=pinterest&utm_source=pinterest")
    )
    assert "UTM_URL_UTM_VALUES_NONEMPTY" in _failed_codes(
        _result(utm_url="https://diamondshelf.us/products/fragrance-pick?utm_source=")
    )
    assert "UTM_URL_UTM_SOURCE_PINTEREST" in _failed_codes(
        _result(utm_url="https://diamondshelf.us/products/fragrance-pick?utm_source=meta")
    )


def test_board_relevance_phase1_uses_routing_label_as_warning_not_speculative_hard_fail():
    result = _result(board__routing_label="kitchen tools")
    assert result["status"] == "WARNING"
    assert "BOARD_ROUTING_LABEL_MISMATCH" in _warning_codes(result)
    assert "BOARD_ROUTING_LABEL_MISMATCH" not in _failed_codes(result)
    unknown = _result(board__routing_label=None)
    assert "BOARD_RELEVANCE_WEAK_SIGNAL" in _warning_codes(unknown)


def test_quality_result_contains_no_credentials_or_raw_provider_payloads():
    result = _result()
    rendered = repr(result).lower()
    for forbidden in ("access_token", "refresh_token", "authorization", "bearer ", "client_secret", "ciphertext", "raw_body", "raw_json", "traceback"):
        assert forbidden not in rendered
