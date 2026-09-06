import asyncio
from dataclasses import asdict
import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations.buffer.gateway import (
    BufferGateway, BufferPinterestPostPayload, BufferConfigurationError,
    BufferDefinitiveRejection, BufferAmbiguousFailure, BufferReadError,
)
from app.services.pinterest_oauth import READ_SCOPES


SECRET = "fake-buffer-secret-marker-not-a-real-key"


def settings(**overrides):
    values = dict(DATABASE_URL="sqlite+pysqlite:///:memory:", buffer_api_key=SECRET,
                  buffer_api_base="https://api.buffer.com", buffer_organization_id="org-fixture",
                  buffer_pinterest_channel_id="channel-fixture", publishing_enabled=False,
                  buffer_publishing_enabled=False)
    values.update(overrides)
    return Settings(_env_file=None, **values)


def payload():
    return BufferPinterestPostPayload("channel-fixture", "board-fixture", 'Approved "title"',
        "Exact description\nSecond line", "https://diamondshelf.us/products/item?utm_source=pinterest",
        "https://cdn.shopify.com/image.jpg", "Authentic product alt text")


def post():
    return {"id": "buffer-post-fixture", "status": "sending", "channelId": "channel-fixture",
            "createdAt": "2026-09-06T00:00:00Z", "dueAt": None, "sentAt": None, "externalLink": None}


def channel():
    return {"id": "channel-fixture", "name": "Fixture", "service": "pinterest",
            "isDisconnected": False, "isLocked": False,
            "metadata": {"boards": [{"serviceId": "board-fixture", "name": "Board"}]}}


def run_write(handler, config=None, value=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BufferGateway(config or settings(publishing_enabled=True, buffer_publishing_enabled=True), client=client).create_pinterest_post(value or payload())
    return asyncio.run(run())


def test_protected_configuration_defaults():
    expected = {"buffer_api_key": None, "buffer_api_base": "https://api.buffer.com",
                "buffer_organization_id": None, "buffer_pinterest_channel_id": None,
                "buffer_publishing_enabled": False, "publishing_enabled": False,
                "pinterest_write_scope_enabled": False, "pinterest_single_pin_pilot_enabled": False,
                "pinterest_single_pin_pilot_publication_id": "",
                "pinterest_single_pin_pilot_publication_fingerprint": "",
                "pinterest_single_pin_pilot_request_fingerprint": ""}
    for name, value in expected.items():
        assert Settings.model_fields[name].default == value
    assert READ_SCOPES == ("user_accounts:read", "boards:read", "pins:read")
    assert SECRET not in repr(settings())
    assert "buffer_api_key" not in settings().model_dump()
    assert SECRET not in repr(BufferGateway(settings()))


@pytest.mark.parametrize("global_flag,buffer_flag", [(False, False), (False, True), (True, False)])
def test_write_requires_both_flags_before_any_http(global_flag, buffer_flag):
    calls = []
    def handler(request):
        calls.append(request)
        raise AssertionError("Network must not be reached")
    with pytest.raises(BufferConfigurationError, match="BUFFER_PUBLISHING_DISABLED"):
        run_write(handler, settings(publishing_enabled=global_flag, buffer_publishing_enabled=buffer_flag))
    assert calls == []


@pytest.mark.parametrize("override", [dict(buffer_api_key=None), dict(buffer_api_key=""),
    dict(buffer_api_key="invalid\nheader"), dict(buffer_api_base="https://untrusted.invalid"),
    dict(buffer_pinterest_channel_id=None), dict(buffer_pinterest_channel_id="other")])
def test_invalid_configuration_prevents_http(override):
    calls = []
    with pytest.raises(BufferConfigurationError):
        run_write(lambda req: calls.append(req), settings(publishing_enabled=True, buffer_publishing_enabled=True, **override))
    assert calls == []


def test_exact_single_mocked_mutation_and_safe_pending_result(caplog):
    calls = []
    def handler(request):
        calls.append(1)
        assert str(request.url) == "https://api.buffer.com"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer " + SECRET
        data = json.loads(request.content)
        assert SECRET not in request.content.decode()
        assert "PostActionSuccess" in data["query"] and "MutationError" in data["query"]
        assert payload().title not in data["query"]
        assert data["variables"] == {"input": payload().to_input()}
        assert request.extensions["timeout"] == {"connect": 5.0, "read": 20.0, "write": 10.0, "pool": 5.0}
        return httpx.Response(200, json={"data": {"createPost": {"__typename": "PostActionSuccess", "post": {**post(), "raw_body": SECRET}}}})
    result = run_write(handler)
    assert calls == [1]
    assert asdict(result) == dict(buffer_post_id="buffer-post-fixture", status="sending", channel_id="channel-fixture",
        created_at="2026-09-06T00:00:00Z", due_at=None, sent_at=None, external_link=None)
    assert SECRET not in repr(result) + caplog.text
    assert not hasattr(result, "pinterest_pin_id")


@pytest.mark.parametrize("code,status", [("GRAPHQL_VALIDATION_FAILED", 400), ("UNAUTHORIZED", 401), ("FORBIDDEN", 403)])
def test_proven_http_rejection_no_retry(code, status):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"errors": [{"message": SECRET, "extensions": {"code": code}}]})
    with pytest.raises(BufferDefinitiveRejection, match="^BUFFER_REQUEST_REJECTED$") as error:
        run_write(handler)
    assert calls == [1]
    assert SECRET not in str(error.value)


