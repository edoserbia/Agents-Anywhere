"""Keep a deleted timeline item deleted in every session of the same thread.

One runtime thread can be reachable through more than one AA session: the
scanner derives an id for the thread it discovers, while a session created from
a client keeps the id that client chose. Both project the same runtime items, so
a mark stored only against ``session_id`` let the same message come back as soon
as the reader reopened the thread through another session. The mark is therefore
also stored against the runtime thread.

Revision ID: v2_45
Revises: v2_44
"""

import sqlalchemy as sa
from alembic import op

revision = "v2_45_jupiter"
down_revision = "v2_44_jupiter"
branch_labels = None
depends_on = None

TABLE = "timeline_thread_hides"


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
            "connector_id",
            sa.Text(),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("external_session_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("hidden_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "connector_id", "runtime", "external_session_id", "item_id"
        ),
    )
    op.create_index(
        "ix_timeline_thread_hides_thread",
        TABLE,
        ["connector_id", "runtime", "external_session_id"],
    )


def downgrade() -> None:
    if _table_exists(TABLE):
        op.drop_table(TABLE)
