"""Synthetic complete inventory. Only caller-created disposable PostgreSQL."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa

from app.state_transfer.catalog import dependencies
from app.state_transfer.policy import FOUNDATION_FKS, SOURCE

STAMP = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
CIPHERTEXT = "opaque-not-a-fernet-token-DO-NOT-LOG"


def identity(table, index=0):
    return str(uuid5(NAMESPACE_URL, f"offline-transfer/{table}/{index}"))


def seed_inventory(engine):
    with engine.begin() as c:
        # Exact inventoried managed-source keys; clean 0031's historical column
        # clones omit these eight. This fixture never changes target migrations.
        for child, column, parent, constraint, ondelete in FOUNDATION_FKS:
            c.execute(sa.text(
                f'ALTER TABLE public."{child}" ADD CONSTRAINT "{constraint}" '
                f'FOREIGN KEY ("{column}") REFERENCES public."{parent}" (id)' +
                (f" ON DELETE {ondelete}" if ondelete else "")
            ))
        metadata, order, _, _ = dependencies(c)
        for name in order:
            table = metadata.tables[f"public.{name}"]
            if name == "routine_publishing_control":
                c.execute(table.update().values(updated_at=STAMP))
                continue
            rows = []
            for i in range(SOURCE[name].count):
                row = {}
                for col in table.columns:
                    key, typ = col.name, col.type
                    if key == "id":
                        value = identity(name, i)
                    elif col.nullable:
                        value = None
                    elif isinstance(typ, sa.Enum):
                        value = typ.enums[0]
                    elif isinstance(typ, sa.Boolean):
                        value = False
                    elif isinstance(typ, sa.Integer):
                        value = i + 1
                    elif isinstance(typ, sa.Numeric):
                        value = Decimal("1.234567")
                    elif isinstance(typ, sa.DateTime):
                        value = STAMP
                    elif isinstance(typ, sa.Date):
                        value = date(2026, 10, 3)
                    elif isinstance(typ, sa.JSON):
                        value = {"fixture": True}
                    elif isinstance(typ, sa.String):
                        value = hashlib.sha256(f"{name}/{key}/{i}".encode()).hexdigest()
                        value = value[:typ.length] if typ.length else value
                    else:
                        raise AssertionError(f"Unreviewed fixture type {name}/{key}")
                    row[key] = value
                for fk in table.foreign_key_constraints:
                    for e in fk.elements:
                        remote = e.column.table.name
                        if remote != name and not e.parent.nullable:
                            if e.column.name == "id":
                                row[e.parent.name] = identity(remote, i % SOURCE[remote].count)
                row.update(overrides(name, i))
                if name == "content_revisions" and i:
                    row["parent_revision_id"] = identity(name, i - 1)
                if name.startswith("pinterest_autonomous_") and name.endswith("_runs") and i:
                    row["supersedes_run_id"] = identity(name, i - 1)
                rows.append(row)
            if rows:
                c.execute(table.insert(), rows)
        # Exercise JSON null vs SQL NULL, decimal precision, escaped Unicode,
        # timestamps and nested opaque strings without decrypting anything.
        c.execute(sa.text(
            "UPDATE public.pin_creatives SET render_spec='null'::json WHERE id=:id"
        ), {"id": identity("pin_creatives")})
        c.execute(sa.text(
            "UPDATE public.products SET price_min=1234567.12,"
            "shopify_data=CAST(:data AS json) WHERE id=:id"
        ), {"id": identity("products"),
            "data": '{"precise":1234567890123.12345678901234567890}'})


def overrides(name, i):
    common = {
        "catalog_sync_jobs": {"status": "SUCCEEDED"},
        "pin_drafts": {"status": "APPROVED" if i < 8 else "READY_FOR_REVIEW"},
        "pin_creatives": {
            "render_status": "RENDERED", "rendered_url": f"/media/offline/{i}.png",
            "sha256": hashlib.sha256(f"offline-png-{i}".encode()).hexdigest(),
        },
        "pin_approvals": {"decision": "APPROVED"},
        "pin_publications": {
            "status": "PUBLISHED" if i < 4 else "CANCELLED",
            "media_url_snapshot": f"https://media.fixture.invalid/{i}.png?opaque=signed",
        },
        "buffer_pilot_activations": {"status": "CONSUMED", "consumed_at": STAMP},
        "publication_dispatch_authorizations": {
            "status": "CONSUMED" if i < 6 else "EXPIRED",
            "consumed_at": STAMP if i < 6 else None,
        },
        "routine_dispatch_permits": {
            "status": "CONSUMED" if i < 3 else "EXPIRED",
            "consumed_at": STAMP if i < 3 else None, "dispatch_provider": "buffer",
        },
        "routine_publishing_runs": {"status": "SUCCEEDED", "mode": "DRY_RUN" if i == 0 else "LIVE"},
        "publication_attempts": {
            "status": "SUCCEEDED" if i < 4 else ("UNKNOWN" if i < 7 else "FAILED"),
            "dispatch_provider": "buffer" if i < 7 else "pinterest_direct",
            "provider_operation_id": f"offline-operation-{i}" if i < 4 else None,
        },
        "publication_reconciliation_events": {
            "action": "PROVIDER_PIN_CONFIRMED" if i < 4 else "CANCELLED_UNKNOWN",
            "previous_status": "PUBLISH_UNKNOWN", "new_status": "PUBLISHED" if i < 4 else "CANCELLED",
            "provider": "buffer" if i < 4 else "pinterest_direct",
        },
        "pinterest_connections": {
            "status": "CONNECTED" if i == 0 else "DISCONNECTED",
            "access_token_ciphertext": CIPHERTEXT if i == 0 else "",
            "refresh_token_ciphertext": CIPHERTEXT if i == 0 else "",
        },
        "pinterest_portfolio_plans": {"status": "ACTIVE"},
        "pinterest_portfolio_plan_items": {"status": "SCHEDULED" if i == 0 else "PLANNED"},
        "pinterest_optimizer_applications": {"status": "APPLIED"},
        "pinterest_seo_briefs": {"status": "CURRENT"},
        "pinterest_autonomous_run_reconciliations": {"status": "RECONCILED"},
        "pinterest_autonomous_generation_runs": {"status": "SUCCEEDED" if i == 0 else "FAILED"},
        "pinterest_autonomous_execution_runs": {"status": "SUCCEEDED" if i == 0 else "FAILED", "stage": "PERMITTED"},
        "pinterest_autonomous_destination_runs": {"status": "SUCCEEDED" if i == 0 else "FAILED", "stage": "EXECUTION_READY"},
    }
    return common.get(name, {})