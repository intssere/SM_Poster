from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.models.domain import (
    PinPublication,
    PinterestAnalyticsIngestionRun,
    PinterestAnalyticsSnapshot,
    PinterestConnection,
    PublicationStatus,
)
from app.services.pinterest_oauth import (
    PinterestClient,
    decrypt_token,
    refresh_connection,
)

METRIC_POLICY_VERSION = "PINTEREST_ORGANIC_ANALYTICS_V1"
WINDOW_DAYS = {
    "D1": 1,
    "D7": 7,
    "D30": 30,
    "D90": 90,
}
REQUESTED_METRICS = (
    "IMPRESSION",
    "SAVE",
    "PIN_CLICK",
    "OUTBOUND_CLICK",
    "ENGAGEMENT",
)
METRIC_ALIASES = {
    "impressions": ("IMPRESSION", "IMPRESSIONS"),
    "saves": ("SAVE", "SAVES"),
    "pin_clicks": ("PIN_CLICK", "PIN_CLICKS"),
    "outbound_clicks": ("OUTBOUND_CLICK", "OUTBOUND_CLICKS"),
    "engagements": ("ENGAGEMENT", "ENGAGEMENTS"),
}
RATE_QUANTUM = Decimal("0.000000000001")


class AnalyticsError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PinterestAnalyticsClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    async def fetch_pin_analytics(
        self,
        *,
        access_token: str,
        pin_id: str,
        range_start: date,
        range_end: date,
    ) -> dict[str, Any]:
        settings = get_settings()
        url = f"{settings.pinterest_api_base.rstrip('/')}/pins/{pin_id}/analytics"
        params = {
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "metric_types": ",".join(REQUESTED_METRICS),
            "app_types": "ALL",
            "split_field": "NO_SPLIT",
        }
        owned = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        )
        try:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except (httpx.HTTPError, OSError) as exc:
            raise AnalyticsError("PROVIDER_REQUEST_FAILED") from exc
        finally:
            if owned:
                await client.aclose()

        if not 200 <= response.status_code < 300:
            raise AnalyticsError(f"PROVIDER_HTTP_{response.status_code}")

        try:
            payload = response.json()
        except Exception as exc:
            raise AnalyticsError("PROVIDER_REQUEST_FAILED") from exc
        if not isinstance(payload, dict):
            raise AnalyticsError("MALFORMED_PROVIDER_RESPONSE")
        return payload


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def observation_range(publication: PinPublication, window: str) -> tuple[date, date]:
    days = WINDOW_DAYS.get(window)
    if days is None:
        raise AnalyticsError("INVALID_OBSERVATION_WINDOW")
    if publication.status != PublicationStatus.PUBLISHED or publication.published_at is None:
        raise AnalyticsError("PUBLICATION_NOT_PUBLISHED")
    if not publication.pinterest_pin_id:
        raise AnalyticsError("PINTEREST_PIN_ID_REQUIRED")
    start = _utc(publication.published_at).date()
    return start, start + timedelta(days=days - 1)


def observation_due(publication: PinPublication, window: str, *, now: datetime | None = None) -> bool:
    _, range_end = observation_range(publication, window)
    return _utc(now).date() > range_end


def _normalize_metric_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalyticsError("MALFORMED_PROVIDER_METRIC")
    return value


