from app.services.scoring import ProductSignals, score_product


def test_out_of_stock_product_scores_zero():
    assert score_product(ProductSignals(inventory_available=False, image_quality=1, brand_recognition=1)) == 0


def test_strong_product_scores_high():
    score = score_product(ProductSignals(
        inventory_available=True, image_quality=1, brand_recognition=1,
        seasonal_relevance=.8, gift_relevance=.8, newness=.7,
        manual_priority=.5, content_coverage_gap=1,
    ))
    assert score > 75
