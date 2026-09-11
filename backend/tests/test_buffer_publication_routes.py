import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api.routes import publications as routes


class FakeDb:
    def __init__(self, row=None):
        self.row = row

    def get(self, model, identifier):
        return self.row if self.row and self.row.id == identifier else None


def request():
    return SimpleNamespace(cookies={})


def test_buffer_authorization_is_fixed_to_buffer(monkeypatch):
    row = SimpleNamespace(id="publication")
    auth = SimpleNamespace(id="auth", status="ACTIVE", authorized_by="operator",
                           authorized_at="now", expires_at="later", confirmation_text_version="CONFIRM_DISPATCH_V1")
    create = Mock(return_value=auth)
    monkeypatch.setattr(routes, "current_user", lambda request: "operator")
    monkeypatch.setattr(routes, "create_authorization", create)
    result = routes.authorize_buffer(
        row.id, request(), routes.DispatchAuthorizationRequest(
            confirmed=True, confirmation_text_version=routes.CONFIRMATION_TEXT_VERSION,
        ), FakeDb(row),
    )
    assert result["id"] == "auth"
    assert create.call_args.kwargs == {"actor": "operator", "dispatch_provider": "buffer"}


def test_buffer_routes_require_auth_before_delegation(monkeypatch):
    row = SimpleNamespace(id="publication")
    monkeypatch.setattr(routes, "current_user", lambda request: None)
    preflight = AsyncMock(side_effect=AssertionError("preflight reached"))
    dispatch = AsyncMock(side_effect=AssertionError("dispatch reached"))
    monkeypatch.setattr(routes, "build_buffer_execution_evidence", preflight)
    monkeypatch.setattr(routes.buffer_manual_publication_dispatch, "dispatch_buffer", dispatch)
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.publish_buffer(row.id, request(), FakeDb(row)))
    assert error.value.status_code == 401
    preflight.assert_not_called()
    dispatch.assert_not_called()


def test_buffer_publish_preflights_then_delegates_once(monkeypatch):
    row = SimpleNamespace(id="publication")
    evidence = object()
    order = []
    monkeypatch.setattr(routes, "current_user", lambda request: "operator")
    async def preflight(db, publication):
        order.append("preflight")
        return evidence
    async def dispatch(db, publication, *, execution_evidence):
        order.append("dispatch")
        assert execution_evidence is evidence
    monkeypatch.setattr(routes, "build_buffer_execution_evidence", preflight)
    monkeypatch.setattr(routes.buffer_manual_publication_dispatch, "dispatch_buffer", dispatch)
    monkeypatch.setattr(routes, "_dto", lambda db, publication: {"id": publication.id})
    assert asyncio.run(routes.publish_buffer(row.id, request(), FakeDb(row))) == {"id": row.id}
    assert order == ["preflight", "dispatch"]


def test_direct_publish_remains_isolated_from_buffer(monkeypatch):
    row = SimpleNamespace(id="publication")
    direct = AsyncMock()
    buffer = AsyncMock(side_effect=AssertionError("buffer dispatch reached"))
    monkeypatch.setattr(routes.manual_publication_dispatch, "dispatch_publication", direct)
    monkeypatch.setattr(routes.buffer_manual_publication_dispatch, "dispatch_buffer", buffer)
    monkeypatch.setattr(routes, "_dto", lambda db, publication: {"id": publication.id})
    assert asyncio.run(routes.publish(row.id, FakeDb(row))) == {"id": row.id}
    direct.assert_awaited_once()
    buffer.assert_not_called()


def test_authorization_request_forbids_client_owned_execution_fields():
    with pytest.raises(ValueError):
        routes.DispatchAuthorizationRequest(
            confirmed=True,
            confirmation_text_version=routes.CONFIRMATION_TEXT_VERSION,
            authorization_id="client-owned",
            buffer_organization_id="client-owned",
            execution_evidence={"ready": True},
        )