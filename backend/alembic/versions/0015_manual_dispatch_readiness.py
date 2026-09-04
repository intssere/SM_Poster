"""manual dispatch readiness

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publication_dispatch_authorizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), nullable=False),
        sa.Column("authorized_by", sa.String(255), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publication_fingerprint", sa.String(64), nullable=False),
        sa.Column("quality_policy_version", sa.String(80), nullable=False),
        sa.Column("quality_snapshot", sa.JSON(), nullable=False),
        sa.Column("readiness_snapshot", sa.JSON(), nullable=False),
        sa.Column("duplicate_snapshot", sa.JSON(), nullable=False),
        sa.Column("confirmation_text_version", sa.String(80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.String(255)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["publication_id"], ["pin_publications.id"], name="fk_publication_dispatch_authorizations_publication", ondelete="RESTRICT"),
    )
    op.create_index("ix_publication_dispatch_authorizations_publication_id", "publication_dispatch_authorizations", ["publication_id"])
    op.create_index("ix_publication_dispatch_authorizations_expires_at", "publication_dispatch_authorizations", ["expires_at"])
    op.create_index("ix_publication_dispatch_authorizations_status", "publication_dispatch_authorizations", ["status"])
    op.create_index("ix_publication_dispatch_authorizations_authorized_at", "publication_dispatch_authorizations", ["authorized_at"])
    op.create_index(
        "uq_publication_dispatch_authorizations_active",
        "publication_dispatch_authorizations",
        ["publication_id"],
        unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"),
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "publication_reconciliation_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), nullable=False),
        sa.Column("attempt_id", sa.String(36)),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("previous_status", sa.String(30), nullable=False),
        sa.Column("new_status", sa.String(30), nullable=False),
        sa.Column("provider_pin_id", sa.String(255)),
        sa.Column("reason", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["publication_id"], ["pin_publications.id"], name="fk_publication_reconciliation_events_publication", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["publication_attempts.id"], name="fk_publication_reconciliation_events_attempt", ondelete="RESTRICT"),
    )
    op.create_index("ix_publication_reconciliation_events_publication_id", "publication_reconciliation_events", ["publication_id"])
    op.create_index("ix_publication_reconciliation_events_attempt_id", "publication_reconciliation_events", ["attempt_id"])
    op.create_index("ix_publication_reconciliation_events_created_at", "publication_reconciliation_events", ["created_at"])


def downgrade():
    op.drop_index("ix_publication_reconciliation_events_created_at", table_name="publication_reconciliation_events")
    op.drop_index("ix_publication_reconciliation_events_attempt_id", table_name="publication_reconciliation_events")
    op.drop_index("ix_publication_reconciliation_events_publication_id", table_name="publication_reconciliation_events")
    op.drop_table("publication_reconciliation_events")
    op.drop_index("uq_publication_dispatch_authorizations_active", table_name="publication_dispatch_authorizations")
    op.drop_index("ix_publication_dispatch_authorizations_authorized_at", table_name="publication_dispatch_authorizations")
    op.drop_index("ix_publication_dispatch_authorizations_status", table_name="publication_dispatch_authorizations")
    op.drop_index("ix_publication_dispatch_authorizations_expires_at", table_name="publication_dispatch_authorizations")
    op.drop_index("ix_publication_dispatch_authorizations_publication_id", table_name="publication_dispatch_authorizations")
    op.drop_table("publication_dispatch_authorizations")
