import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_0015",
        ROOT / "alembic/versions/0015_manual_dispatch_readiness.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_0014_to_0015_adds_authorization_and_reconciliation_audit_without_fabricating_history():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    meta = sa.MetaData()
    sa.Table(
        "pin_publications",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
    )
    sa.Table(
        "publication_attempts",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("pin_publications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("safe_response_metadata", sa.JSON(), nullable=False),
        sa.UniqueConstraint("publication_id", "attempt_number", name="uq_publication_attempt_number"),
    )
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        meta.create_all(conn)
        conn.execute(
            sa.text("INSERT INTO pin_publications(id, publication_fingerprint, status) VALUES ('p1', :fp, 'PUBLISH_UNKNOWN')"),
            {"fp": "a" * 64},
        )
        conn.execute(
            sa.text("INSERT INTO publication_attempts(id, publication_id, attempt_number, status, safe_response_metadata) VALUES ('att1','p1',1,'UNKNOWN','{}')")
        )

        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()

        insp = sa.inspect(conn)
        assert "publication_dispatch_authorizations" in insp.get_table_names()
        assert "publication_reconciliation_events" in insp.get_table_names()
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM pin_publications WHERE id='p1'")) == 1
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM publication_attempts WHERE id='att1'")) == 1
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM publication_dispatch_authorizations")) == 0
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM publication_reconciliation_events")) == 0

        now = datetime.now(timezone.utc).isoformat()
        expires = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        conn.execute(
            sa.text(
                """
                INSERT INTO publication_dispatch_authorizations(
                    id, publication_id, authorized_by, authorized_at, publication_fingerprint,
                    quality_policy_version, quality_snapshot, readiness_snapshot,
                    duplicate_snapshot, confirmation_text_version, expires_at, status
                ) VALUES (
                    'auth1', 'p1', 'admin', :now, :fp, 'PINTEREST_QUALITY_V1',
                    '{}', '{}', '{}', 'CONFIRM_V1', :expires, 'ACTIVE'
                )
                """
            ),
            {"now": now, "expires": expires, "fp": "a" * 64},
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO publication_reconciliation_events(
                    id, publication_id, attempt_id, actor, action, previous_status,
                    new_status, provider_pin_id, reason
                ) VALUES (
                    'rec1', 'p1', 'att1', 'admin', 'PROVIDER_PIN_CONFIRMED',
                    'PUBLISH_UNKNOWN', 'PUBLISHED', 'pin-123', 'operator confirmed'
                )
                """
            )
        )
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM publication_dispatch_authorizations WHERE publication_id='p1'")) == 1
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM publication_reconciliation_events WHERE publication_id='p1'")) == 1

        with pytest.raises(IntegrityError):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO publication_dispatch_authorizations(
                        id, publication_id, authorized_by, authorized_at, publication_fingerprint,
                        quality_policy_version, quality_snapshot, readiness_snapshot,
                        duplicate_snapshot, confirmation_text_version, expires_at, status
                    ) VALUES (
                        'auth2', 'p1', 'admin', :now, :fp, 'PINTEREST_QUALITY_V1',
                        '{}', '{}', '{}', 'CONFIRM_V1', :expires, 'ACTIVE'
                    )
                    """
                ),
                {"now": now, "expires": expires, "fp": "a" * 64},
            )

        conn.execute(sa.text("UPDATE publication_dispatch_authorizations SET status='CONSUMED' WHERE id='auth1'"))
        conn.execute(
            sa.text(
                """
                INSERT INTO publication_dispatch_authorizations(
                    id, publication_id, authorized_by, authorized_at, publication_fingerprint,
                    quality_policy_version, quality_snapshot, readiness_snapshot,
                    duplicate_snapshot, confirmation_text_version, expires_at, status
                ) VALUES (
                    'auth3', 'p1', 'admin', :now, :fp, 'PINTEREST_QUALITY_V1',
                    '{}', '{}', '{}', 'CONFIRM_V1', :expires, 'ACTIVE'
                )
                """
            ),
            {"now": now, "expires": expires, "fp": "a" * 64},
        )

        with pytest.raises(IntegrityError):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO publication_dispatch_authorizations(
                        id, publication_id, authorized_by, authorized_at, publication_fingerprint,
                        quality_policy_version, quality_snapshot, readiness_snapshot,
                        duplicate_snapshot, confirmation_text_version, expires_at, status
                    ) VALUES (
                        'bad-auth', 'missing-publication', 'admin', :now, :fp, 'PINTEREST_QUALITY_V1',
                        '{}', '{}', '{}', 'CONFIRM_V1', :expires, 'ACTIVE'
                    )
                    """
                ),
                {"now": now, "expires": expires, "fp": "a" * 64},
            )

        with pytest.raises(IntegrityError):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO publication_reconciliation_events(
                        id, publication_id, actor, action, previous_status, new_status
                    ) VALUES (
                        'bad-rec', 'missing-publication', 'admin', 'CANCELLED_UNKNOWN',
                        'PUBLISH_UNKNOWN', 'CANCELLED'
                    )
                    """
                )
            )

