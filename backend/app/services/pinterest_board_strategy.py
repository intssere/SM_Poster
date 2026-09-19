from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.domain import (
    Board,
    PinConcept,
    PinDraft,
    PinterestBoard,
    PinterestBoardProvisioningAttempt,
    PinterestConnection,
)


BOARD_STRATEGY_VERSION = "PINTEREST_BOARD_STRATEGY_V1"
SUPPORTED_PRIVACY = "PUBLIC"


def normalize_board_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def board_request_fingerprint(
    *,
    connection_id: str,
    external_user_id: str,
    canonical_key: str,
    desired_name: str,
    desired_description: str,
    privacy: str,
) -> str:
    payload = {
        "version": BOARD_STRATEGY_VERSION,
        "connection_id": connection_id,
        "external_user_id": external_user_id,
        "canonical_key": canonical_key,
        "desired_name": normalize_board_text(desired_name),
        "desired_description": " ".join(desired_description.split()),
        "privacy": privacy,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _connection(db) -> tuple[PinterestConnection | None, list[str]]:
    rows = list(db.scalars(
        select(PinterestConnection)
        .where(PinterestConnection.status == "CONNECTED")
        .order_by(PinterestConnection.created_at, PinterestConnection.id)
    ).all())
    if not rows:
        return None, ["PINTEREST_CONNECTION_REQUIRED"]
    if len(rows) != 1:
        return None, ["MULTIPLE_CONNECTED_PINTEREST_ACCOUNTS"]
    return rows[0], []


def _local_intent(db, *, draft_id: str | None, canonical_key: str | None):
    if bool(draft_id) == bool(canonical_key):
        return None, ["EXACTLY_ONE_BOARD_INTENT_REQUIRED"]

    board = None
    resolved_draft_id = draft_id
    if draft_id:
        draft = db.get(PinDraft, draft_id)
        concept = db.get(PinConcept, draft.concept_id) if draft else None
        board = db.get(Board, concept.board_id) if concept and concept.board_id else None
        if not draft:
            return None, ["DRAFT_NOT_FOUND"]
        if not concept:
            return None, ["PIN_CONCEPT_NOT_FOUND"]
        if not board or not board.active:
            return None, ["LOCAL_BOARD_INTENT_REQUIRED"]
    else:
        key = str(canonical_key or "").strip()
        if not key:
            return None, ["CANONICAL_BOARD_KEY_REQUIRED"]
        matches = list(db.scalars(
            select(Board)
            .where(Board.slug == key, Board.active.is_(True))
            .order_by(Board.id)
        ).all())
        if not matches:
            return None, ["LOCAL_BOARD_INTENT_REQUIRED"]
        if len(matches) != 1:
            return None, ["LOCAL_BOARD_INTENT_AMBIGUOUS"]
        board = matches[0]

    rules = board.rules or {}
    desired_name = str(board.name or "").strip()
    description = rules.get("description")
    if not isinstance(description, str) or not description.strip():
        description = f"{desired_name} inspiration and product discoveries curated by Diamond Shelf."
    description = " ".join(description.split())[:500]
    privacy = str(rules.get("privacy") or SUPPORTED_PRIVACY).strip().upper()
    blockers = []
    if not board.slug or not desired_name:
        blockers.append("LOCAL_BOARD_INTENT_INCOMPLETE")
    if privacy != SUPPORTED_PRIVACY:
        blockers.append("UNSUPPORTED_BOARD_PRIVACY")
    return {
        "draft_id": resolved_draft_id,
        "local_board_id": board.id,
        "canonical_key": board.slug,
        "desired_name": desired_name,
        "desired_description": description,
        "privacy": privacy,
    }, blockers


def _match_candidates(db, connection: PinterestConnection, intent: dict[str, Any]):
    all_active = list(db.scalars(
        select(PinterestBoard)
        .where(
            PinterestBoard.connection_id == connection.id,
            PinterestBoard.is_active.is_(True),
        )
        .order_by(PinterestBoard.external_board_id, PinterestBoard.id)
    ).all())
    canonical_key = intent["canonical_key"]
    desired_normalized = normalize_board_text(intent["desired_name"])

    def is_current(row: PinterestBoard) -> bool:
        return bool(
            connection.boards_last_synced_at
            and row.last_synced_at
            and row.last_synced_at == connection.boards_last_synced_at
        )

    routing = [
        row for row in all_active
        if str(row.routing_label or "").strip().casefold() == canonical_key.casefold()
    ]
    name = [
        row for row in all_active
        if normalize_board_text(row.name) == desired_normalized
    ]

    # Routing label is explicit operator/system identity and therefore outranks
    # a name-only semantic match. Duplicates at either tier fail closed.
    if routing:
        return "routing_label", routing, is_current
    if name:
        return "normalized_name", name, is_current
    return None, [], is_current


def _attempt_state(db, request_fingerprint: str):
    return db.scalar(
        select(PinterestBoardProvisioningAttempt)
        .where(PinterestBoardProvisioningAttempt.request_fingerprint == request_fingerprint)
        .limit(1)
    )


def board_strategy(
    db,
    *,
    draft_id: str | None = None,
    canonical_key: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    intent, blockers = _local_intent(db, draft_id=draft_id, canonical_key=canonical_key)
    connection, connection_blockers = _connection(db)
    blockers = [*blockers, *connection_blockers]

    base = {
        "strategy_version": BOARD_STRATEGY_VERSION,
        "draft_id": draft_id,
        "status": "BLOCKED",
        "selected_board_id": None,
        "selected_external_board_id": None,
        "match_basis": None,
        "canonical_key": intent["canonical_key"] if intent else canonical_key,
        "desired_name": intent["desired_name"] if intent else None,
        "desired_description": intent["desired_description"] if intent else None,
        "privacy": intent["privacy"] if intent else None,
        "request_fingerprint": None,
        "provisioning_ready": False,
        "blockers": blockers,
        "existing_attempt": None,
        "state_mutated": False,
        "provider_called": False,
    }
    if blockers or not intent or not connection:
        return base

    if not connection.boards_last_synced_at:
        base["blockers"] = ["BOARD_SYNC_REQUIRED"]
        return base

    match_basis, matches, is_current = _match_candidates(db, connection, intent)
    if matches:
        if len(matches) != 1:
            base["blockers"] = ["EXISTING_BOARD_MATCH_AMBIGUOUS"]
            base["match_basis"] = match_basis
            return base
        row = matches[0]
        base["match_basis"] = match_basis
        base["selected_board_id"] = row.id
        base["selected_external_board_id"] = row.external_board_id
        if not is_current(row):
            base["blockers"] = ["BOARD_SYNC_STALE"]
            return base
        if not row.is_eligible:
            base["blockers"] = ["EXISTING_BOARD_NOT_ELIGIBLE"]
            return base
        base["status"] = "ROUTE_EXISTING"
        base["blockers"] = []
        return base

    request_fp = board_request_fingerprint(
        connection_id=connection.id,
        external_user_id=connection.external_user_id,
        canonical_key=intent["canonical_key"],
        desired_name=intent["desired_name"],
        desired_description=intent["desired_description"],
        privacy=intent["privacy"],
    )
    base["request_fingerprint"] = request_fp

    attempt = _attempt_state(db, request_fp)
    if attempt is not None:
        base["existing_attempt"] = {
            "id": attempt.id,
            "status": attempt.status,
            "provider_board_id": attempt.provider_board_id,
            "provider_mutation_started_at": attempt.provider_mutation_started_at,
            "error_code": attempt.error_code,
        }
        base["blockers"] = [{
            "STARTED": (
                "PROVISIONING_MUTATION_ALREADY_STARTED"
                if attempt.provider_mutation_started_at
                else "PROVISIONING_IN_PROGRESS"
            ),
            "SUCCEEDED": "PROVISIONING_SUCCEEDED_SYNC_REQUIRED",
            "FAILED": "PROVISIONING_FAILED_RETRY_NOT_AUTHORIZED",
            "UNKNOWN": "PROVISIONING_UNKNOWN_RECONCILIATION_REQUIRED",
        }.get(attempt.status, "PROVISIONING_ATTEMPT_STATE_INVALID")]
        return base

    provisioning_blockers: list[str] = []
    if not settings.pinterest_board_write_scope_enabled:
        provisioning_blockers.append("BOARD_WRITE_SCOPE_DISABLED")
    if not settings.pinterest_board_provisioning_enabled:
        provisioning_blockers.append("BOARD_PROVISIONING_DISABLED")
    if "boards:write" not in (connection.granted_scopes or []):
        provisioning_blockers.append("BOARDS_WRITE_SCOPE_REQUIRED")

    base["status"] = "PROVISION_REQUIRED"
    base["provisioning_ready"] = not provisioning_blockers
    base["blockers"] = provisioning_blockers
    return base