def test_explicit_mutation_error_no_retry():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"data": {"createPost": {"__typename": "ValidationError", "errorMessage": SECRET}}})
    with pytest.raises(BufferDefinitiveRejection, match="^BUFFER_MUTATION_REJECTED$"):
        run_write(handler)
    assert calls == [1]


@pytest.mark.parametrize("failure", ["timeout", "transport", "500", "invalid-json", "missing-id", "empty-id", "conflict",
    "top-errors", "unknown-client-error", "redirect", "secret-id", "wrong-channel", "malformed-post", "unknown-result"])
def test_uncertain_outcome_never_retries_or_exposes_secret(failure, caplog):
    calls = []
    def handler(request):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "transport":
            raise httpx.ConnectError(SECRET, request=request)
        if failure == "500":
            return httpx.Response(500, text=SECRET)
        if failure == "invalid-json":
            return httpx.Response(200, text=SECRET)
        if failure == "unknown-client-error":
            return httpx.Response(400, json={"message": SECRET})
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://untrusted.invalid"})
        body = {"data": {"createPost": {"__typename": "PostActionSuccess", "post": post()}}}
        result = body["data"]["createPost"]
        if failure == "missing-id":
            del result["post"]["id"]
        elif failure == "empty-id":
            result["post"]["id"] = ""
        elif failure == "secret-id":
            result["post"]["id"] = SECRET
        elif failure == "wrong-channel":
            result["post"]["channelId"] = "other"
        elif failure == "conflict":
            result["errorMessage"] = SECRET
        elif failure == "top-errors":
            body["errors"] = [{"message": SECRET}]
        elif failure == "malformed-post":
            result["post"] = []
        elif failure == "unknown-result":
            body["data"]["createPost"] = None
        return httpx.Response(200, json=body)
    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)
    assert calls == [1]
    assert SECRET not in str(error.value) + repr(error.value) + caplog.text


def test_read_queries_normalize_exact_ids_and_bounded_empty_history():
    calls = []
    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        assert "mutation" not in data["query"]
        if "account" in data["query"]:
            body = {"account": {"organizations": [{"id": "org-fixture", "name": "Organization", "secret": SECRET}]}}
        elif "query Channels" in data["query"]:
            assert data["variables"] == {"input": {"organizationId": "org-fixture"}}
            body = {"channels": [channel()]}
        elif "query Channel(" in data["query"]:
            assert data["variables"] == {"input": {"id": "channel-fixture"}}
            body = {"channel": channel()}
        else:
            assert data["variables"] == {"first": 5, "input": {"organizationId": "org-fixture",
                "filter": {"channelIds": ["channel-fixture"], "status": ["sent"]},
                "sort": [{"field": "createdAt", "direction": "desc"}]}}
            body = {"posts": {"edges": []}}
        return httpx.Response(200, json={"data": body})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = BufferGateway(settings(), client=client)
            assert await gateway.organizations() == [{"id": "org-fixture", "name": "Organization"}]
            channels = await gateway.channels("org-fixture")
            assert channels[0]["boards"] == [{"serviceId": "board-fixture", "name": "Board"}]
            assert await gateway.channel("channel-fixture") == channels[0]
            assert await gateway.recent_posts("org-fixture", "channel-fixture", first=5) == []
    asyncio.run(run())
    assert len(calls) == 4


