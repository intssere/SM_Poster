import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    AuditLog,
    Board,
    PinterestBoard,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
)
from app.services import pinterest_board_provisioning as provisioning
from app.services import pinterest_board_strategy as strategy
from app.services import pinterest_oauth as oauth


NOW = datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "pinterest_board_write_scope_enabled": False,
        "pinterest_board_provisioning_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _local_board(db, *, key="arabian-fragrance", name="Arabian Fragrance", rules=None):
    row = Board(
        id=f"local-{key}",
        store_id="store-1",
        pinterest_board_id=None,
        name=name,
        slug=key,
        rules=rules or {},
        active=True,
    )
    db.add(row)
    db.commit()
    return row


def _connection(db, *, scopes=None, synced=True):
    row = PinterestConnection(
        id="connection-1",
        external_user_id="user-1",
        username="diamond-shelf",
        granted_scopes=scopes or ["user_accounts:read", "boards:read", "pins:read"],
        access_token_ciphertext="cipher-access",
        refresh_token_ciphertext="cipher-refresh",
        status="CONNECTED",
        boards_last_synced_at=NOW if synced else None,
    )
    db.add(row)
    db.commit()
    return row


def _provider_board(
    db,
    connection,
    *,
    external_id="board-1",
    name="Arabian Fragrance",
    routing_label=None,
    eligible=True,
    last_synced_at=NOW,
):
    row = PinterestBoard(
        id=f"row-{external_id}",
        connection_id=connection.id,
        external_board_id=external_id,
        name=name,
        privacy="PUBLIC",
        is_active=True,
        is_eligible=eligible,
        routing_label=routing_label,
        last_seen_at=last_synced_at,
        last_synced_at=last_synced_at,
    )
    db.add(row)
    db.commit()
    return row


def _enabled_settings():
    return _settings(
        pinterest_board_write_scope_enabled=True,
        pinterest_board_provisioning_enabled=True,
    )


def _provisionable(db, *, key="arabian-fragrance"):
    _local_board(db, key=key, name="Arabian Fragrance" if key == "arabian-fragrance" else "Designer Fragrances")
    connection = _connection(
        db,
        scopes=["user_accounts:read", "boards:read", "pins:read", "boards:write"],
    )
    return connection


def test_routing_label_exact_match_wins():
    db = _db()
    _local_board(db)
    connection = _connection(db)
    board = _provider_board(
        db,
        connection,
        name="Completely Different Display Name",
        routing_label="arabian-fragrance",
        eligible=True,
    )

    result = strategy.board_strategy(db, canonical_key="arabian-fragrance", settings=_settings())

    assert result["status"] == "ROUTE_EXISTING"
    assert result["selected_board_id"] == board.id
    assert result["match_basis"] == "routing_label"
    assert result["blockers"] == []
    assert result["provider_called"] is False
    assert result["state_mutated"] is False
    db.close()


def test_normalized_name_match_routes_existing_board():
    db = _db()
    _local_board(db)
    connection = _connection(db)
    board = _provider_board(
        db,
        connection,
        name="  ARABIAN---FRAGRANCE!!! ",
        eligible=True,
    )

    result = strategy.board_strategy(db, canonical_key="arabian-fragrance", settings=_settings())

    assert result["status"] == "ROUTE_EXISTING"
    assert result["selected_board_id"] == board.id
    assert result["match_basis"] == "normalized_name"
    db.close()


def test_stale_semantic_board_blocks_instead_of_provisioning_duplicate():
    db = _db()
    _local_board(db)
    connection = _connection(db)
    _provider_board(
        db,
        connection,
        eligible=True,
        last_synced_at=NOW - timedelta(minutes=10),
    )

    result = strategy.board_strategy(db, canonical_key="arabian-fragrance", settings=_enabled_settings())

    assert result["status"] == "BLOCKED"
    assert result["blockers"] == ["BOARD_SYNC_STALE"]
    assert result["request_fingerprint"] is None
    db.close()