def normalize_provider_payload(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise AnalyticsError("MALFORMED_PROVIDER_RESPONSE")
    all_bucket = payload.get("all")
    if not isinstance(all_bucket, dict):
        raise AnalyticsError("MALFORMED_PROVIDER_RESPONSE")
    summary = all_bucket.get("summary_metrics")
    if not isinstance(summary, dict):
        raise AnalyticsError("MALFORMED_PROVIDER_RESPONSE")

    normalized: dict[str, int] = {}
    for canonical, aliases in METRIC_ALIASES.items():
        present = [alias for alias in aliases if alias in summary]
        if len(present) > 1:
            raise AnalyticsError("AMBIGUOUS_PROVIDER_METRIC")
        if not present:
            normalized[canonical] = 0
        else:
            normalized[canonical] = _normalize_metric_value(summary[present[0]])
    return normalized


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0").quantize(RATE_QUANTUM)
    try:
        value = Decimal(numerator) / Decimal(denominator)
    except (InvalidOperation, ZeroDivisionError):
        return Decimal("0").quantize(RATE_QUANTUM)
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def derived_rates(metrics: dict[str, int]) -> dict[str, Decimal]:
    impressions = metrics["impressions"]
    return {
        "save_rate": _rate(metrics["saves"], impressions),
        "pin_click_rate": _rate(metrics["pin_clicks"], impressions),
        "outbound_click_rate": _rate(metrics["outbound_clicks"], impressions),
        "engagement_rate": _rate(metrics["engagements"], impressions),
    }


def provider_payload_fingerprint(
    *,
    pinterest_pin_id: str,
    window: str,
    range_start: date,
    range_end: date,
    metrics: dict[str, int],
) -> str:
    payload = {
        "metric_policy_version": METRIC_POLICY_VERSION,
        "observation_window": window,
        "pinterest_pin_id": pinterest_pin_id,
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "metrics": {
            key: int(metrics[key])
            for key in sorted(METRIC_ALIASES)
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_snapshot(db, publication_id: str, window: str) -> PinterestAnalyticsSnapshot | None:
    return db.scalar(
        select(PinterestAnalyticsSnapshot)
        .where(
            PinterestAnalyticsSnapshot.publication_id == publication_id,
            PinterestAnalyticsSnapshot.observation_window == window,
            PinterestAnalyticsSnapshot.metric_policy_version == METRIC_POLICY_VERSION,
        )
        .limit(1)
    )


def analytics_readiness(
    db,
    publication_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    publication = db.get(PinPublication, publication_id)
    if publication is None:
        raise AnalyticsError("PUBLICATION_NOT_FOUND")

    result: dict[str, Any] = {
        "publication_id": publication_id,
        "windows": {},
    }
    prerequisite_error = None
    if publication.status != PublicationStatus.PUBLISHED or publication.published_at is None:
        prerequisite_error = "PUBLICATION_NOT_PUBLISHED"
    elif not publication.pinterest_pin_id:
        prerequisite_error = "PINTEREST_PIN_ID_REQUIRED"

    for window in WINDOW_DAYS:
        if prerequisite_error:
            result["windows"][window] = {
                "ready": False,
                "due": False,
                "collected": False,
                "status": prerequisite_error,
            }
            continue

        range_start, range_end = observation_range(publication, window)
        due = _utc(now).date() > range_end
        snapshot = _existing_snapshot(db, publication_id, window)
        collected = snapshot is not None
        if collected:
            status = "COLLECTED"
        elif due:
            status = "DUE"
        else:
            status = "NOT_DUE"
        result["windows"][window] = {
            "ready": bool(due and not collected),
            "due": due,
            "collected": collected,
            "status": status,
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
            "snapshot_id": snapshot.id if snapshot else None,
        }
    return result


def analytics_summary(db, publication_id: str) -> dict[str, Any]:
    if db.get(PinPublication, publication_id) is None:
        raise AnalyticsError("PUBLICATION_NOT_FOUND")
    rows = list(db.scalars(
        select(PinterestAnalyticsSnapshot)
        .where(PinterestAnalyticsSnapshot.publication_id == publication_id)
        .order_by(PinterestAnalyticsSnapshot.range_start, PinterestAnalyticsSnapshot.observation_window)
    ).all())
    return {
        "publication_id": publication_id,
        "snapshots": [
            {
                "id": row.id,
                "observation_window": row.observation_window,
                "range_start": row.range_start.isoformat(),
                "range_end": row.range_end.isoformat(),
                "provider_payload_fingerprint": row.provider_payload_fingerprint,
                "impressions": row.impressions,
                "saves": row.saves,
                "pin_clicks": row.pin_clicks,
                "outbound_clicks": row.outbound_clicks,
                "engagements": row.engagements,
                "save_rate": f"{Decimal(row.save_rate):.12f}",
                "pin_click_rate": f"{Decimal(row.pin_click_rate):.12f}",
                "outbound_click_rate": f"{Decimal(row.outbound_click_rate):.12f}",
                "engagement_rate": f"{Decimal(row.engagement_rate):.12f}",
                "observed_at": _utc(row.observed_at).isoformat(),
                "finalized_at": _utc(row.finalized_at).isoformat(),
            }
            for row in rows
        ],
    }


def _mark_failed(
    db,
    run: PinterestAnalyticsIngestionRun,
    code: str,
    *,
    now: datetime,
    fingerprint: str | None = None,
    provider_called: bool | None = None,
) -> None:
    run.status = "FAILED"
    run.completed_at = now
    run.error_code = code
    if fingerprint:
        run.provider_payload_fingerprint = fingerprint
    metadata = dict(run.safe_metadata or {})
    if provider_called is not None:
        metadata["provider_called"] = provider_called
    run.safe_metadata = metadata
    db.commit()


def _connected_account(db) -> PinterestConnection:
    rows = list(db.scalars(
        select(PinterestConnection)
        .where(PinterestConnection.status == "CONNECTED")
        .order_by(PinterestConnection.created_at, PinterestConnection.id)
    ).all())
    if len(rows) != 1:
        raise AnalyticsError("CONNECTED_ACCOUNT_COUNT_INVALID")
    connection = rows[0]
    if not connection.access_token_ciphertext:
        raise AnalyticsError("ACCESS_TOKEN_REQUIRED")
    if "pins:read" not in (connection.granted_scopes or []):
        raise AnalyticsError("PINS_READ_SCOPE_REQUIRED")
    return connection


async def ingest_publication_analytics(
    db,
    publication_id: str,
    window: str,
    *,
    settings: Settings | None = None,
    client: PinterestAnalyticsClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if settings.pinterest_analytics_ingestion_enabled is not True:
        raise AnalyticsError("ANALYTICS_INGESTION_DISABLED")

    publication = db.get(PinPublication, publication_id)
    if publication is None:
        raise AnalyticsError("PUBLICATION_NOT_FOUND")
    if window not in WINDOW_DAYS:
        raise AnalyticsError("INVALID_OBSERVATION_WINDOW")

    range_start, range_end = observation_range(publication, window)
    now = _utc(now)
    if now.date() <= range_end:
        raise AnalyticsError("OBSERVATION_NOT_DUE")

    run = PinterestAnalyticsIngestionRun(
        publication_id=publication.id,
        observation_window=window,
        pinterest_pin_id=publication.pinterest_pin_id,
        range_start=range_start,
        range_end=range_end,
        status="STARTED",
        started_at=now,
        safe_metadata={
            "metric_policy_version": METRIC_POLICY_VERSION,
            "provider_called": False,
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    fingerprint: str | None = None
    try:
        connection = _connected_account(db)

        expiry = connection.access_token_expires_at
        if expiry is not None and _utc(expiry) <= now + timedelta(minutes=5):
            connection = await refresh_connection(db, connection, PinterestClient())

        try:
            access_token = decrypt_token(connection.access_token_ciphertext)
        except Exception as exc:
            raise AnalyticsError("ACCESS_TOKEN_REQUIRED") from exc

        provider = client or PinterestAnalyticsClient()
        run.safe_metadata = {
            "metric_policy_version": METRIC_POLICY_VERSION,
            "provider_called": True,
        }
        db.commit()

        payload = await provider.fetch_pin_analytics(
            access_token=access_token,
            pin_id=publication.pinterest_pin_id,
            range_start=range_start,
            range_end=range_end,
        )
        metrics = normalize_provider_payload(payload)
        rates = derived_rates(metrics)
        fingerprint = provider_payload_fingerprint(
            pinterest_pin_id=publication.pinterest_pin_id,
            window=window,
            range_start=range_start,
            range_end=range_end,
            metrics=metrics,
        )

        existing = _existing_snapshot(db, publication.id, window)
        if existing is not None:
            if existing.provider_payload_fingerprint != fingerprint:
                _mark_failed(
                    db,
                    run,
                    "ANALYTICS_SNAPSHOT_CONFLICT",
                    now=now,
                    fingerprint=fingerprint,
                    provider_called=True,
                )
                raise AnalyticsError("ANALYTICS_SNAPSHOT_CONFLICT")
            run.status = "SUCCEEDED"
            run.completed_at = now
            run.snapshot_id = existing.id
            run.provider_payload_fingerprint = fingerprint
            run.error_code = None
            db.commit()
            return {
                "status": "IDEMPOTENT",
                "snapshot_id": existing.id,
                "run_id": run.id,
            }

        snapshot = PinterestAnalyticsSnapshot(
            publication_id=publication.id,
            pinterest_pin_id=publication.pinterest_pin_id,
            metric_policy_version=METRIC_POLICY_VERSION,
            observation_window=window,
            range_start=range_start,
            range_end=range_end,
            provider_payload_fingerprint=fingerprint,
            impressions=metrics["impressions"],
            saves=metrics["saves"],
            pin_clicks=metrics["pin_clicks"],
            outbound_clicks=metrics["outbound_clicks"],
            engagements=metrics["engagements"],
            save_rate=rates["save_rate"],
            pin_click_rate=rates["pin_click_rate"],
            outbound_click_rate=rates["outbound_click_rate"],
            engagement_rate=rates["engagement_rate"],
            safe_metric_map=dict(metrics),
            observed_at=now,
            finalized_at=now,
        )
        try:
            with db.begin_nested():
                db.add(snapshot)
                db.flush()
        except IntegrityError:
            existing = _existing_snapshot(db, publication.id, window)
            if existing is not None and existing.provider_payload_fingerprint == fingerprint:
                run.status = "SUCCEEDED"
                run.completed_at = now
                run.snapshot_id = existing.id
                run.provider_payload_fingerprint = fingerprint
                run.error_code = None
                db.commit()
                return {
                    "status": "IDEMPOTENT",
                    "snapshot_id": existing.id,
                    "run_id": run.id,
                }
            _mark_failed(
                db,
                run,
                "ANALYTICS_SNAPSHOT_CONFLICT",
                now=now,
                fingerprint=fingerprint,
                provider_called=True,
            )
            raise AnalyticsError("ANALYTICS_SNAPSHOT_CONFLICT")

        run.status = "SUCCEEDED"
        run.completed_at = now
        run.snapshot_id = snapshot.id
        run.provider_payload_fingerprint = fingerprint
        run.error_code = None
        db.commit()
        db.refresh(snapshot)
        return {
            "status": "SUCCEEDED",
            "snapshot_id": snapshot.id,
            "run_id": run.id,
        }
    except AnalyticsError as exc:
        if run.status == "STARTED":
            _mark_failed(
                db,
                run,
                exc.code,
                now=now,
                fingerprint=fingerprint,
                provider_called=bool((run.safe_metadata or {}).get("provider_called")),
            )
        raise
    except Exception as exc:
        if run.status == "STARTED":
            _mark_failed(
                db,
                run,
                "ANALYTICS_READ_FAILED",
                now=now,
                fingerprint=fingerprint,
                provider_called=bool((run.safe_metadata or {}).get("provider_called")),
            )
        raise AnalyticsError("ANALYTICS_READ_FAILED") from exc
