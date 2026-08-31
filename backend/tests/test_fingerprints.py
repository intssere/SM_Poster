from app.services.fingerprints import concept_fingerprint, creative_fingerprint, text_fingerprint


def test_concept_fingerprint_is_order_independent_for_products():
    a = concept_fingerprint(product_ids=["2", "1"], content_angle="Gift Ideas", keyword_cluster="gifts", board_id="b1")
    b = concept_fingerprint(product_ids=["1", "2"], content_angle="gift ideas", keyword_cluster="gifts", board_id="b1")
    assert a == b


def test_text_fingerprint_normalizes_whitespace_and_case():
    a = text_fingerprint(title="Luxury  Perfume", description="Best PICK", alt_text="Bottle")
    b = text_fingerprint(title="luxury perfume", description="best pick", alt_text="bottle")
    assert a == b


def test_creative_template_version_changes_hash():
    a = creative_fingerprint(source_image_sha256="x", template_key="luxury", template_version=1, text_hash="t")
    b = creative_fingerprint(source_image_sha256="x", template_key="luxury", template_version=2, text_hash="t")
    assert a != b