@pytest.mark.parametrize("first,status", [(0, "sent"), (101, "sent"), (True, "sent"), (10, "invalid")])
def test_post_filter_invalid_zero_http(first, status):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected HTTP"))) as client:
            with pytest.raises(BufferConfigurationError):
                await BufferGateway(settings(), client=client).recent_posts("org", "channel", first=first, status=status)
    asyncio.run(run())


@pytest.mark.parametrize("data", [{"channel": None}, {"channel": {**channel(), "isLocked": "false"}},
    {"channel": {**channel(), "name": SECRET}}, {"channel": {**channel(), "metadata": {"boards": None}}}])
def test_malformed_reads_are_sanitized(data):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": data}))) as client:
            with pytest.raises(BufferReadError, match="^BUFFER_READ_INVALID$"):
                await BufferGateway(settings(), client=client).channel("channel-fixture")
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "500", "errors", "invalid-json"])
def test_read_failures_are_not_publication_outcomes(failure):
    calls = []
    def handler(request):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if failure == "500":
            return httpx.Response(500, text=SECRET)
        if failure == "invalid-json":
            return httpx.Response(200, text=SECRET)
        return httpx.Response(200, json={"errors": [{"message": SECRET}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(BufferReadError, match="^BUFFER_READ_FAILED$"):
                await BufferGateway(settings(), client=client).organizations()
    asyncio.run(run())
    assert calls == [1]


def test_recent_sent_post_is_allowlisted():
    row = {**post(), "status": "sent", "sentAt": "2026-09-06T01:00:00Z",
           "externalLink": "https://www.pinterest.com/pin/123/", "raw": SECRET}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
            json={"data": {"posts": {"edges": [{"node": row}]}}}))) as client:
            results = await BufferGateway(settings(), client=client).recent_posts("org-fixture", "channel-fixture")
            assert len(results) == 1
            assert results[0].external_link == row["externalLink"]
            assert SECRET not in repr(results)
    asyncio.run(run())


def test_api_key_cannot_enter_query_variables():
    value = payload()
    value = BufferPinterestPostPayload(value.channel_id, value.board_service_id, SECRET,
        value.text, value.url, value.image_url, value.alt_text)
    with pytest.raises(BufferConfigurationError, match="BUFFER_SECRET_IN_PAYLOAD"):
        run_write(lambda r: pytest.fail("Unexpected HTTP"), value=value)


@pytest.mark.parametrize("display_name", ["Pinterest Display Name", None])
def test_channel_display_name_preserved(display_name):
    async def run():
        def handler(request):
            data = json.loads(request.content)
            assert "displayName" in data["query"]
            row = {**channel(), "displayName": display_name}
            body = {"channels": [row]} if "query Channels(" in data["query"] else {"channel": row}
            return httpx.Response(200, json={"data": body})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = BufferGateway(settings(), client=client)
            assert (await gateway.channel("channel-fixture"))["displayName"] == display_name
            assert (await gateway.channels("org-fixture"))[0]["displayName"] == display_name
    asyncio.run(run())


@pytest.mark.parametrize("display_name", [123, True, [], {}, "x" * 2049, "bad\nname", SECRET])
def test_invalid_channel_display_name_fails_closed(display_name):
    async def run():
        def handler(request):
            data = json.loads(request.content)
            row = {**channel(), "displayName": display_name}
            body = {"channels": [row]} if "query Channels(" in data["query"] else {"channel": row}
            return httpx.Response(200, json={"data": body})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = BufferGateway(settings(), client=client)
            with pytest.raises(BufferReadError, match="^BUFFER_READ_INVALID$"):
                await gateway.channel("channel-fixture")
            with pytest.raises(BufferReadError, match="^BUFFER_READ_INVALID$"):
                await gateway.channels("org-fixture")
    asyncio.run(run())


@pytest.mark.parametrize("status,code", [(200, "UNAUTHORIZED"), (401, "UNAUTHENTICATED"), (404, "NOT_FOUND")])
def test_auth_code_does_not_broaden_definitive_outcomes(status, code):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"errors": [{"message": SECRET, "extensions": {"code": code}}]})
    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$"):
        run_write(handler)
    assert calls == [1]
