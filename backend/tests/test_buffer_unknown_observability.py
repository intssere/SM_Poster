import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.integrations.buffer.gateway import (
    BufferGateway,
    BufferPinterestPostPayload,
    BufferAmbiguousFailure,
    BufferDefinitiveRejection,
)
from app.models.domain import PinPublication, PublicationAttempt
from app.services import buffer_manual_publication_dispatch as dispatch
from test_buffer_phase3b_execution_gate import _fresh_persisted_case, NOW, SECRET


GATEWAY_SECRET = "fake-observability-secret-not-a-real-key"


def settings():
    return Settings(
        _env_file=None,
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        buffer_api_key=GATEWAY_SECRET,
        buffer_api_base="https://api.buffer.com",
        buffer_organization_id="org-fixture",
        buffer_pinterest_channel_id="channel-fixture",
        publishing_enabled=True,
        buffer_publishing_enabled=True,
    )


def payload():
    return BufferPinterestPostPayload(
        "channel-fixture", "board-fixture", "Approved title", "Approved text",
        "https://diamondshelf.us/products/item", "https://cdn.shopify.com/image.jpg", "Approved alt text",
    )


def run_write(handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BufferGateway(settings(), client=client).create_pinterest_post(payload())
    return asyncio.run(run())


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("connect-timeout", ("connect_timeout", "connecting", False, None)),
        ("write-timeout", ("write_timeout", "sending_request", False, None)),
        ("read-timeout", ("read_timeout", "waiting_for_response", False, None)),
        ("500", ("http_non_200", "http_response", True, 500)),
        ("invalid-json", ("response_decode_error", "decoding_response", True, 200)),
        ("graphql-errors", ("graphql_envelope_invalid", "validating_graphql_response", True, 200)),
        ("missing-id", ("mutation_result_invalid", "normalizing_mutation_result", True, 200)),
    ],
)
def test_gateway_ambiguous_failure_is_structured_sanitized_and_never_retried(failure, expected):
    calls = []

    def handler(request):
        calls.append(1)
        if failure == "connect-timeout":
            raise httpx.ConnectTimeout(GATEWAY_SECRET, request=request)
        if failure == "write-timeout":
            raise httpx.WriteTimeout(GATEWAY_SECRET, request=request)
        if failure == "read-timeout":
            raise httpx.ReadTimeout(GATEWAY_SECRET, request=request)
        if failure == "500":
            return httpx.Response(500, text=GATEWAY_SECRET)
        if failure == "invalid-json":
            return httpx.Response(200, text=GATEWAY_SECRET)
        if failure == "graphql-errors":
            return httpx.Response(200, json={"errors": [{"message": GATEWAY_SECRET}]})
        post = {
            "status": "sending", "channelId": "channel-fixture",
            "createdAt": "2026-09-12T13:00:00Z", "dueAt": None,
            "sentAt": None, "externalLink": None,
        }
        return httpx.Response(200, json={"data": {"createPost": {
            "__typename": "PostActionSuccess", "post": post,
        }}})

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as caught:
        run_write(handler)
    assert calls == [1]
    diagnostic = caught.value.safe_diagnostic()
    assert (diagnostic["failure_code"], diagnostic["phase"],
            diagnostic["response_received"], diagnostic["http_status"]) == expected
    assert diagnostic["outcome_class"] == "ambiguous"
    assert diagnostic["request_send_state"] == "unknown"
    assert GATEWAY_SECRET not in str(caught.value) + repr(caught.value) + json.dumps(diagnostic)


def test_definitive_rejection_has_safe_diagnostic_and_one_http_call():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"errors": [{
            "message": GATEWAY_SECRET, "extensions": {"code": "UNAUTHORIZED"},
        }]})

    with pytest.raises(BufferDefinitiveRejection, match="^BUFFER_REQUEST_REJECTED$") as caught:
        run_write(handler)
    assert calls == [1]
    assert caught.value.safe_diagnostic() == {
        "outcome_class": "definitive_rejection",
        "failure_code": "definitive_http_rejection",
        "phase": "http_response",
        "response_received": True,
        "http_status": 401,
        "request_send_state": "unknown",
    }
    assert GATEWAY_SECRET not in repr(caught.value) + json.dumps(caught.value.safe_diagnostic())


def _run_dispatch_case(tmp_path, monkeypatch, gateway, caplog):
    factory, engine, pid, _aid, _bid, configured, evidence = _fresh_persisted_case(tmp_path)
    try:
        with factory() as db:
            publication = db.get(PinPublication, pid)

            async def verify(*args, **kwargs):
                return None

            monkeypatch.setattr(dispatch, "verify_destination", verify)
            result = asyncio.run(dispatch.dispatch_buffer(
                db, publication, settings=configured, gateway=gateway,
                now=NOW, execution_evidence=evidence,
            ))
            attempt = db.scalar(select(PublicationAttempt).where(
                PublicationAttempt.publication_id == pid,
                PublicationAttempt.dispatch_provider == "buffer",
            ))
            return result, attempt
    finally:
        engine.dispose()


def test_dispatch_persists_sanitized_ambiguous_diagnostic_and_structured_log(tmp_path, monkeypatch, caplog):
    class Gateway:
        async def create_pinterest_post(self, value):
            raise BufferAmbiguousFailure(
                "BUFFER_RESPONSE_UNCERTAIN",
                failure_code="read_timeout",
                phase="waiting_for_response",
                response_received=False,
            )

    caplog.set_level("INFO", logger=dispatch.__name__)
    publication, attempt = _run_dispatch_case(tmp_path, monkeypatch, Gateway(), caplog)
    assert publication.status.value == "PUBLISH_UNKNOWN"
    assert attempt.status == "UNKNOWN"
    diagnostic = attempt.safe_response_metadata["mutation"]
    assert diagnostic["outcome_class"] == "ambiguous"
    assert diagnostic["failure_code"] == "read_timeout"
    assert diagnostic["phase"] == "waiting_for_response"
    assert diagnostic["response_received"] is False
    assert diagnostic["http_status"] is None
    assert diagnostic["request_send_state"] == "unknown"
    assert diagnostic["observed_at"].endswith("+00:00")
    records = [record for record in caplog.records if record.getMessage() == "buffer_mutation_outcome"]
    assert len(records) == 1
    assert records[0].buffer_failure_code == "read_timeout"
    assert records[0].buffer_failure_phase == "waiting_for_response"
    assert records[0].buffer_provider == "buffer"
    assert SECRET not in json.dumps(attempt.safe_response_metadata) + repr(records[0].__dict__)


def test_unexpected_mutation_exception_stays_unknown_without_exception_text(tmp_path, monkeypatch, caplog):
    class Gateway:
        async def create_pinterest_post(self, value):
            raise RuntimeError(SECRET)

    caplog.set_level("INFO", logger=dispatch.__name__)
    publication, attempt = _run_dispatch_case(tmp_path, monkeypatch, Gateway(), caplog)
    assert publication.status.value == "PUBLISH_UNKNOWN"
    assert attempt.status == "UNKNOWN"
    diagnostic = attempt.safe_response_metadata["mutation"]
    assert diagnostic["outcome_class"] == "ambiguous"
    assert diagnostic["failure_code"] == "unexpected_exception"
    assert diagnostic["phase"] == "mutation_boundary"
    assert SECRET not in json.dumps(attempt.safe_response_metadata) + caplog.text
