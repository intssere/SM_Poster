import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    PinPublication,
    PinterestAnalyticsIngestionRun,
    PinterestAnalyticsSnapshot,
    PinterestConnection,
    PublicationStatus,
)
from app.services import pinterest_performance_analytics as analytics


PUBLISHED_AT = datetime(2026, 9, 1, 14, 35, tzinfo=timezone.utc)


def _db():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "pinterest_analytics_ingestion_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def _publication(
    db,
    *,
    publication_id="pub-1",
    pin_id="pin-1",
    status=PublicationStatus.PUBLISHED,
    published_at=PUBLISHED_AT,
):
    row = PinPublication(
        id=publication_id,
        draft_id="draft-1",
        creative_id="creative-1",
        publication_fingerprint=(publication_id.replace("-", "") + "f" * 64)[:64],
        status=status,
        pinterest_pin_id=pin_id,
        published_at=published_at,
    )
    db.add(row)
    db.commit()
    return row


def _connection(db, *, scopes=None, connection_id="conn-1", expiry=None):
    row = PinterestConnection(
        id=connection_id,
        external_user_id=f"user-{connection_id}",
        username="diamond-shelf",
        granted_scopes=scopes if scopes is not None else ["user_accounts:read", "boards:read", "pins:read"],
        access_token_ciphertext="cipher-access",
        refresh_token_ciphertext="cipher-refresh",
        access_token_expires_at=expiry,
        status="CONNECTED",
    )
    db.add(row)
    db.commit()
    return row


class FakeAnalyticsClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {
            "all": {
                "summary_metrics": {
                    "IMPRESSION": 100,
                    "SAVE": 10,
                    "PIN_CLICK": 5,
                    "OUTBOUND_CLICK": 2,
                    "ENGAGEMENT": 20,
                }
            }
        }
        self.error = error
        self.calls = []

    async def fetch_pin_analytics(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.payload


@pytest.mark.parametrize(
    "window,expected_end,first_due",
    [
        ("D1", "2026-09-01", "2026-09-02"),
        ("D7", "2026-09-07", "2026-09-08"),
        ("D30", "2026-09-30", "2026-10-01"),
        ("D90", "2026-11-29", "2026-11-30"),
    ],
)
def test_observation_windows_use_utc_calendar_boundaries(window, expected_end, first_due):
    engine, db = _db()
    publication = _publication(db)
    start, end = analytics.observation_range(publication, window)
    assert start.isoformat() == "2026-09-01"
    assert end.isoformat() == expected_end
    assert analytics.observation_due(
        publication,
        window,
        now=datetime.fromisoformat(expected_end).replace(tzinfo=timezone.utc),
    ) is False
    assert analytics.observation_due(
        publication,
        window,
        now=datetime.fromisoformat(first_due).replace(tzinfo=timezone.utc),
    ) is True
    db.close()
    engine.dispose()


def test_naive_published_at_is_treated_as_utc():
    engine, db = _db()
    publication = _publication(
        db,
        published_at=datetime(2026, 9, 1, 23, 59),
    )
    start, end = analytics.observation_range(publication, "D1")
    assert start.isoformat() == "2026-09-01"
    assert end.isoformat() == "2026-09-01"
    db.close()
    engine.dispose()


@pytest.mark.parametrize(
    "status,published_at,pin_id,code",
    [
        (PublicationStatus.SCHEDULED, PUBLISHED_AT, "pin-1", "PUBLICATION_NOT_PUBLISHED"),
        (PublicationStatus.PUBLISHED, None, "pin-1", "PUBLICATION_NOT_PUBLISHED"),
        (PublicationStatus.PUBLISHED, PUBLISHED_AT, None, "PINTEREST_PIN_ID_REQUIRED"),
    ],
)
def test_publication_prerequisites_fail_closed(status, published_at, pin_id, code):
    engine, db = _db()
    publication = _publication(db, status=status, published_at=published_at, pin_id=pin_id)
    with pytest.raises(analytics.AnalyticsError, match=code):
        analytics.observation_range(publication, "D1")
    db.close()
    engine.dispose()


def test_normalize_provider_payload_and_rates():
    payload = {
        "all": {
            "summary_metrics": {
                "IMPRESSION": 100,
                "SAVES": 10,
                "PIN_CLICK": 5,
                "OUTBOUND_CLICKS": 2,
                "ENGAGEMENT": 20,
            }
        }
    }
    metrics = analytics.normalize_provider_payload(payload)
    assert metrics == {
        "impressions": 100,
        "saves": 10,
        "pin_clicks": 5,
        "outbound_clicks": 2,
        "engagements": 20,
    }
    rates = analytics.derived_rates(metrics)
    assert rates["save_rate"] == Decimal("0.100000000000")
    assert rates["pin_click_rate"] == Decimal("0.050000000000")
    assert rates["outbound_click_rate"] == Decimal("0.020000000000")
    assert rates["engagement_rate"] == Decimal("0.200000000000")


def test_missing_metrics_become_zero_only_on_valid_success_shape():
    metrics = analytics.normalize_provider_payload({"all": {"summary_metrics": {"IMPRESSION": 0}}})
    assert metrics == {
        "impressions": 0,
        "saves": 0,
        "pin_clicks": 0,
        "outbound_clicks": 0,
        "engagements": 0,
    }
    assert all(
        rate == Decimal("0.000000000000")
        for rate in analytics.derived_rates(metrics).values()
    )


@pytest.mark.parametrize(
    "payload,code",
    [
        (None, "MALFORMED_PROVIDER_RESPONSE"),
        ({}, "MALFORMED_PROVIDER_RESPONSE"),
        ({"all": []}, "MALFORMED_PROVIDER_RESPONSE"),
        ({"all": {"summary_metrics": []}}, "MALFORMED_PROVIDER_RESPONSE"),
        (
            {"all": {"summary_metrics": {"IMPRESSION": 1, "IMPRESSIONS": 1}}},
            "AMBIGUOUS_PROVIDER_METRIC",
        ),
        (
            {"all": {"summary_metrics": {"IMPRESSION": True}}},
            "MALFORMED_PROVIDER_METRIC",
        ),
        (
            {"all": {"summary_metrics": {"IMPRESSION": "1"}}},
            "MALFORMED_PROVIDER_METRIC",
        ),
        (
            {"all": {"summary_metrics": {"IMPRESSION": 1.0}}},
            "MALFORMED_PROVIDER_METRIC",
        ),
        (
            {"all": {"summary_metrics": {"IMPRESSION": -1}}},
            "MALFORMED_PROVIDER_METRIC",
        ),
    ],
)
def test_malformed_provider_metrics_fail_closed(payload, code):
    with pytest.raises(analytics.AnalyticsError, match=code):
        analytics.normalize_provider_payload(payload)


def test_provider_fingerprint_is_deterministic_and_bound_to_window():
    metrics = {
        "impressions": 100,
        "saves": 10,
        "pin_clicks": 5,
        "outbound_clicks": 2,
        "engagements": 20,
    }
    a = analytics.provider_payload_fingerprint(
        pinterest_pin_id="pin-1",
        window="D1",
        range_start=PUBLISHED_AT.date(),
        range_end=PUBLISHED_AT.date(),
        metrics=metrics,
    )
    b = analytics.provider_payload_fingerprint(
        pinterest_pin_id="pin-1",
        window="D1",
        range_start=PUBLISHED_AT.date(),
        range_end=PUBLISHED_AT.date(),
        metrics=dict(reversed(list(metrics.items()))),
    )
    c = analytics.provider_payload_fingerprint(
        pinterest_pin_id="pin-1",
        window="D7",
        range_start=PUBLISHED_AT.date(),
        range_end=PUBLISHED_AT.date() + timedelta(days=6),
        metrics=metrics,
    )
    assert a == b
    assert len(a) == 64
    assert c != a


def test_exact_http_request_contract_and_no_write(monkeypatch):
    seen = {}

    async def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"all": {"summary_metrics": {"IMPRESSION": 1}}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = analytics.PinterestAnalyticsClient(client)
    payload = asyncio.run(
        provider.fetch_pin_analytics(
            access_token="secret-token",
            pin_id="12345",
            range_start=PUBLISHED_AT.date(),
            range_end=PUBLISHED_AT.date() + timedelta(days=6),
        )
    )
    asyncio.run(client.aclose())

    assert payload["all"]["summary_metrics"]["IMPRESSION"] == 1
    assert seen["method"] == "GET"
    assert seen["path"].endswith("/pins/12345/analytics")
    assert seen["query"] == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-07",
        "metric_types": "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK,ENGAGEMENT",
        "app_types": "ALL",
        "split_field": "NO_SPLIT",
    }
    assert seen["authorization"] == "Bearer secret-token"


@pytest.mark.parametrize("status,code", [(400, "PROVIDER_HTTP_400"), (500, "PROVIDER_HTTP_500")])
def test_http_non_2xx_is_sanitized(status, code):
    async def handler(request):
        return httpx.Response(status, text="secret provider body")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = analytics.PinterestAnalyticsClient(client)
    with pytest.raises(analytics.AnalyticsError, match=code):
        asyncio.run(
            provider.fetch_pin_analytics(
                access_token="secret-token",
                pin_id="pin-1",
                range_start=PUBLISHED_AT.date(),
                range_end=PUBLISHED_AT.date(),
            )
        )
    asyncio.run(client.aclose())


