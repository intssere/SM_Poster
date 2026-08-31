import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.domain import (
    AIGeneratedAsset,
    AIRequestTelemetry,
    AISettings,
    ContentRevision,
    ContentVersionSelection,
    PinApproval,
    PinCreative,
    PinDraft,
    PinPublication,
)
from app.api.routes import proposals as proposal_routes
from app.schemas.pins import RegenerationRequest
from app.services.ai_providers import (
    OllamaTextProvider,
    OpenAIImageProvider,
    OpenAITextProvider,
    ProviderUnavailable,
    TextGenerationResult,
    provider_for_settings,
    video_provider_for_settings,
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
        observed.append((method, url, headers, payload, timeout))
        if method == "GET":
            return 200, {"data": [{"id": "gpt-5.6-luna"}]}
        return 200, {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": '{"headline":"ok"}'}],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    provider = OpenAITextProvider("gpt-5.6-luna", api_key=secret, requester=request)
    assert provider.health()["reachable"] is True
    assert provider.generate("prompt").total_tokens == 15
    assert observed[0][2]["Authorization"] == f"Bearer {secret}"
    assert observed[1][1] == "https://api.openai.com/v1/responses"
    assert observed[1][3]["model"] == "gpt-5.6-luna"
    assert observed[1][3]["text"]["format"]["type"] == "json_schema"
    assert observed[1][3]["text"]["format"]["strict"] is True
    assert "messages" not in observed[1][3]
    assert secret not in json.dumps(provider.health())

    rejected = OpenAITextProvider("gpt-5.6-luna", api_key=secret, requester=lambda *_: (401, {"message": secret}))
    status = rejected.health()
    assert status["failure_code"] == "authentication_error"
    assert secret not in json.dumps(status)


def test_terra_requires_explicit_selection_and_hosted_video_provider_is_disabled():
    luna_settings = SimpleNamespace(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-5.6-luna",
        video_model="sora-2",
        request_timeout_seconds=30,
    )
    terra_settings = SimpleNamespace(
        **{**vars(luna_settings), "hosted_model": "gpt-5.6-terra"}
    )

    assert provider_for_settings(luna_settings).model == "gpt-5.6-luna"
    assert provider_for_settings(terra_settings).model == "gpt-5.6-terra"
    assert video_provider_for_settings(luna_settings) is None
    assert video_provider_for_settings(terra_settings) is None


def test_luna_provider_failure_does_not_retry_or_escalate_to_terra():
    calls = []

    def request(method, url, headers, payload, timeout):
        calls.append((url, payload["model"]))
        return 500, {}

    provider = OpenAITextProvider(
        "gpt-5.6-luna",
        api_key="test-only",
        requester=request,
    )
    try:
        provider.generate("grounded prompt")
    except ProviderUnavailable as exc:
        assert exc.code == "provider_error"
    else:
        raise AssertionError("Provider failure must be surfaced")

    assert calls == [("https://api.openai.com/v1/responses", "gpt-5.6-luna")]


def test_openai_credentials_cannot_be_routed_to_arbitrary_compatible_endpoints(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.example/v1")
    observed = []
    provider = OpenAITextProvider(
        "gpt-5.6-luna",
        api_key="test-only",
        requester=lambda method, url, headers, payload, timeout: (
            observed.append(url) or (200, {"data": []})
        ),
    )
    assert provider.health()["reachable"] is True
    assert observed == ["https://api.openai.com/v1/models"]

    for endpoint in (
        "https://example.com/v1",
        "http://api.openai.com/v1",
        "https://user:password@api.openai.com/v1",
        "https://api.openai.com/v1?proxy=true",
    ):
        try:
            OpenAITextProvider("gpt-5.6-luna", api_key="test-only", base_url=endpoint)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe OpenAI endpoint was accepted: {endpoint}")


def test_openai_image_adapter_requires_inline_output_and_background_safety_gate():
    import base64

    secret = "sk-image-test-secret"
    image_bytes = png((1024, 1536))
    calls = []

    def request(method, url, headers, payload, timeout):
        calls.append((url, headers, payload))
        if url.endswith("/images/generations"):
            return 200, {"data": [{"b64_json": base64.b64encode(image_bytes).decode()}]}
        return 200, {"output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": json.dumps({
                "background_only": True,
                "contains_product": False,
                "contains_packaging": False,
                "contains_logo": False,
                "contains_text": False,
                "contains_person": False,
            })}],
        }]}

    provider = OpenAIImageProvider(
        "gpt-image-2", api_key=secret, safety_model="gpt-5.6-luna", requester=request,
    )
    result = provider.generate_background("Muted editorial light.")
    image_call = calls[0]
    assert image_call[0] == "https://api.openai.com/v1/images/generations"
    assert image_call[1]["Authorization"] == f"Bearer {secret}"
    assert image_call[2]["model"] == "gpt-image-2"
    assert image_call[2]["size"] == "1024x1536"
    assert image_call[2]["n"] == 1
    assert "response_format" not in image_call[2]
    assert image_call[2]["output_format"] == "png"
    assert result.image_bytes == image_bytes
    assert secret not in json.dumps(image_call[2])
    assert secret not in repr(result)

    decision = provider.validate_background(result.image_bytes)
    safety_call = calls[-1]
    assert decision["background_only"] is True
    assert safety_call[0] == "https://api.openai.com/v1/responses"
    assert safety_call[2]["text"]["format"]["type"] == "json_schema"
    assert "data:image/png;base64," in json.dumps(safety_call[2])

    url_only = OpenAIImageProvider(
        "gpt-image-2", api_key="test-only",
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
        hosted_model="gpt-5.6-luna",
        daily_budget_usd=5,
        monthly_budget_usd=20,
    )
    provider = FakeProvider(
        safe_copy(product.title),
        name="openai",
        model="gpt-5.6-luna",
        usage=(1000, 500, 1500),
    )
    revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: provider,
    ).regenerate_copy(draft_id)

    usage = db.scalar(select(AIRequestTelemetry))
    settings = AISettingsService(proposal_service.session_factory).get()
    assert revision["estimated_cost_usd"] == 0.0008
    assert isinstance(revision["estimated_cost_usd"], float)
    assert revision["actual_cost_usd"] is None
    assert usage.estimated_cost_usd == Decimal("0.00080000")
    assert usage.actual_cost_usd is None
    assert isinstance(settings["daily_budget_usd"], float)
    assert settings["credentials_configured"] is True
    persisted = json.dumps([
        dict(row) for row in db.execute(select(AIRequestTelemetry.__table__)).mappings()
    ], default=str)
    assert secret not in persisted
    assert secret not in json.dumps(settings)
    db.close()


