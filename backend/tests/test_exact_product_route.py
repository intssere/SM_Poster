from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import proposals as proposal_routes
from app.services.creative_rendering import CreativeRenderError


def route_client():
    app = FastAPI()
    app.include_router(proposal_routes.router, prefix="/api")
    return TestClient(app)


def test_exact_product_route_returns_bounded_review_result_with_publishing_disabled(monkeypatch):
    calls = {}

    class FakeRenderer:
        pass

    class FakeService:
        def generate_controlled_batch(self, **kwargs):
            calls.update(kwargs)
            return {
                "products_selected": 1,
                "proposals_generated": 2,
                "representative_proposals": [
                    {"id": "draft-1", "approval_status": "REVIEW"},
                    {"id": "draft-2", "approval_status": "REVIEW"},
                ],
                "rendered_creatives": [
                    {"id": "creative-1", "status": "RENDERED"},
                    {"id": "creative-2", "status": "RENDERED"},
                ],
            }

    monkeypatch.setattr(proposal_routes, "PinProposalService", FakeService)
    monkeypatch.setattr(proposal_routes, "CreativeRenderService", FakeRenderer)

    response = route_client().post(
        "/api/pins/generate/exact-product",
        json={
            "product_id": "00000000-0000-0000-0000-000000000001",
            "max_proposals_per_product": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["products_selected"] == 1
    assert payload["proposals_generated"] == 2
    assert payload["publishing_enabled"] is False
    assert {item["approval_status"] for item in payload["representative_proposals"]} == {"REVIEW"}
    assert calls == {
        "product_limit": 1,
        "max_proposals_per_product": 2,
        "exact_product_id": "00000000-0000-0000-0000-000000000001",
        "renderer": calls["renderer"],
    }
    assert isinstance(calls["renderer"], FakeRenderer)


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            ValueError("Exact product is unavailable or lacks eligible persisted Shopify media and provenance."),
            "unavailable or lacks eligible persisted Shopify media",
        ),
        (
            ValueError("The exact product already has this deterministic proposal."),
            "already has this deterministic proposal",
        ),
        (
            CreativeRenderError("Mocked source download failed."),
            "Mocked source download failed",
        ),
    ],
)
def test_exact_product_route_maps_expected_generation_failures_to_422(monkeypatch, error, detail):
    class FakeRenderer:
        pass

    class FailingService:
        def generate_controlled_batch(self, **kwargs):
            raise error

    monkeypatch.setattr(proposal_routes, "PinProposalService", FailingService)
    monkeypatch.setattr(proposal_routes, "CreativeRenderService", FakeRenderer)

    response = route_client().post(
        "/api/pins/generate/exact-product",
        json={
            "product_id": "00000000-0000-0000-0000-000000000001",
            "max_proposals_per_product": 1,
        },
    )

    assert response.status_code == 422
    assert detail in response.json()["detail"]