"""Keep local timeline deletions for items not yet persisted.

The runtime can report a message after the reader has already removed its
optimistic bubble.  A separate tombstone table lets that deletion remain
durable without inventing a timeline row or touching runtime history.

Revision ID: v2_38
Revises: v2_37
"""

import sqlalchemy as sa
from alembic import op

revision = "v2_38"
down_revision = "v2_37"
branch_labels = None
depends_on = None

TABLE = "timeline_item_hides"


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    if _table_exists(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("hidden_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "item_id"),
    )


def downgrade() -> None:
    if _table_exists(TABLE):
        op.drop_table(TABLE)
