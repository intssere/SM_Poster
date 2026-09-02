from app.services.pinterest_publisher import media_publishable

def test_media_url_rejects_private_and_accepts_public():
    assert not media_publishable("https://localhost/a")
    assert not media_publishable("http://example.test/a")
    assert not media_publishable("https://127.0.0.1/a")
    assert media_publishable("https://cdn.example.test/a")