def test_existing_but_ineligible_semantic_board_blocks_duplicate_creation():
    db = _db()
    _local_board(db)
    connection = _connection(db)
    board = _provider_board(db, connection, eligible=False)

    result = strategy.board_strategy(db, canonical_key="arabian-fragrance", settings=_enabled_settings())

    assert result["status"] == "BLOCKED"
    assert result["selected_board_id"] == board.id
    assert result["blockers"] == ["EXISTING_BOARD_NOT_ELIGIBLE"]
    db.close()


def test_no_match_returns_deterministic_provisioning_plan():
    db = _db()
    _local_board(db)
    _connection(
        db,
        scopes=["user_accounts:read", "boards:read", "pins:read", "boards:write"],
    )

    first = strategy.board_strategy(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
    )
    second = strategy.board_strategy(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
    )

    assert first["status"] == "PROVISION_REQUIRED"
    assert first["provisioning_ready"] is True
    assert first["blockers"] == []
    assert len(first["request_fingerprint"]) == 64
    assert first["request_fingerprint"] == second["request_fingerprint"]
    assert first["desired_name"] == "Arabian Fragrance"
    assert first["privacy"] == "PUBLIC"
    db.close()


def test_default_flags_keep_provisioning_inert():
    db = _db()
    _local_board(db)
    _connection(db)

    result = strategy.board_strategy(db, canonical_key="arabian-fragrance", settings=_settings())

    assert result["status"] == "PROVISION_REQUIRED"
    assert result["provisioning_ready"] is False
    assert result["blockers"] == [
        "BOARD_WRITE_SCOPE_DISABLED",
        "BOARD_PROVISIONING_DISABLED",
        "BOARDS_WRITE_SCOPE_REQUIRED",
    ]
    defaults = _settings()
    assert defaults.pinterest_board_write_scope_enabled is False
    assert defaults.pinterest_board_provisioning_enabled is False
    db.close()


def test_existing_started_attempt_prevents_second_mutation_boundary():
    db = _db()
    _provisionable(db)
    settings = _enabled_settings()

    attempt = provisioning.start_board_provisioning(
        db,
        canonical_key="arabian-fragrance",
        settings=settings,
        now=NOW,
    )

    with pytest.raises(provisioning.BoardProvisioningError, match="PROVISIONING_IN_PROGRESS"):
        provisioning.start_board_provisioning(
            db,
            canonical_key="arabian-fragrance",
            settings=settings,
            now=NOW,
        )

    assert db.query(PinterestBoardProvisioningAttempt).count() == 1
    assert attempt.status == "STARTED"
    audit = db.scalar(
        select(AuditLog)
        .where(AuditLog.action == "PINTEREST_BOARD_PROVISIONING_STARTED")
    )
    assert audit is not None
    assert audit.entity_id == attempt.id
    db.close()


def test_oauth_board_write_scope_is_explicitly_opt_in():
    read_only = type(
        "Settings",
        (),
        {
            "pinterest_write_scope_enabled": False,
            "pinterest_board_write_scope_enabled": False,
        },
    )()
    board_write = type(
        "Settings",
        (),
        {
            "pinterest_write_scope_enabled": False,
            "pinterest_board_write_scope_enabled": True,
        },
    )()

    assert "boards:write" not in oauth.requested_scopes(read_only)
    assert "boards:write" in oauth.requested_scopes(board_write)
    assert oauth.granted_scopes_valid(
        [*oauth.READ_SCOPES, "boards:write"],
        board_write,
    )
    assert not oauth.granted_scopes_valid(
        list(oauth.READ_SCOPES),
        board_write,
    )
    assert not oauth.granted_scopes_valid(
        [*oauth.READ_SCOPES, "boards:write"],
        read_only,
    )


