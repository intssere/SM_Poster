import json
from decimal import Decimal

from sqlalchemy import func, select

from app.models.domain import (
    AIRequestTelemetry,
    ContentRevision,
    PinApproval,
    PinCreative,
    PinDraft,
    PinPublication,
)
from app.services.ai_providers import (
    OllamaTextProvider,
    OpenAIImageProvider,
    OpenAITextProvider,
    ProviderUnavailable,
    TextGenerationResult,
)
from app.services.ai_regeneration import AIRegenerationError, AIRegenerationService, AISettingsService

from test_creative_rendering import CreativeRenderService, CreativeStorage, png
from test_pin_proposals import add_product, setup_service


class FakeProvider:
    def __init__(self, text, *, name="ollama", model="test-model", error=None, usage=(20, 30, 50)):
        self.name = name
        self.model = model
        self.text = text
        self.error = error
        self.usage = usage
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        assert "Persisted facts" in prompt
        assert "TEXT ONLY" in prompt
        if self.error:
            raise self.error
        return TextGenerationResult(
            text=self.text,
            model=self.model,
            prompt_tokens=self.usage[0],
            completion_tokens=self.usage[1],
            total_tokens=self.usage[2],
        )


def safe_copy(title):
    return json.dumps({
        "headline": f"Discover {title}",
        "title": f"{title} | Catalog Edit",
        "description": f"Explore {title} using verified catalog details.",
        "alt_text": f"{title} shown in the authentic Shopify product image.",
    })


def prepared(suffix):
    db, store, proposal_service = setup_service()
    product = add_product(db, store, suffix=suffix)
    draft_id = proposal_service.generate_controlled_batch(
        product_limit=1, max_proposals_per_product=1
    )["representative_proposals"][0]["id"]
    return db, product, proposal_service, draft_id


def test_ollama_adapter_success_health_and_failure_are_sanitized():
    calls = []

    def request(method, url, headers, payload, timeout):
        calls.append((method, url, headers, payload, timeout))
        if url.endswith("/api/tags"):
            return 200, {"models": [{"name": "llama-test"}]}
        return 200, {"response": '{"headline":"ok"}', "prompt_eval_count": 4, "eval_count": 2}

    provider = OllamaTextProvider("http://127.0.0.1:11434", "llama-test", 7, requester=request)
    assert provider.health()["model_available"] is True
    result = provider.generate("prompt")
    assert result.total_tokens == 6
    assert calls[-1][-1] == 7.0

    failed = OllamaTextProvider(
        "http://127.0.0.1:11434",
        "llama-test",
        requester=lambda *_: (503, {"error": "internal-sensitive-details"}),
    )
    status = failed.health()
    assert status["reachable"] is False
    assert "internal-sensitive-details" not in json.dumps(status)


def test_ollama_endpoint_rejects_ssrf_and_embedded_credentials():
    for endpoint in (
        "http://169.254.169.254",
        "https://localhost:11434",
        "http://user:password@localhost:11434",
        "http://localhost:11434/api",
    ):
        try:
            OllamaTextProvider(endpoint, "llama-test")
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe local endpoint was accepted: {endpoint}")


