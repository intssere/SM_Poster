from app.services.utm import build_pinterest_utm_url


def test_utm_preserves_existing_query():
    url = build_pinterest_utm_url("https://diamondshelf.us/products/test?variant=123", campaign="wave1", content="pin-a")
    assert "variant=123" in url
    assert "utm_source=pinterest" in url
    assert "utm_medium=organic_social" in url
    assert "utm_campaign=wave1" in url
    assert "utm_content=pin-a" in url
