"""Allowlisted capture diagnostics. Exception text is never a diagnostic."""
from __future__ import annotations

from enum import Enum
import re


class CaptureStage(str, Enum):
    CONNECT = "CONNECT"
    SESSION_SETUP = "SESSION_SETUP"
    EXECUTE_SINGLE_STATEMENT = "EXECUTE_SINGLE_STATEMENT"
    FETCH_ONE_ROW = "FETCH_ONE_ROW"
    VALIDATE_SINGLE_ROW = "VALIDATE_SINGLE_ROW"
    WRITE_CAPSULE_FILE = "WRITE_CAPSULE_FILE"
    HASH_CAPSULE = "HASH_CAPSULE"
    OFFLINE_WRAP = "OFFLINE_WRAP"
    ROLLBACK_CLOSE = "ROLLBACK/CLOSE"


def _attribute(obj, name):
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def safe_diagnostic(exc: Exception, stage: CaptureStage) -> dict:
    """Read only class name, SQLSTATE, and statement position; never str/repr."""
    name = _attribute(type(exc), "__name__")
    if type(name) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
        name = "Exception"
    result = {"stage": stage.value, "exception_class": name}
    sqlstate = _attribute(exc, "sqlstate")
    if type(sqlstate) is str and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
        result["sqlstate"] = sqlstate
    position = _attribute(_attribute(exc, "diag"), "statement_position")
    if type(position) is str and re.fullmatch(r"[1-9][0-9]{0,9}", position):
        position = int(position)
    if type(position) is int and 0 < position <= 2_147_483_647:
        result["statement_position"] = position
    return result