def test_openai_adapter_success_failure_and_secret_non_leakage():
    secret = "sk-test-secret-never-return"
    observed = []

    def request(method, url, headers, payload, timeout):
        observed.append(headers)
        if method == "GET":
            return 200, {"data": [{"id": "gpt-test"}]}
        return 200, {
            "choices": [{"message": {"content": '{"headline":"ok"}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    provider = OpenAITextProvider("gpt-test", api_key=secret, requester=request)
    assert provider.health()["reachable"] is True
    assert provider.generate("prompt").total_tokens == 15
    assert observed[0]["Authorization"] == f"Bearer {secret}"
    assert secret not in json.dumps(provider.health())

    rejected = OpenAITextProvider("gpt-test", api_key=secret, requester=lambda *_: (401, {"message": secret}))
    status = rejected.health()
    assert status["failure_code"] == "authentication_error"
    assert secret not in json.dumps(status)


def test_openai_image_adapter_requires_inline_output_and_background_safety_gate():
    import base64

    image_bytes = png((1024, 1536))
    calls = []

    def request(method, url, headers, payload, timeout):
        calls.append((url, payload))
        if url.endswith("/images/generations"):
            return 200, {"data": [{"b64_json": base64.b64encode(image_bytes).decode()}]}
        return 200, {"choices": [{"message": {"content": json.dumps({
            "background_only": True,
            "contains_product": False,
            "contains_packaging": False,
            "contains_logo": False,
            "contains_text": False,
            "contains_person": False,
        })}}]}

    provider = OpenAIImageProvider(
        "gpt-image-1", api_key="test-only", safety_model="gpt-4o-mini", requester=request,
    )
    result = provider.generate_background("Muted editorial light.")
    decision = provider.validate_background(result.image_bytes)
    assert decision["background_only"] is True
    assert "data:image/png;base64," in json.dumps(calls[-1][1])

    url_only = OpenAIImageProvider(
        "gpt-image-1", api_key="test-only",
        requester=lambda *_: (200, {"data": [{"url": "https://example.com/image.png"}]}),
    )
    try:
        url_only.generate_background("Muted editorial light.")
    except ProviderUnavailable as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("URL-only image output must not trigger arbitrary image fetching")


def test_local_provider_success_persists_usage_and_safe_revision():
    db, product, proposal_service, draft_id = prepared("local-provider")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True, provider_mode="local_free", local_model="llama-test"
    )
    provider = FakeProvider(safe_copy(product.title))
    revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: provider,
    ).regenerate_copy(draft_id)

    usage = db.scalar(select(AIRequestTelemetry))
    assert provider.calls == 1
    assert revision["generation_mode"] == "provider_generated"
    assert revision["provider_mode"] == "local_free"
    assert revision["actual_cost_usd"] is None
    assert usage.success is True
    assert usage.provider == "ollama"
    assert usage.total_tokens == 50
    assert usage.fallback_used is False
    db.close()


def test_unavailable_local_provider_records_failure_and_uses_deterministic_fallback():
    db, product, proposal_service, draft_id = prepared("local-fallback")
    AISettingsService(proposal_service.session_factory).update(enabled=True, provider_mode="local_free")
    provider = FakeProvider(
        safe_copy(product.title),
        error=ProviderUnavailable("timeout", "provider timed out"),
    )
    revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: provider,
    ).regenerate_copy(draft_id)

    usage = db.scalar(select(AIRequestTelemetry))
    assert revision["generation_mode"] == "fallback_timeout"
    assert "persisted catalog information" in revision["description"]
    assert usage.success is False
    assert usage.failure_code == "timeout"
    assert usage.fallback_used is True
    db.close()


def test_hosted_provider_records_tokens_cost_and_never_persists_secret(monkeypatch):
    db, product, proposal_service, draft_id = prepared("hosted-provider")
    secret = "sk-hosted-secret-never-store"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-4o-mini",
        daily_budget_usd=5,
        monthly_budget_usd=20,
    )
    provider = FakeProvider(
        safe_copy(product.title),
        name="openai",
        model="gpt-4o-mini",
        usage=(1000, 500, 1500),
    )
    revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: provider,
    ).regenerate_copy(draft_id)

    usage = db.scalar(select(AIRequestTelemetry))
    settings = AISettingsService(proposal_service.session_factory).get()
    assert revision["estimated_cost_usd"] == 0.00045
    assert isinstance(revision["estimated_cost_usd"], float)
    assert revision["actual_cost_usd"] is None
    assert usage.estimated_cost_usd == Decimal("0.00045000")
    assert usage.actual_cost_usd is None
    assert isinstance(settings["daily_budget_usd"], float)
    assert settings["credentials_configured"] is True
    persisted = json.dumps([
        dict(row) for row in db.execute(select(AIRequestTelemetry.__table__)).mappings()
    ], default=str)
    assert secret not in persisted
    assert secret not in json.dumps(settings)
    db.close()


def test_unsafe_provider_copy_is_rejected_before_revision_but_telemetry_remains():
    db, product, proposal_service, draft_id = prepared("unsafe-provider")
    AISettingsService(proposal_service.session_factory).update(enabled=True, provider_mode="local_free")
    unsafe = json.dumps({
        "headline": f"Number one {product.title}",
        "title": product.title,
        "description": f"{product.title} is the bestseller deal of the season.",
        "alt_text": f"{product.title} product image",
    })
    provider = FakeProvider(unsafe)

    try:
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=lambda _: provider,
        ).regenerate_copy(draft_id)
    except AIRegenerationError as exc:
        assert "unsupported claim" in str(exc)
    else:
        raise AssertionError("Unsafe provider copy must not create a revision")

    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    usage = db.scalar(select(AIRequestTelemetry))
    assert usage.failure_code == "fact_safety_rejected"
    assert usage.success is False
    db.close()


