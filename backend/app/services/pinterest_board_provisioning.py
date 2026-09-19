from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    AuditLog,
    PinterestBoard,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
)
from app.services.pinterest_board_strategy import (
    BOARD_STRATEGY_VERSION,
    board_request_fingerprint,
    board_strategy,
    normalize_board_text,
)
from app.services.pinterest_oauth import decrypt_token


BOARD_PROVISIONING_ACTOR = "autonomous-board-provisioner-v1"


class BoardProvisioningError(RuntimeError):
    pass


class PinterestBoardProvisioningClient:
    async def create_board(self, access_token: str, payload: dict[str, Any]) -> tuple[int, Any]:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.post(
                f"{settings.pinterest_api_base.rstrip('/')}/boards",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        try:
            body = response.json()
        except Exception:
            body = None
        return response.status_code, body


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_exact_attempt(db, attempt_id: str) -> PinterestBoardProvisioningAttempt:
    attempt = db.get(PinterestBoardProvisioningAttempt, attempt_id)
    if attempt is None:
        raise BoardProvisioningError("BOARD_PROVISIONING_ATTEMPT_NOT_FOUND")
    return attempt


def _validate_attempt_binding(
    attempt: PinterestBoardProvisioningAttempt,
    connection: PinterestConnection,
) -> None:
    expected = board_request_fingerprint(
        connection_id=connection.id,
        external_user_id=connection.external_user_id,
        canonical_key=attempt.canonical_key,
        desired_name=attempt.desired_name,
        desired_description=attempt.desired_description,
        privacy=attempt.privacy,
    )
    if expected != attempt.request_fingerprint:
        raise BoardProvisioningError("BOARD_PROVISIONING_ATTEMPT_DRIFT")


def start_board_provisioning(
    db,
    *,
    draft_id: str | None = None,
    canonical_key: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PinterestBoardProvisioningAttempt:
    settings = settings or get_settings()
    plan = board_strategy(
        db,
        draft_id=draft_id,
        canonical_key=canonical_key,
        settings=settings,
    )
    if plan["status"] != "PROVISION_REQUIRED":
        raise BoardProvisioningError(plan["status"])
    if plan["provisioning_ready"] is not True:
        raise BoardProvisioningError((plan["blockers"] or ["BOARD_PROVISIONING_NOT_READY"])[0])

    connection = db.scalar(
        select(PinterestConnection)
        .where(PinterestConnection.status == "CONNECTED")
        .order_by(PinterestConnection.created_at, PinterestConnection.id)
        .limit(1)
    )
    if connection is None:
        raise BoardProvisioningError("PINTEREST_CONNECTION_REQUIRED")

    existing = db.scalar(
        select(PinterestBoardProvisioningAttempt)
        .where(PinterestBoardProvisioningAttempt.request_fingerprint == plan["request_fingerprint"])
        .limit(1)
    )
    if existing is not None:
        raise BoardProvisioningError(
            {
                "STARTED": "PROVISIONING_IN_PROGRESS",
                "SUCCEEDED": "PROVISIONING_SUCCEEDED_SYNC_REQUIRED",
                "FAILED": "PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
                "UNKNOWN": "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
            }.get(existing.status, "PROVISIONING_ATTEMPT_STATE_INVALID")
        )

    now = now or _now()
    attempt = PinterestBoardProvisioningAttempt(
        connection_id=connection.id,
        canonical_key=plan["canonical_key"],
        desired_name=plan["desired_name"],
        desired_description=plan["desired_description"],
        privacy=plan["privacy"],
        request_fingerprint=plan["request_fingerprint"],
        status="STARTED",
        started_at=now,
        safe_metadata={
            "strategy_version": BOARD_STRATEGY_VERSION,
            "provider_called": False,
        },
    )
    db.add(attempt)
    db.flush()
    db.add(AuditLog(
        actor=BOARD_PROVISIONING_ACTOR,
        action="PINTEREST_BOARD_PROVISIONING_STARTED",
        entity_type="PinterestBoardProvisioningAttempt",
        entity_id=attempt.id,
        metadata_json={
            "canonical_key": attempt.canonical_key,
            "request_fingerprint": attempt.request_fingerprint,
        },
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise BoardProvisioningError("BOARD_PROVISIONING_REQUEST_ALREADY_EXISTS") from None
    except Exception:
        db.rollback()
        raise
    db.refresh(attempt)
    return attempt


async def execute_board_provisioning_attempt(
    db,
    attempt_id: str,
    *,
    settings: Settings | None = None,
    client: PinterestBoardProvisioningClient | None = None,
    now: datetime | None = None,
) -> PinterestBoardProvisioningAttempt:
    settings = settings or get_settings()
    if settings.pinterest_board_write_scope_enabled is not True:
        raise BoardProvisioningError("BOARD_WRITE_SCOPE_DISABLED")
    if settings.pinterest_board_provisioning_enabled is not True:
        raise BoardProvisioningError("BOARD_PROVISIONING_DISABLED")

    attempt = _load_exact_attempt(db, attempt_id)
    if attempt.status == "UNKNOWN":
        raise BoardProvisioningError("PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED")
    if attempt.status != "STARTED":
        raise BoardProvisioningError(f"BOARD_PROVISIONING_ATTEMPT_{attempt.status}")
    if attempt.completed_at is not None or attempt.provider_board_id is not None:
        raise BoardProvisioningError("BOARD_PROVISIONING_ATTEMPT_DRIFT")

    connection = db.get(PinterestConnection, attempt.connection_id)
    if (
        connection is None
        or connection.status != "CONNECTED"
        or not connection.access_token_ciphertext
    ):
        raise BoardProvisioningError("PINTEREST_CONNECTION_REQUIRED")
    if "boards:write" not in (connection.granted_scopes or []):
        raise BoardProvisioningError("BOARDS_WRITE_SCOPE_REQUIRED")
    _validate_attempt_binding(attempt, connection)

    now = now or _now()
    try:
        access_token = decrypt_token(connection.access_token_ciphertext)
    except Exception:
        attempt.status = "FAILED"
        attempt.completed_at = now
        attempt.error_code = "TOKEN_DECRYPT_FAILED"
        attempt.safe_metadata = {
            "strategy_version": BOARD_STRATEGY_VERSION,
            "provider_called": False,
            "classification": "FAILED",
        }
        db.commit()
        return attempt

    payload = {
        "name": attempt.desired_name,
        "description": attempt.desired_description,
        "privacy": attempt.privacy,
    }
    provider = client or PinterestBoardProvisioningClient()
    try:
        status_code, body = await provider.create_board(access_token, payload)
    except (httpx.TimeoutException, httpx.TransportError, OSError):
        attempt.status = "UNKNOWN"
        attempt.completed_at = now
        attempt.error_code = "PINTEREST_BOARD_CREATE_TRANSPORT_UNKNOWN"
        attempt.safe_metadata = {
            "strategy_version": BOARD_STRATEGY_VERSION,
            "provider_called": True,
            "classification": "UNKNOWN",
        }
        db.commit()
        return attempt
    except Exception:
        attempt.status = "UNKNOWN"
        attempt.completed_at = now
        attempt.error_code = "PINTEREST_BOARD_CREATE_RESPONSE_UNKNOWN"
        attempt.safe_metadata = {
            "strategy_version": BOARD_STRATEGY_VERSION,
            "provider_called": True,
            "classification": "UNKNOWN",
        }
        db.commit()
        return attempt

    safe_metadata = {
        "strategy_version": BOARD_STRATEGY_VERSION,
        "provider_called": True,
        "http_status": int(status_code),
    }
    provider_id = body.get("id") if isinstance(body, dict) else None
    valid_provider_id = (
        not isinstance(provider_id, bool)
        and isinstance(provider_id, (str, int))
        and bool(str(provider_id).strip())
    )

    if 200 <= int(status_code) < 300 and valid_provider_id:
        attempt.status = "SUCCEEDED"
        attempt.provider_board_id = str(provider_id).strip()
        attempt.error_code = None
        safe_metadata["classification"] = "SUCCEEDED"
    elif 400 <= int(status_code) < 500:
        attempt.status = "FAILED"
        attempt.error_code = "PINTEREST_BOARD_CREATE_REJECTED"
        safe_metadata["classification"] = "FAILED"
    else:
        attempt.status = "UNKNOWN"
        attempt.error_code = "PINTEREST_BOARD_CREATE_PROVIDER_UNKNOWN"
        safe_metadata["classification"] = "UNKNOWN"

    attempt.completed_at = now
    attempt.safe_metadata = safe_metadata
    db.add(AuditLog(
        actor=BOARD_PROVISIONING_ACTOR,
        action=f"PINTEREST_BOARD_PROVISIONING_{attempt.status}",
        entity_type="PinterestBoardProvisioningAttempt",
        entity_id=attempt.id,
        metadata_json={
            "canonical_key": attempt.canonical_key,
            "request_fingerprint": attempt.request_fingerprint,
            "provider_board_id": attempt.provider_board_id,
            "classification": attempt.status,
            "http_status": safe_metadata.get("http_status"),
        },
    ))
    db.commit()
    db.refresh(attempt)
    return attempt


def reconcile_provisioned_board(
    db,
    attempt_id: str,
    *,
    now: datetime | None = None,
) -> PinterestBoard:
    attempt = _load_exact_attempt(db, attempt_id)
    if attempt.status != "SUCCEEDED" or not attempt.provider_board_id:
        raise BoardProvisioningError("CONFIRMED_PROVIDER_BOARD_REQUIRED")

    connection = db.get(PinterestConnection, attempt.connection_id)
    if (
        connection is None
        or connection.status != "CONNECTED"
        or not connection.boards_last_synced_at
    ):
        raise BoardProvisioningError("BOARD_SYNC_REQUIRED")

    board = db.scalar(
        select(PinterestBoard)
        .where(
            PinterestBoard.connection_id == connection.id,
            PinterestBoard.external_board_id == attempt.provider_board_id,
            PinterestBoard.is_active.is_(True),
        )
        .limit(1)
    )
    if board is None:
        raise BoardProvisioningError("PROVISIONED_BOARD_NOT_SYNCED")
    if not board.last_synced_at or board.last_synced_at != connection.boards_last_synced_at:
        raise BoardProvisioningError("BOARD_SYNC_STALE")
    if normalize_board_text(board.name) != normalize_board_text(attempt.desired_name):
        raise BoardProvisioningError("PROVISIONED_BOARD_IDENTITY_MISMATCH")

    board.routing_label = attempt.canonical_key
    board.is_eligible = True
    db.add(AuditLog(
        actor=BOARD_PROVISIONING_ACTOR,
        action="PINTEREST_BOARD_PROVISIONING_RECONCILED",
        entity_type="PinterestBoard",
        entity_id=board.id,
        metadata_json={
            "attempt_id": attempt.id,
            "canonical_key": attempt.canonical_key,
            "external_board_id": attempt.provider_board_id,
        },
    ))
    db.commit()
    db.refresh(board)
    return board
