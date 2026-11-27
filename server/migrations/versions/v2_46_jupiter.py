"""Backfill thread-scoped marks for deletions recorded before v2_45.

Before v2_45 a deletion only marked the session it happened in. Every session
that already shows the same runtime thread keeps its own rows, so those old marks
have to be lifted to thread scope once; otherwise the message a reader deleted
before the fix still appears when the thread is opened through another session.

Revision ID: v2_46
Revises: v2_45
"""

import sqlalchemy as sa
from alembic import op

revision = "v2_46_jupiter"
down_revision = "v2_45_jupiter"
branch_labels = None
depends_on = None

TABLE = "timeline_thread_hides"


def _table_exists(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    if not _table_exists(TABLE):
        return
    bind = op.get_bind()
    ignore = "OR IGNORE" if bind.dialect.name == "sqlite" else ""
    on_conflict = "" if bind.dialect.name == "sqlite" else "ON CONFLICT DO NOTHING"
    for source in (
        # Deletions recorded while the item was not persisted yet.
        """
        SELECT s.connector_id, s.runtime, s.external_session_id, h.item_id, h.hidden_at
        FROM timeline_item_hides h
        JOIN sessions s ON s.id = h.session_id
        WHERE s.external_session_id IS NOT NULL
        """,
        # Deletions recorded on the stored row.
        """
        SELECT s.connector_id, s.runtime, s.external_session_id, i.id, i.hidden_at
        FROM timeline_items i
        JOIN sessions s ON s.id = i.session_id
        WHERE i.hidden_at IS NOT NULL AND s.external_session_id IS NOT NULL
        """,
    ):
        bind.execute(
            sa.text(
                f"INSERT {ignore} INTO {TABLE} "
                "(connector_id, runtime, external_session_id, item_id, hidden_at) "
                f"{source} {on_conflict}"
            )
        )


def downgrade() -> None:
    # The backfilled rows are indistinguishable from marks written by the write
    # path, and dropping them would resurrect messages a reader deleted on
    # purpose. Only the schema change is reverted, by dropping the table.
    pass