@pytest.mark.parametrize("failure_code", ["authentication_error", "provider_error"])
def test_hosted_provider_failure_is_surfaced_once_without_revision_or_escalation(
    monkeypatch, failure_code
):
    db, product, proposal_service, draft_id = prepared(f"hosted-{failure_code}")
    secret = "sk-hosted-failure-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-5.6-luna",
        daily_budget_usd=5,
        monthly_budget_usd=20,
    )
    draft = db.get(PinDraft, draft_id)
    original = (draft.title, draft.description, draft.status, draft.version)
    provider = FakeProvider(
        safe_copy(product.title),
        name="openai",
        model="gpt-5.6-luna",
        error=ProviderUnavailable(failure_code, "The hosted AI provider failed safely."),
    )
    selected_models = []

    def provider_factory(settings):
        selected_models.append(settings.hosted_model)
        return provider

    with pytest.raises(AIRegenerationError, match="failed safely"):
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=provider_factory,
        ).regenerate_copy(draft_id)

    db.expire_all()
    telemetry = db.scalar(select(AIRequestTelemetry))
    assert provider.calls == 1
    assert selected_models == ["gpt-5.6-luna"]
    assert (draft.title, draft.description, draft.status, draft.version) == original
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    assert db.scalar(select(func.count(ContentVersionSelection.id))) == 0
    assert db.scalar(select(func.count(AIGeneratedAsset.id))) == 0
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    assert telemetry.provider == "openai"
    assert telemetry.model == "gpt-5.6-luna"
    assert telemetry.draft_id == draft_id
    assert telemetry.success is False
    assert telemetry.failure_code == failure_code
    assert telemetry.fallback_used is False
    assert telemetry.fallback_reason is None
    persisted = json.dumps(
        [dict(row) for row in db.execute(select(AIRequestTelemetry.__table__)).mappings()],
        default=str,
    )
    assert secret not in persisted
    assert "Authorization" not in persisted
    assert "gpt-5.6-terra" not in persisted
    db.close()