def test_execute_refuses_without_enabled_flags_or_scope(monkeypatch):
    db = _db()
    connection = _provisionable(db)
    attempt = provisioning.start_board_provisioning(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
        now=NOW,
    )

    with pytest.raises(provisioning.BoardProvisioningError, match="BOARD_WRITE_SCOPE_DISABLED"):
        asyncio.run(
            provisioning.execute_board_provisioning_attempt(
                db,
                attempt.id,
                settings=_settings(
                    pinterest_board_write_scope_enabled=False,
                    pinterest_board_provisioning_enabled=True,
                ),
            )
        )

    connection.granted_scopes = list(oauth.READ_SCOPES)
    db.commit()
    with pytest.raises(provisioning.BoardProvisioningError, match="BOARDS_WRITE_SCOPE_REQUIRED"):
        asyncio.run(
            provisioning.execute_board_provisioning_attempt(
                db,
                attempt.id,
                settings=_enabled_settings(),
            )
        )
    db.close()


class _FakeCreateClient:
    def __init__(self, status_code=201, body=None, error=None):
        self.status_code = status_code
        self.body = {"id": "provider-board-1"} if body is None else body
        self.error = error
        self.calls = []

    async def create_board(self, access_token, payload):
        self.calls.append((access_token, payload))
        if self.error is not None:
            raise self.error
        return self.status_code, self.body


def test_confirmed_success_records_provider_id_without_creating_local_board(monkeypatch):
    db = _db()
    _provisionable(db)
    attempt = provisioning.start_board_provisioning(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
        now=NOW,
    )
    monkeypatch.setattr(provisioning, "decrypt_token", lambda _: "secret-access")
    client = _FakeCreateClient()

    result = asyncio.run(
        provisioning.execute_board_provisioning_attempt(
            db,
            attempt.id,
            settings=_enabled_settings(),
            client=client,
            now=NOW + timedelta(seconds=1),
        )
    )

    assert result.status == "SUCCEEDED"
    assert result.provider_board_id == "provider-board-1"
    assert result.safe_metadata == {
        "strategy_version": strategy.BOARD_STRATEGY_VERSION,
        "provider_called": True,
        "http_status": 201,
        "classification": "SUCCEEDED",
    }
    assert db.query(PinterestBoard).count() == 0
    assert client.calls[0][1]["name"] == "Arabian Fragrance"
    assert client.calls[0][1]["privacy"] == "PUBLIC"
    db.close()


def test_definitive_4xx_is_failed_and_provider_body_is_not_persisted(monkeypatch):
    db = _db()
    _provisionable(db)
    attempt = provisioning.start_board_provisioning(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
        now=NOW,
    )
    monkeypatch.setattr(provisioning, "decrypt_token", lambda _: "secret-access")
    client = _FakeCreateClient(
        status_code=400,
        body={"message": "secret provider body", "access_token": "secret-access"},
    )

    result = asyncio.run(
        provisioning.execute_board_provisioning_attempt(
            db,
            attempt.id,
            settings=_enabled_settings(),
            client=client,
            now=NOW + timedelta(seconds=1),
        )
    )

    assert result.status == "FAILED"
    assert result.error_code == "PINTEREST_BOARD_CREATE_REJECTED"
    rendered = repr(result.safe_metadata)
    assert "secret provider body" not in rendered
    assert "secret-access" not in rendered
    db.close()


def test_transport_or_5xx_unknown_never_auto_retries(monkeypatch):
    db = _db()
    _provisionable(db)
    attempt = provisioning.start_board_provisioning(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
        now=NOW,
    )
    monkeypatch.setattr(provisioning, "decrypt_token", lambda _: "token")
    request = httpx.Request("POST", "https://api.pinterest.com/v5/boards")
    client = _FakeCreateClient(error=httpx.ReadTimeout("timeout", request=request))

    result = asyncio.run(
        provisioning.execute_board_provisioning_attempt(
            db,
            attempt.id,
            settings=_enabled_settings(),
            client=client,
            now=NOW + timedelta(seconds=1),
        )
    )
    assert result.status == "UNKNOWN"
    assert result.error_code == "PINTEREST_BOARD_CREATE_TRANSPORT_UNKNOWN"

    second = _FakeCreateClient(status_code=201)
    with pytest.raises(
        provisioning.BoardProvisioningError,
        match="PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
    ):
        asyncio.run(
            provisioning.execute_board_provisioning_attempt(
                db,
                attempt.id,
                settings=_enabled_settings(),
                client=second,
            )
        )
    assert second.calls == []
    db.close()