def test_ingestion_disabled_blocks_before_audit_run():
    engine, db = _db()
    publication = _publication(db)
    with pytest.raises(analytics.AnalyticsError, match="ANALYTICS_INGESTION_DISABLED"):
        asyncio.run(
            analytics.ingest_publication_analytics(
                db,
                publication.id,
                "D1",
                settings=_settings(pinterest_analytics_ingestion_enabled=False),
                now=PUBLISHED_AT + timedelta(days=1),
            )
        )
    assert db.query(PinterestAnalyticsIngestionRun).count() == 0
    db.close()
    engine.dispose()


def test_not_due_blocks_before_provider_or_run(monkeypatch):
    engine, db = _db()
    publication = _publication(db)
    _connection(db)
    monkeypatch.setattr(analytics, "decrypt_token", lambda _: "token")
    client = FakeAnalyticsClient()

    with pytest.raises(analytics.AnalyticsError, match="OBSERVATION_NOT_DUE"):
        asyncio.run(
            analytics.ingest_publication_analytics(
                db,
                publication.id,
                "D7",
                settings=_settings(),
                client=client,
                now=PUBLISHED_AT + timedelta(days=6),
            )
        )

    assert client.calls == []
    assert db.query(PinterestAnalyticsIngestionRun).count() == 0
    db.close()
    engine.dispose()


@pytest.mark.parametrize(
    "connection_setup,code",
    [
        ("none", "CONNECTED_ACCOUNT_COUNT_INVALID"),
        ("two", "CONNECTED_ACCOUNT_COUNT_INVALID"),
        ("no_scope", "PINS_READ_SCOPE_REQUIRED"),
    ],
)
def test_account_and_scope_gates_are_audited_failures(monkeypatch, connection_setup, code):
    engine, db = _db()
    publication = _publication(db)
    if connection_setup == "two":
        _connection(db, connection_id="conn-1")
        _connection(db, connection_id="conn-2")
    elif connection_setup == "no_scope":
        _connection(db, scopes=["user_accounts:read", "boards:read"])
    monkeypatch.setattr(analytics, "decrypt_token", lambda _: "token")
    client = FakeAnalyticsClient()

    with pytest.raises(analytics.AnalyticsError, match=code):
        asyncio.run(
            analytics.ingest_publication_analytics(
                db,
                publication.id,
                "D1",
                settings=_settings(),
                client=client,
                now=PUBLISHED_AT + timedelta(days=1),
            )
        )

    run = db.query(PinterestAnalyticsIngestionRun).one()
    assert run.status == "FAILED"
    assert run.error_code == code
    assert run.safe_metadata["provider_called"] is False
    assert client.calls == []
    assert db.query(PinterestAnalyticsSnapshot).count() == 0
    db.close()
    engine.dispose()


def test_successful_ingestion_persists_immutable_snapshot(monkeypatch):
    engine, db = _db()
    publication = _publication(db)
    _connection(db)
    monkeypatch.setattr(analytics, "decrypt_token", lambda _: "token")
    client = FakeAnalyticsClient()

    result = asyncio.run(
        analytics.ingest_publication_analytics(
            db,
            publication.id,
            "D1",
            settings=_settings(),
            client=client,
            now=PUBLISHED_AT + timedelta(days=1),
        )
    )

    assert result["status"] == "SUCCEEDED"
    snapshot = db.get(PinterestAnalyticsSnapshot, result["snapshot_id"])
    run = db.get(PinterestAnalyticsIngestionRun, result["run_id"])
    assert snapshot.metric_policy_version == analytics.METRIC_POLICY_VERSION
    assert snapshot.observation_window == "D1"
    assert snapshot.impressions == 100
    assert snapshot.save_rate == Decimal("0.100000000000")
    assert snapshot.safe_metric_map["outbound_clicks"] == 2
    assert run.status == "SUCCEEDED"
    assert run.snapshot_id == snapshot.id
    assert run.safe_metadata["provider_called"] is True
    assert len(client.calls) == 1
    db.close()
    engine.dispose()


def test_exact_repeat_is_idempotent(monkeypatch):
    engine, db = _db()
    publication = _publication(db)
    _connection(db)
    monkeypatch.setattr(analytics, "decrypt_token", lambda _: "token")
    client = FakeAnalyticsClient()

    first = asyncio.run(
        analytics.ingest_publication_analytics(
            db,
            publication.id,
            "D1",
            settings=_settings(),
            client=client,
            now=PUBLISHED_AT + timedelta(days=1),
        )
    )
    second = asyncio.run(
        analytics.ingest_publication_analytics(
            db,
            publication.id,
            "D1",
            settings=_settings(),
            client=client,
            now=PUBLISHED_AT + timedelta(days=2),
        )
    )

    assert first["status"] == "SUCCEEDED"
    assert second["status"] == "IDEMPOTENT"
    assert second["snapshot_id"] == first["snapshot_id"]
    assert db.query(PinterestAnalyticsSnapshot).count() == 1
    assert db.query(PinterestAnalyticsIngestionRun).count() == 2
    assert all(
        row.status == "SUCCEEDED"
        for row in db.query(PinterestAnalyticsIngestionRun).all()
    )
    db.close()
    engine.dispose()


