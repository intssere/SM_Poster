"""API contract only: no storage, provider, or production calls."""
from uuid import uuid4

from test_publications_api import _client


def test_candidate_preflight_requires_auth_and_strict_query(monkeypatch):
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("REPLIT_DEV_DOMAIN", raising=False)
    approval_id, board_id = str(uuid4()), str(uuid4())
    calls = []

    def fake_preflight(db, *, approval_id, pinterest_board_record_id):
        calls.append((approval_id, pinterest_board_record_id))
        return {
            "approval_id": approval_id,
            "board_record_id": pinterest_board_record_id,
            "asset_integrity": "VERIFIED", "quality": "PASS",
            "duplicate": "SAFE_TO_CONTINUE", "routing": "CURRENT",
            "eligible": True, "status": "ELIGIBLE", "reason_codes": [],
        }

    monkeypatch.setattr("app.api.routes.publications.preflight_candidate", fake_preflight)
    with _client(monkeypatch) as client:
        path = "/api/publications/candidate-preflight"
        params = {"approval_id": approval_id, "pinterest_board_record_id": board_id}
        assert client.get(path, params=params).status_code == 401
        login = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret"},
            headers={"Origin": "http://localhost:5000"},
        )
        assert login.status_code == 200
        assert client.get(path, params={**params, "scheduled_for": "2026-09-26T00:00:00Z"}).status_code == 422
        assert client.get(path, params={**params, "approval_id": "not-a-uuid"}).status_code == 422
        response = client.get(path, params=params)
        assert response.status_code == 200
        assert response.json() == fake_preflight(None, approval_id=approval_id, pinterest_board_record_id=board_id)
        assert calls == [(approval_id, board_id), (approval_id, board_id)]
        assert "title" not in response.text
        assert "media_url" not in response.text