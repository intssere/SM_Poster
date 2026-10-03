"""Five-pin bounded autonomous batch. Revision 0034, parent 0033."""
from alembic import op
import sqlalchemy as sa

from app.db import bounded_batch_schema_0034 as frozen

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0034 requires PostgreSQL")
    if any(connection.scalar(sa.text("SELECT to_regclass(:name)"), {"name": f"public.{t}"})
           for t in frozen.TABLES):
        raise RuntimeError("0034 refuses pre-existing controller tables")
    frozen.metadata.create_all(connection, tables=[frozen.batches, frozen.entries])
    frozen.install_guards(connection)


def downgrade():
    connection = op.get_bind()
    for name in frozen.TABLES:
        if connection.scalar(sa.text(f"SELECT count(*) FROM public.{name}")):
            raise RuntimeError("0034 refuses downgrade with batch evidence")
    op.drop_table(frozen.entries.name)
    op.drop_table(frozen.batches.name)
    op.execute("DROP FUNCTION public.bounded_batch_guard()")