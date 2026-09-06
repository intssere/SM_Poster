"""Durable intermediary operation identity; destination Pin IDs retain their meaning."""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _constraints(batch, failure):
    batch.drop_constraint("ck_publication_reconciliation_action", type_="check")
    batch.drop_constraint("ck_publication_reconciliation_transition", type_="check")
    actions = "'PROVIDER_PIN_CONFIRMED', 'CANCELLED_UNKNOWN'"
    transition = "(action = 'PROVIDER_PIN_CONFIRMED' AND new_status = 'PUBLISHED') OR (action = 'CANCELLED_UNKNOWN' AND new_status = 'CANCELLED')"
    if failure:
        actions += ", 'PROVIDER_FAILURE_CONFIRMED'"
        transition += " OR (action = 'PROVIDER_FAILURE_CONFIRMED' AND new_status = 'PUBLISH_FAILED')"
    batch.create_check_constraint("ck_publication_reconciliation_action", f"action IN ({actions})")
    batch.create_check_constraint("ck_publication_reconciliation_transition", f"({transition})")


def upgrade():
    op.add_column("publication_attempts", sa.Column("dispatch_provider", sa.String(40), nullable=False, server_default="pinterest_direct"))
    for column in (
        sa.Column("provider_operation_id", sa.String(255)),
        sa.Column("provider_operation_status", sa.String(30)),
        sa.Column("provider_external_link", sa.Text()),
        sa.Column("provider_submitted_at", sa.DateTime(timezone=True)),
        sa.Column("provider_last_observed_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("publication_attempts", column)
    op.create_index("uq_publication_attempt_provider_operation", "publication_attempts",
                    ["dispatch_provider", "provider_operation_id"], unique=True,
                    sqlite_where=sa.text("provider_operation_id IS NOT NULL"),
                    postgresql_where=sa.text("provider_operation_id IS NOT NULL"))
    with op.batch_alter_table("publication_reconciliation_events") as batch:
        batch.add_column(sa.Column("provider", sa.String(40), nullable=False, server_default="pinterest_direct"))
        batch.add_column(sa.Column("provider_operation_id", sa.String(255)))
        batch.add_column(sa.Column("provider_operation_status", sa.String(30)))
        _constraints(batch, True)


def downgrade():
    # Never discard intermediary identities/audit on a database that used Phase 2.
    connection = op.get_bind()
    if (connection.scalar(sa.text("SELECT COUNT(*) FROM publication_attempts WHERE dispatch_provider <> 'pinterest_direct' OR provider_operation_id IS NOT NULL"))
            or connection.scalar(sa.text("SELECT COUNT(*) FROM publication_reconciliation_events WHERE provider <> 'pinterest_direct' OR action = 'PROVIDER_FAILURE_CONFIRMED' OR provider_operation_id IS NOT NULL"))):
        raise RuntimeError("BUFFER_HISTORY_PREVENTS_DOWNGRADE")
    with op.batch_alter_table("publication_reconciliation_events") as batch:
        _constraints(batch, False)
        for name in ("provider_operation_status", "provider_operation_id", "provider"):
            batch.drop_column(name)
    op.drop_index("uq_publication_attempt_provider_operation", table_name="publication_attempts")
    with op.batch_alter_table("publication_attempts") as batch:
        for name in ("provider_last_observed_at", "provider_submitted_at", "provider_external_link", "provider_operation_status", "provider_operation_id", "dispatch_provider"):
            batch.drop_column(name)