def test_conflicting_finalized_observation_fails_closed(monkeypatch):
    engine, db = _db()
    publication = _publication(db)
    _connection(db)
    monkeypatch.setattr(analytics, "decrypt_token", lambda _: "token")

    asyncio.run(
        analytics.ingest_publication_analytics(
            db,
            publication.id,
            "D1",
            settings=_settings(),
            client=FakeAnalyticsClient(),
            now=PUBLISHED_AT + timedelta(days=1),
        )
    )
    conflicting = FakeAnalyticsClient(
        payload={
            "all": {
                "summary_metrics": {
                    "IMPRESSION": 101,
                    "SAVE": 10,
                    "PIN_CLICK": 5,
                    "OUTBOUND_CLICK": 2,
                    "ENGAGEMENT": 20,
                }
            }
        }
    )

    with pytest.raises(analytics.AnalyticsError, match="ANALYTICS_SNAPSHOT_CONFLICT"):
        asyncio.run(
            analytics.ingest_publication_analytics(
                db,
                publication.id,
                "D1",
                settings=_settings(),
                client=conflicting,
                now=PUBLISHED_AT + timedelta(days=2),
            )
        )

    assert db.query(PinterestAnalyticsSnapshot).count() == 1
    runs = db.query(PinterestAnalyticsIngestionRun).order_by(PinterestAnalyticsIngestionRun.created_at).all()
    assert runs[-1].status == "FAILED"
    assert runs[-1].error_code == "ANALYTICS_SNAPSHOT_CONFLICT"
    assert runs[-1].provider_payload_fingerprint
    db.close()
    engine.dispose()


def test_provider_failure_records_failed_run_without_snapshot(monkeypatch):
    engine, db = _db()
    publication = _publication(db)
    _connection(db)
    monkeypatch.setattr(analytics, "decrypt_token", lambda _: "token")
    client = FakeAnalyticsClient(error=analytics.AnalyticsError("PROVIDER_HTTP_503"))

    with pytest.raises(analytics.AnalyticsError, match="PROVIDER_HTTP_503"):
        asyncio.run(
            analytics.ingest_publication_analytics(
                db,
                publication.id,
                "D1",
                settings=_settings(),
                client=client,
                now=PUBLISHED_AT + timedelta(days=1),
            )
        )

    run = db.query(PinterestAnalyticsIngestionRun).one()
    assert run.status == "FAILED"
    assert run.error_code == "PROVIDER_HTTP_503"
    assert run.safe_metadata["provider_called"] is True
    assert db.query(PinterestAnalyticsSnapshot).count() == 0
    db.close()
    engine.dispose()


def test_readiness_and_summary_are_provider_free(monkeypatch):
    engine, db = _db()
    publication = _publication(db)
    called = []

    async def forbidden(*args, **kwargs):
        called.append(1)
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(analytics.PinterestAnalyticsClient, "fetch_pin_analytics", forbidden)

    readiness = analytics.analytics_readiness(
        db,
        publication.id,
        now=PUBLISHED_AT + timedelta(days=8),
    )
    assert readiness["windows"]["D1"]["status"] == "DUE"
    assert readiness["windows"]["D7"]["status"] == "DUE"
    assert readiness["windows"]["D30"]["status"] == "NOT_DUE"
    assert readiness["windows"]["D90"]["status"] == "NOT_DUE"

    summary = analytics.analytics_summary(db, publication.id)
    assert summary == {"publication_id": publication.id, "snapshots": []}
    assert called == []
    db.close()
    engine.dispose()


def test_readiness_reports_prerequisite_errors_without_provider():
    engine, db = _db()
    publication = _publication(
        db,
        status=PublicationStatus.SCHEDULED,
        pin_id=None,
        published_at=None,
    )
    result = analytics.analytics_readiness(db, publication.id)
    assert set(result["windows"]) == {"D1", "D7", "D30", "D90"}
    assert all(
        item["status"] == "PUBLICATION_NOT_PUBLISHED"
        and item["ready"] is False
        for item in result["windows"].values()
    )
    db.close()
    engine.dispose()


def test_unknown_publication_summary_and_readiness_fail_closed():
    engine, db = _db()
    with pytest.raises(analytics.AnalyticsError, match="PUBLICATION_NOT_FOUND"):
        analytics.analytics_readiness(db, "missing")
    with pytest.raises(analytics.AnalyticsError, match="PUBLICATION_NOT_FOUND"):
        analytics.analytics_summary(db, "missing")
    db.close()
    engine.dispose()