def test_nonnumeric_hallucinated_catalog_fact_is_rejected():
    db, product, proposal_service, draft_id = prepared("unsupported-wording")
    AISettingsService(proposal_service.session_factory).update(enabled=True, provider_mode="local_free")
    provider = FakeProvider(json.dumps({
        "headline": product.title,
        "title": product.title,
        "description": f"{product.title} includes an Italian leather case.",
        "alt_text": f"{product.title} product image",
    }))
    try:
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=lambda _: provider,
        ).regenerate_copy(draft_id)
    except AIRegenerationError as exc:
        assert "unsupported catalog wording" in str(exc)
    else:
        raise AssertionError("Unverified nonnumeric facts must not be stored")
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    db.close()


def test_paid_budget_blocks_provider_call_and_leaves_local_mode_available():
    db, product, proposal_service, draft_id = prepared("budget-provider")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-4o-mini",
        daily_budget_usd=0.000001,
        monthly_budget_usd=0.000001,
    )
    hosted = FakeProvider(safe_copy(product.title), name="openai", model="gpt-4o-mini")
    service = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: hosted,
    )
    revision = service.regenerate_copy(draft_id)

    usage = db.scalar(select(AIRequestTelemetry))
    assert hosted.calls == 0
    assert revision["generation_mode"] == "fallback_budget_exceeded"
    assert usage.failure_code == "budget_exceeded"
    assert usage.fallback_used is True

    AISettingsService(proposal_service.session_factory).update(
        enabled=True, provider_mode="local_free"
    )
    local = FakeProvider(safe_copy(product.title), name="ollama")
    local_revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: local,
    ).regenerate_copy(draft_id)
    assert local.calls == 1
    assert local_revision["generation_mode"] == "provider_generated"
    db.close()


def test_paid_zero_budget_and_unpriced_model_fail_closed_without_provider_calls():
    db, product, proposal_service, draft_id = prepared("budget-fail-closed")
    provider = FakeProvider(safe_copy(product.title), name="openai", model="unknown-paid-model")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="unknown-paid-model",
        daily_budget_usd=10,
        monthly_budget_usd=100,
    )
    revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: provider,
    ).regenerate_copy(draft_id)
    assert provider.calls == 0
    assert revision["generation_mode"] == "fallback_pricing_unavailable"

    AISettingsService(proposal_service.session_factory).update(
        daily_budget_usd=0,
        monthly_budget_usd=0,
    )
    priced = FakeProvider(safe_copy(product.title), name="openai", model="gpt-4o-mini")
    zero_revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: priced,
    ).regenerate_copy(draft_id)
    assert priced.calls == 0
    assert zero_revision["generation_mode"] == "fallback_budget_exceeded"
    db.close()


def test_provider_regeneration_preserves_original_proposal_creative_and_publish_state(tmp_path):
    db, product, proposal_service, draft_id = prepared("provider-immutability")
    renderer = CreativeRenderService(
        proposal_service.session_factory,
        downloader=lambda _: png(),
        storage=CreativeStorage(tmp_path),
    )
    assert renderer.render_review_batch(1)["rendered"] == 1
    draft = db.get(PinDraft, draft_id)
    creative = db.scalar(select(PinCreative).where(PinCreative.draft_id == draft_id))
    before = (
        draft.title, draft.description, draft.text_fingerprint, draft.status,
        creative.id, creative.sha256, creative.creative_fingerprint, creative.render_spec,
    )
    AISettingsService(proposal_service.session_factory).update(enabled=True, provider_mode="local_free")
    provider = FakeProvider(safe_copy(product.title))
    AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: provider,
    ).regenerate_copy(draft_id)

    db.expire_all()
    assert (
        draft.title, draft.description, draft.text_fingerprint, draft.status,
        creative.id, creative.sha256, creative.creative_fingerprint, creative.render_spec,
    ) == before
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()