def test_5xx_and_invalid_2xx_are_unknown(monkeypatch):
    for status_code, body in [(503, {"message": "oops"}), (201, {"name": "missing id"})]:
        db = _db()
        _provisionable(db)
        attempt = provisioning.start_board_provisioning(
            db,
            canonical_key="arabian-fragrance",
            settings=_enabled_settings(),
            now=NOW,
        )
        monkeypatch.setattr(provisioning, "decrypt_token", lambda _: "token")
        result = asyncio.run(
            provisioning.execute_board_provisioning_attempt(
                db,
                attempt.id,
                settings=_enabled_settings(),
                client=_FakeCreateClient(status_code=status_code, body=body),
                now=NOW + timedelta(seconds=1),
            )
        )
        assert result.status == "UNKNOWN"
        assert result.provider_board_id is None
        db.close()


def test_exact_provider_id_reconciliation_marks_only_created_board_eligible(monkeypatch):
    db = _db()
    connection = _provisionable(db)
    attempt = provisioning.start_board_provisioning(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
        now=NOW,
    )
    monkeypatch.setattr(provisioning, "decrypt_token", lambda _: "token")
    asyncio.run(
        provisioning.execute_board_provisioning_attempt(
            db,
            attempt.id,
            settings=_enabled_settings(),
            client=_FakeCreateClient(status_code=201, body={"id": "provider-board-1"}),
            now=NOW + timedelta(seconds=1),
        )
    )

    board = _provider_board(
        db,
        connection,
        external_id="provider-board-1",
        name="Arabian Fragrance",
        eligible=False,
        last_synced_at=NOW,
    )
    reconciled = provisioning.reconcile_provisioned_board(db, attempt.id)

    assert reconciled.id == board.id
    assert reconciled.is_eligible is True
    assert reconciled.routing_label == "arabian-fragrance"

    routed = strategy.board_strategy(
        db,
        canonical_key="arabian-fragrance",
        settings=_settings(),
    )
    assert routed["status"] == "ROUTE_EXISTING"
    assert routed["selected_board_id"] == board.id
    db.close()


def test_reconciliation_fails_on_wrong_provider_identity(monkeypatch):
    db = _db()
    connection = _provisionable(db)
    attempt = provisioning.start_board_provisioning(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
        now=NOW,
    )
    monkeypatch.setattr(provisioning, "decrypt_token", lambda _: "token")
    asyncio.run(
        provisioning.execute_board_provisioning_attempt(
            db,
            attempt.id,
            settings=_enabled_settings(),
            client=_FakeCreateClient(status_code=201, body={"id": "provider-board-1"}),
            now=NOW + timedelta(seconds=1),
        )
    )
    _provider_board(
        db,
        connection,
        external_id="provider-board-1",
        name="Unrelated Board",
        eligible=False,
        last_synced_at=NOW,
    )

    with pytest.raises(
        provisioning.BoardProvisioningError,
        match="PROVISIONED_BOARD_IDENTITY_MISMATCH",
    ):
        provisioning.reconcile_provisioned_board(db, attempt.id)
    db.close()


def test_read_only_strategy_never_mutates_attempts_or_provider_state():
    db = _db()
    _local_board(db)
    _connection(
        db,
        scopes=["user_accounts:read", "boards:read", "pins:read", "boards:write"],
    )

    before = (
        db.query(PinterestBoardProvisioningAttempt).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )
    result = strategy.board_strategy(
        db,
        canonical_key="arabian-fragrance",
        settings=_enabled_settings(),
    )
    after = (
        db.query(PinterestBoardProvisioningAttempt).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )

    assert result["status"] == "PROVISION_REQUIRED"
    assert before == after
    assert result["provider_called"] is False
    assert result["state_mutated"] is False
    db.close()
