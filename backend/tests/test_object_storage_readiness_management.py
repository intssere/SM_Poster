"""Management integration tests: fake child only, no application lifespan."""
from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import object_storage_readiness as routes
from app.core import auth, readiness_execution_auth as grants
from app.core.config import Settings
from app.middleware import AdminAuthMiddleware
from app.services import readiness_execution as execution
from app.services.deployment_attestation import BuildProvenance
from app.services.readiness_execution_contract import ReadinessError

PATH = "/api/internal/operations/object-storage-readiness"
ORIGIN = "http://localhost:5000"


@pytest.fixture
def management(monkeypatch, tmp_path):
    overlay = tmp_path / ".replit"
    probe = tmp_path / "probe.py"
    overlay.write_bytes(b"test overlay")
    probe.write_bytes(b"not executed")
    settings = Settings(
        _env_file=None, database_url="sqlite:///:memory:", app_env="test",
        app_secret_key="synthetic-test-session-key-" * 3, admin_username="test-admin",
        admin_password_hash="synthetic-password-hash", auth_allowed_origins=ORIGIN,
        object_storage_readiness_management_enabled="true",
        readiness_expected_canonical_commit_sha="a" * 40,
        readiness_expected_canonical_tree_sha="b" * 40,
        readiness_expected_release_commit_sha="c" * 40,
        readiness_expected_release_tree_sha="d" * 40,
        readiness_expected_overlay_sha256=hashlib.sha256(overlay.read_bytes()).hexdigest(),
        readiness_expected_probe_sha256=hashlib.sha256(probe.read_bytes()).hexdigest(),
        readiness_expected_topology="canonical_parent_with_checkpoint_overlay",
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(grants, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    from app import middleware
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)
    monkeypatch.setenv("REPLIT_DEPLOYMENT", "1")  # Synthetic marker, no live execution.
    monkeypatch.setenv(execution.PARENT_GATE, "false")
    monkeypatch.setattr(execution, "OVERLAY_PATH", overlay)
    monkeypatch.setattr(execution, "PROBE_PATH", probe)
    binding = grants.binding_from_settings(settings)
    provenance = BuildProvenance(
        present=True, valid=True, commit_sha=binding.canonical_commit_sha,
        tree_sha=binding.canonical_tree_sha, release_commit_sha=binding.release_commit_sha,
        release_tree_sha=binding.release_tree_sha, topology=binding.topology,
        overlay_path=".replit", overlay_sha256=binding.overlay_sha256,
    )
    monkeypatch.setattr(execution, "read_build_provenance", lambda: provenance)
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(routes.router, prefix="/api")
    client = TestClient(app)  # Deliberately no app.main/startup/scheduler lifespan.
    client.cookies.set(auth.SESSION_COOKIE, auth.make_session(settings.admin_username))
    state = SimpleNamespace(settings=settings, binding=binding, client=client,
                            provenance=provenance, monkeypatch=monkeypatch)
    state.headers = {
        "Origin": ORIGIN, grants.CONFIRM_HEADER: grants.CONFIRMATION,
        grants.GRANT_HEADER: grants.issue_authorization(settings, binding),
    }
    return state


def _no_execute(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Admission/launch must not be reached")
    monkeypatch.setattr(routes, "execute_readiness", forbidden)
    monkeypatch.setattr(execution, "execution_engine", forbidden)


def test_valid_management_post_and_no_store(management, monkeypatch):
    calls = []
    def execute(settings, binding, claims):
        calls.append((settings, binding, claims))
        return {"outcome": "PASS", "exit_code": 0, "receipt": None}
    monkeypatch.setattr(routes, "execute_readiness", execute)
    response = management.client.post(PATH, headers=management.headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1 and calls[0][1] == management.binding
    assert calls[0][2]["actor"] == grants.actor_digest("test-admin")


@pytest.mark.parametrize("cookie", ["absent", "tampered", "expired", "malformed"])
def test_invalid_admin_never_reaches_execution(management, monkeypatch, cookie):
    _no_execute(monkeypatch)
    management.client.cookies.clear()
    if cookie == "tampered":
        value = auth.make_session("test-admin") + "x"
    elif cookie == "expired":
        body = base64.urlsafe_b64encode(
            json.dumps({"sub": "test-admin", "exp": 1}).encode()
        ).decode().rstrip("=")
        value = auth._sign(body)
    elif cookie == "malformed":
        body = base64.urlsafe_b64encode(b'["not-a-session-object"]').decode().rstrip("=")
        value = auth._sign(body)
    else:
        value = None
    if value:
        management.client.cookies.set(auth.SESSION_COOKIE, value)
    assert management.client.post(PATH, headers=management.headers).status_code == 401


@pytest.mark.parametrize("exposed", [True, False])
def test_auth_bypass_is_never_management_authorization(management, monkeypatch, exposed):
    _no_execute(monkeypatch)
    management.settings.auth_disabled = True
    if not exposed:
        monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
        monkeypatch.delenv("REPLIT_DEV_DOMAIN", raising=False)
    assert management.client.post(PATH, headers=management.headers).status_code == 503


@pytest.mark.parametrize("origin", [None, "https://untrusted.invalid", "null"])
def test_missing_or_untrusted_origin(management, monkeypatch, origin):
    _no_execute(monkeypatch)
    headers = dict(management.headers)
    headers.pop("Origin")
    if origin:
        headers["Origin"] = origin
    assert management.client.post(PATH, headers=headers).status_code == 403


@pytest.mark.parametrize("gate", ["false", "", "TRUE", "1", "yes"])
def test_management_gate_exact_opt_in(management, monkeypatch, gate):
    _no_execute(monkeypatch)
    management.settings.object_storage_readiness_management_enabled = gate
    assert management.client.post(PATH, headers=management.headers).status_code == 503


@pytest.mark.parametrize("gate", ["true", "", "TRUE", "0"])
def test_parent_probe_gate_must_remain_closed(management, monkeypatch, gate):
    _no_execute(monkeypatch)
    monkeypatch.setenv(execution.PARENT_GATE, gate)
    response = management.client.post(PATH, headers=management.headers)
    assert response.status_code == 503
    assert response.json()["code"] == "PARENT_GATE_NOT_CLOSED"


def test_workspace_marker_is_not_production(management, monkeypatch):
    _no_execute(monkeypatch)
    monkeypatch.delenv("REPLIT_DEPLOYMENT")
    monkeypatch.setenv("REPLIT_DEV_DOMAIN", "synthetic.invalid")
    assert management.client.post(PATH, headers=management.headers).status_code == 503


@pytest.mark.parametrize("field", [
    name for name in Settings.model_fields
    if name.endswith("_enabled") and name != "object_storage_readiness_management_enabled"
])
def test_every_operational_gate_must_be_closed(management, monkeypatch, field):
    _no_execute(monkeypatch)
    setattr(management.settings, field, True)
    response = management.client.post(PATH, headers=management.headers)
    assert response.status_code == 503
    assert response.json()["code"] == "OPERATIONAL_GATES_NOT_CLOSED"


@pytest.mark.parametrize("field,value", [
    ("routine_pinterest_dry_run", False), ("routine_pinterest_batch_size", 2),
    ("routine_pinterest_daily_write_limit", 2),
])
def test_dry_run_bounds(management, monkeypatch, field, value):
    _no_execute(monkeypatch)
    setattr(management.settings, field, value)
    assert management.client.post(PATH, headers=management.headers).status_code == 503


@pytest.mark.parametrize("query,body", [
    ("?bucket=other", None), ("?key=other", None), ("?url=https://evil.invalid", None),
    ("", "{}"), ("", '{"payload":"abc"}'), ("", '{"publication_id":"x"}'),
    ("", '{"argv":["--other"]}'),
])
def test_caller_inputs_prohibited(management, monkeypatch, query, body):
    _no_execute(monkeypatch)
    assert management.client.post(
        PATH + query, content=body, headers=management.headers,
    ).status_code == 400


@pytest.mark.parametrize("header", [grants.GRANT_HEADER, grants.CONFIRM_HEADER])
def test_explicit_authorization_and_confirmation_required(management, monkeypatch, header):
    _no_execute(monkeypatch)
    headers = dict(management.headers)
    headers.pop(header)
    assert management.client.post(PATH, headers=headers).status_code == 403


def test_duplicate_management_headers_refused(management, monkeypatch):
    _no_execute(monkeypatch)
    headers = list(management.headers.items()) + [
        (grants.GRANT_HEADER, management.headers[grants.GRANT_HEADER]),
    ]
    assert management.client.post(PATH, headers=headers).status_code == 403


@pytest.mark.parametrize("change", [
    "signature", "expired", "future", "too_long", "purpose", "binding", "actor",
    "bool_expiry", "extra", "missing", "session_instead",
])
def test_grant_validation(management, monkeypatch, change):
    _no_execute(monkeypatch)
    token = management.headers[grants.GRANT_HEADER]
    body, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if change == "signature":
        token = body + "." + ("x" * 43)
    elif change == "session_instead":
        token = auth.make_session("test-admin")
    else:
        updates = {
            "expired": {"iat": 1, "exp": 2},
            "future": {"iat": claims["iat"] + 500, "exp": claims["exp"] + 500},
            "too_long": {"exp": claims["iat"] + 181},
            "purpose": {"purpose": "business-publication"},
            "binding": {"binding": "e" * 64}, "actor": {"actor": "f" * 64},
            "bool_expiry": {"exp": True}, "extra": {"bucket": "unsafe"},
        }
        if change == "missing":
            claims.pop("jti")
        else:
            claims.update(updates[change])
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        token = body + "." + grants._sign(body, management.settings)
    headers = dict(management.headers, **{grants.GRANT_HEADER: token})
    response = management.client.post(PATH, headers=headers)
    assert response.status_code == 403
    assert token not in response.text


@pytest.mark.parametrize("field", [
    "commit_sha", "tree_sha", "release_commit_sha",
    "release_tree_sha", "overlay_sha256", "topology", "valid",
])
def test_manifest_binding_mismatch(management, monkeypatch, field):
    _no_execute(monkeypatch)
    value = False if field == "valid" else "wrong"
    monkeypatch.setattr(execution, "read_build_provenance", lambda: replace(
        management.provenance, **{field: value},
    ))
    assert management.client.post(PATH, headers=management.headers).status_code == 503


@pytest.mark.parametrize("artifact", ["OVERLAY_PATH", "PROBE_PATH"])
def test_actual_artifact_digest_checked(management, monkeypatch, artifact):
    _no_execute(monkeypatch)
    getattr(execution, artifact).write_bytes(b"changed after certification")
    assert management.client.post(PATH, headers=management.headers).status_code == 503


@pytest.mark.parametrize("outcome,expected", [("FAILED", 502), ("UNKNOWN", 503)])
def test_failed_and_unknown_child_http_status(management, monkeypatch, outcome, expected):
    monkeypatch.setattr(routes, "execute_readiness", lambda *_: {
        "outcome": outcome, "exit_code": 1, "receipt": None,
    })
    assert management.client.post(PATH, headers=management.headers).status_code == expected


def test_status_retrieval_after_gate_closed_does_not_execute(management, monkeypatch):
    _no_execute(monkeypatch)
    management.settings.object_storage_readiness_management_enabled = "false"
    monkeypatch.setattr(routes, "lookup_readiness", lambda *_: {
        "admission_state": "CONSUMED", "outcome": "UNKNOWN", "receipt": None,
        "exit_code": None,
    })
    response = management.client.get(PATH, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.json()["admission_state"] == "CONSUMED"
    assert response.headers["cache-control"] == "no-store"


def test_scope_is_not_rearmed_by_new_grant_nonce(management):
    one = grants.issue_authorization(management.settings, management.binding)
    two = grants.issue_authorization(management.settings, management.binding)
    assert one != two
    assert management.binding.scope == grants.binding_from_settings(management.settings).scope


def test_auth_and_grant_signatures_are_domain_separated(management):
    token = management.headers[grants.GRANT_HEADER]
    assert auth.verify_session(token) is None
    with pytest.raises(ReadinessError):
        grants.verify_authorization(
            auth.make_session("test-admin"), management.settings,
            management.binding, "test-admin",
        )