import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.integrations.buffer.gateway import (
    BufferAmbiguousFailure,
    BufferDefinitiveRejection,
    BufferGateway,
    BufferPinterestPostPayload,
)


SECRET = "fake-buffer-secret-marker-not-a-real-key"


def settings():
    return Settings(
        _env_file=None,
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        buffer_api_key=SECRET,
        buffer_api_base="https://api.buffer.com",
        buffer_organization_id="org-fixture",
        buffer_pinterest_channel_id="channel-fixture",
        publishing_enabled=True,
        buffer_publishing_enabled=True,
    )


def payload():
    return BufferPinterestPostPayload(
        "channel-fixture",
        "board-fixture",
        "Approved title",
        "Approved description",
        "https://diamondshelf.us/products/item?utm_source=pinterest",
        "https://cdn.shopify.com/image.jpg",
        "Authentic product alt text",
    )


def post():
    return {
        "id": "buffer-post-fixture",
        "status": "sending",
        "channelId": "channel-fixture",
        "createdAt": "2026-09-13T12:00:00Z",
        "dueAt": None,
        "sentAt": None,
        "externalLink": None,
    }


def run_write(handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = BufferGateway(settings(), client=client)
            return await gateway.create_pinterest_post(payload())

    return asyncio.run(run())


@pytest.mark.parametrize(
    "code",
    ["GRAPHQL_PARSE_FAILED", "GRAPHQL_VALIDATION_FAILED"],
)
def test_http_200_proven_request_shape_graphql_rejection_is_definitive(code, caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{"message": SECRET, "extensions": {"code": code}}],
            },
        )

    with pytest.raises(BufferDefinitiveRejection, match="^BUFFER_REQUEST_REJECTED$") as error:
        run_write(handler)

    assert calls == [1]
    assert error.value.safe_diagnostic() == {
        "outcome_class": "definitive_rejection",
        "failure_code": "graphql_top_level_rejection",
        "phase": "validating_graphql_response",
        "response_received": True,
        "http_status": 200,
        "request_send_state": "unknown",
        "graphql_error_count": 1,
        "graphql_error_codes": [code],
    }
    assert SECRET not in str(error.value) + repr(error.value) + caplog.text


@pytest.mark.parametrize(
    "code",
    ["UNAUTHORIZED", "FORBIDDEN", "UNEXPECTED", "RATE_LIMITED", "UNAUTHENTICATED", "INTERNAL_SERVER_ERROR"],
)
def test_http_200_auth_unknown_or_system_graphql_error_remains_ambiguous(code, caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{"message": SECRET, "extensions": {"code": code}}],
            },
        )

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)

    assert calls == [1]
    diagnostic = error.value.safe_diagnostic()
    assert diagnostic["failure_code"] == "graphql_top_level_error"
    assert diagnostic["outcome_class"] == "ambiguous"
    assert diagnostic["http_status"] == 200
    assert diagnostic["graphql_error_count"] == 1
    assert diagnostic["graphql_error_codes"] == [code]
    assert SECRET not in str(error.value) + repr(error.value) + caplog.text


def test_http_200_partial_data_plus_known_error_remains_ambiguous():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": {"createPost": {"__typename": "PostActionSuccess", "post": post()}},
                "errors": [{"message": SECRET, "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}],
            },
        )

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)

    assert calls == [1]
    diagnostic = error.value.safe_diagnostic()
    assert diagnostic["failure_code"] == "graphql_top_level_error"
    assert diagnostic["graphql_error_codes"] == ["GRAPHQL_VALIDATION_FAILED"]
    assert diagnostic["graphql_error_count"] == 1


def test_http_200_safe_graphql_code_and_structural_path_are_normalized_and_retained(caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{
                    "message": SECRET,
                    "path": ["createPost", "post", 0, "id"],
                    "extensions": {"code": "forbidden", "providerDetail": SECRET},
                }],
            },
        )

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)

    assert calls == [1]
    diagnostic = error.value.safe_diagnostic()
    assert diagnostic["failure_code"] == "graphql_top_level_error"
    assert diagnostic["graphql_error_count"] == 1
    assert diagnostic["graphql_error_codes"] == ["FORBIDDEN"]
    assert diagnostic["graphql_error_paths"] == [["createPost", "post", 0, "id"]]
    assert SECRET not in str(error.value) + repr(error.value) + caplog.text + repr(diagnostic)


def test_invalid_graphql_path_is_not_persisted_but_safe_code_remains(caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{
                    "message": SECRET,
                    "path": ["createPost", SECRET],
                    "extensions": {"code": "FORBIDDEN"},
                }],
            },
        )

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)

    diagnostic = error.value.safe_diagnostic()
    assert calls == [1]
    assert diagnostic["graphql_error_codes"] == ["FORBIDDEN"]
    assert "graphql_error_paths" not in diagnostic
    assert SECRET not in repr(diagnostic) + caplog.text


@pytest.mark.parametrize(
    "errors",
    [
        "not-a-list",
        ["not-an-error-object"],
    ],
)
def test_http_200_malformed_graphql_error_envelope_remains_ambiguous(errors):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"data": None, "errors": errors})

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)

    assert calls == [1]
    assert error.value.safe_diagnostic()["failure_code"] == "graphql_envelope_invalid"
    assert error.value.safe_diagnostic()["outcome_class"] == "ambiguous"


def test_http_200_graphql_error_without_extensions_remains_envelope_invalid_and_redacted(caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"data": None, "errors": [{"message": SECRET}]})

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)

    assert calls == [1]
    assert error.value.safe_diagnostic()["failure_code"] == "graphql_envelope_invalid"
    assert SECRET not in str(error.value) + repr(error.value) + caplog.text


def test_malformed_graphql_code_is_never_persisted(caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={
            "data": None,
            "errors": [{"message": SECRET, "extensions": {"code": "FORBIDDEN\n" + SECRET}}],
        })

    with pytest.raises(BufferAmbiguousFailure, match="^BUFFER_RESPONSE_UNCERTAIN$") as error:
        run_write(handler)

    diagnostic = error.value.safe_diagnostic()
    assert calls == [1]
    assert diagnostic["failure_code"] == "graphql_envelope_invalid"
    assert "graphql_error_codes" not in diagnostic
    assert SECRET not in repr(diagnostic) + caplog.text


def test_http_200_success_still_normalizes_exact_post():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={"data": {"createPost": {"__typename": "PostActionSuccess", "post": post()}}},
        )

    result = run_write(handler)

    assert calls == [1]
    assert result.buffer_post_id == "buffer-post-fixture"
    assert result.channel_id == "channel-fixture"
    assert result.status == "sending"


def test_typed_mutation_error_remains_definitive():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": {
                    "createPost": {
                        "__typename": "ValidationError",
                        "errorMessage": SECRET,
                    }
                }
            },
        )

    with pytest.raises(BufferDefinitiveRejection, match="^BUFFER_MUTATION_REJECTED$") as error:
        run_write(handler)

    assert calls == [1]
    assert error.value.safe_diagnostic()["failure_code"] == "explicit_mutation_rejection"
