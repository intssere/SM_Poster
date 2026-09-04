from types import SimpleNamespace
from app.services.publication_preview import CONFIRMATION_PROMPT


def test_confirmation_prompt_is_server_owned_and_bounded():
    assert "reviewed this exact approved publication" in CONFIRMATION_PROMPT
    assert "future manual Pinterest dispatch" in CONFIRMATION_PROMPT


def test_preview_service_module_exposes_no_provider_credentials():
    from app.services import publication_preview
    assert not hasattr(publication_preview, "access_token")
    assert not hasattr(publication_preview, "refresh_token")