@pytest.mark.parametrize(
    ("provider", "expected_code"),
    [
        (
            FakeProvider(
                "not-json",
                name="openai",
                model="gpt-5.6-luna",
            ),
            "invalid_response",
        ),
        (
            FakeProvider(
                safe_copy("unused"),
                name="openai",
                model="gpt-5.6-luna",
                error=ProviderUnavailable(
                    "invalid_response",
                    "The hosted AI provider returned no usable text.",
                ),
            ),
            "invalid_response",
        ),
        (
            FakeProvider(
                json.dumps({
                    "headline": "extra",
                    "title": "extra",
                    "description": "extra",
                    "alt_text": "extra",
                    "unexpected": "must fail",
                }),
                name="openai",
                model="gpt-5.6-luna",
            ),
            "invalid_response",
        ),
        (
            FakeProvider(
                json.dumps({
                    "headline": 123,
                    "title": "wrong type",
                    "description": "wrong type",
                    "alt_text": "wrong type",
                }),
                name="openai",
                model="gpt-5.6-luna",
            ),
            "invalid_response",
        ),
    ],
)
def test_hosted_malformed_or_refused_output_creates_no_revision(provider, expected_code):
    db, product, proposal_service, draft_id = prepared(f"hosted-{id(provider)}")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-5.6-luna",
        daily_budget_usd=5,
        monthly_budget_usd=20,
    )

    with pytest.raises(AIRegenerationError):
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=lambda _: provider,
        ).regenerate_copy(draft_id)

    telemetry = db.scalar(select(AIRequestTelemetry))
    assert provider.calls == 1
    assert telemetry.failure_code == expected_code
    assert telemetry.fallback_used is False
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    assert db.scalar(select(func.count(ContentVersionSelection.id))) == 0
    assert db.scalar(select(func.count(AIGeneratedAsset.id))) == 0
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_hosted_fact_safety_rejection_preserves_telemetry_without_revision():
    db, product, proposal_service, draft_id = prepared("hosted-fact-safety")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-5.6-luna",
        daily_budget_usd=5,
        monthly_budget_usd=20,
    )
    provider = FakeProvider(
        json.dumps({
            "headline": f"Number one {product.title}",
            "title": product.title,
            "description": f"{product.title} is the bestseller deal of the season.",
            "alt_text": f"{product.title} product image",
        }),
        name="openai",
        model="gpt-5.6-luna",
    )

    with pytest.raises(AIRegenerationError, match="unsupported claim"):
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=lambda _: provider,
        ).regenerate_copy(draft_id)

    telemetry = db.scalar(select(AIRequestTelemetry))
    assert provider.calls == 1
    assert telemetry.failure_code == "fact_safety_rejected"
    assert telemetry.validation_failure_reason == "fact_safety_rejected"
    assert telemetry.fallback_used is False
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    db.close()


def test_hosted_authentication_error_message_and_telemetry_do_not_leak_secret():
    db, product, proposal_service, draft_id = prepared("hosted-auth-secret")
    secret = "sk-never-persist-or-return"
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-5.6-luna",
        daily_budget_usd=5,
        monthly_budget_usd=20,
    )
    calls = []

    def rejected(method, url, headers, payload, timeout):
        calls.append((method, url, headers, payload, timeout))
        return 401, {"error": {"message": secret}}

    provider = OpenAITextProvider(
        "gpt-5.6-luna",
        api_key=secret,
        requester=rejected,
    )
    with pytest.raises(AIRegenerationError) as exc:
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=lambda _: provider,
        ).regenerate_copy(draft_id)

    assert len(calls) == 1
    assert calls[0][1] == "https://api.openai.com/v1/responses"
    assert secret not in str(exc.value)
    telemetry = db.scalar(select(AIRequestTelemetry))
    persisted = json.dumps(dict(db.execute(
        select(AIRequestTelemetry.__table__)
    ).mappings().one()), default=str)
    assert telemetry.failure_code == "authentication_error"
    assert telemetry.prompt_tokens is None
    assert telemetry.completion_tokens is None
    assert telemetry.total_tokens is None
    assert telemetry.actual_cost_usd is None
    assert secret not in persisted
    assert "Authorization" not in persisted
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
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
        hosted_model="gpt-5.6-terra",
        daily_budget_usd=0.000001,
        monthly_budget_usd=0.000001,
    )
    hosted = FakeProvider(safe_copy(product.title), name="openai", model="gpt-5.6-terra")
    service = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: hosted,
    )
    with pytest.raises(AIRegenerationError, match="budget controls"):
        service.regenerate_copy(draft_id)

    usage = db.scalar(select(AIRequestTelemetry))
    assert hosted.calls == 0
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    assert usage.failure_code == "budget_exceeded"
    assert usage.fallback_used is False
    assert usage.fallback_reason is None

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


