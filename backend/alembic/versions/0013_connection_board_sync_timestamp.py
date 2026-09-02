from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("pinterest_connections", sa.Column("boards_last_synced_at", sa.DateTime(timezone=True), nullable=True))

def downgrade():
    op.drop_column("pinterest_connections", "boards_last_synced_at")
