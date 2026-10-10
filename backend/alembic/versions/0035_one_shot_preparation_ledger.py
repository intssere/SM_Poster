"""Durable one-shot preparation reservation ledger. Revision 0035, parent 0034.

Schema only. This migration never prepares a batch or admits publication.
"""
from alembic import op
import sqlalchemy as sa

from app.state_transfer.one_shot_operation_reservation import reservations

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0035 requires PostgreSQL")
    existing = connection.scalar(sa.text(
        "SELECT to_regclass('public.routine_one_shot_preparation_operations')"
    ))
    if existing is not None:
        raise RuntimeError("0035 refuses pre-existing one-shot ledger")
    # Keep the definition frozen to the pre-reviewed reservation contract.
    reservations.create(connection, checkfirst=False)


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0035 requires PostgreSQL")
    count = connection.scalar(sa.text(
        "SELECT count(*) FROM public.routine_one_shot_preparation_operations"
    ))
    if count:
        raise RuntimeError("0035 refuses downgrade with durable reservation evidence")
    op.drop_table("routine_one_shot_preparation_operations")