def test_paid_zero_budget_and_invalid_pricing_fail_closed_without_provider_calls():
    db, product, proposal_service, draft_id = prepared("budget-fail-closed")
    provider = FakeProvider(safe_copy(product.title), name="openai", model="gpt-5.6-terra")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-5.6-terra",
        daily_budget_usd=10,
        monthly_budget_usd=100,
    )
    settings_row = db.scalar(select(AISettings))
    settings_row.pricing_metadata = {
        "gpt-5.6-terra": {"input_per_1m": "not-a-price", "output_per_1m": 12.00},
    }
    db.commit()
    with pytest.raises(AIRegenerationError, match="budget controls"):
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=lambda _: provider,
        ).regenerate_copy(draft_id)
    assert provider.calls == 0
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    first_usage = db.scalar(select(AIRequestTelemetry))
    assert first_usage.failure_code == "pricing_unavailable"
    assert first_usage.fallback_used is False

    AISettingsService(proposal_service.session_factory).update(
        daily_budget_usd=0,
        monthly_budget_usd=0,
    )
    AISettingsService(proposal_service.session_factory).update(hosted_model="gpt-5.6-luna")
    priced = FakeProvider(safe_copy(product.title), name="openai", model="gpt-5.6-luna")
    with pytest.raises(AIRegenerationError, match="budget controls"):
        AIRegenerationService(
            proposal_service.session_factory,
            provider_factory=lambda _: priced,
        ).regenerate_copy(draft_id)
    assert priced.calls == 0
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    failures = db.scalars(select(AIRequestTelemetry).order_by(AIRequestTelemetry.created_at)).all()
    assert [failure.failure_code for failure in failures] == [
        "pricing_unavailable",
        "budget_exceeded",
    ]
    db.close()


def test_paid_failure_is_returned_to_api_caller_as_conflict(monkeypatch):
    class FailedRegeneration:
        def regenerate_copy(self, draft_id):
            raise AIRegenerationError("The hosted AI provider credentials were rejected.")

    monkeypatch.setattr(
        proposal_routes,
        "AIRegenerationService",
        lambda: FailedRegeneration(),
    )
    with pytest.raises(HTTPException) as exc:
        proposal_routes.regenerate_proposal(
            "proposal-id",
            RegenerationRequest(kind="copy", count=1),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "The hosted AI provider credentials were rejected."


def test_terra_pricing_is_explicitly_selectable_and_costed():
    db, product, proposal_service, draft_id = prepared("terra-pricing")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        hosted_model="gpt-5.6-terra",
        daily_budget_usd=5,
        monthly_budget_usd=20,
    )
    provider = FakeProvider(
        safe_copy(product.title),
        name="openai",
        model="gpt-5.6-terra",
        usage=(1000, 500, 1500),
    )

    revision = AIRegenerationService(
        proposal_service.session_factory,
        provider_factory=lambda _: provider,
    ).regenerate_copy(draft_id)

    usage = db.scalar(select(AIRequestTelemetry))
    assert provider.calls == 1
    assert usage.model == "gpt-5.6-terra"
    assert revision["estimated_cost_usd"] == 0.008
    assert usage.estimated_cost_usd == Decimal("0.00800000")
    assert revision["actual_cost_usd"] is None
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
