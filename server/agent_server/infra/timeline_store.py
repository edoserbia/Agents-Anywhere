from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_server.core.models import TimelineItem
from agent_server.core.utc import utc_now
from agent_server.infra.db import timeline_item_hides, timeline_items
from agent_server.infra.db.engine import SQLITE_BACKEND


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SqlTimelineStore:
    def __init__(self, engine: AsyncEngine, *, backend: str = SQLITE_BACKEND) -> None:
        self._engine = engine
        self._backend = backend

    async def read(self, session_id: str) -> list[TimelineItem]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    timeline_items.select()
                    .where(timeline_items.c.session_id == session_id)
                    .order_by(
                        timeline_items.c.order_seq,
                        timeline_items.c.updated_seq,
                        timeline_items.c.id,
                    )
                )
            ).mappings().all()
        return [TimelineItem.model_validate_json(row["payload_json"]) for row in rows]

    async def recovery_items(
        self, session_id: str, *, limit: int = 100,
    ) -> tuple[list[TimelineItem], bool]:
        """Probe IDs first; caller holds the session revision fence."""
        async with self._engine.connect() as conn:
            ids = (await conn.execute(
                select(timeline_items.c.id)
                .where(timeline_items.c.session_id == session_id)
                .limit(limit + 1)
            )).scalars().all()
        if len(ids) > limit:
            return [], True
        items = await self.read_many(session_id, set(ids))
        hidden_ids = await self.hidden_item_ids(session_id)
        items = [item for item in items if item.id not in hidden_ids]
        items.sort(key=lambda item: (item.orderSeq, item.updatedSeq, item.id))
        return items, False

    async def replace(self, session_id: str, items: list[TimelineItem]) -> None:
        async with self._engine.begin() as conn:
            await self.replace_all(conn, session_id, items)

    async def delete_items(
        self,
        conn: AsyncConnection,
        session_id: str,
        item_ids: set[str],
    ) -> None:
        """Delete the given stable IDs inside the caller's transaction."""

        ids = list(item_ids)
        for offset in range(0, len(ids), 500):
            await conn.execute(
                delete(timeline_items).where(
                    timeline_items.c.session_id == session_id,
                    timeline_items.c.id.in_(ids[offset : offset + 500]),
                )
            )

    async def replace_all(
        self,
        conn: AsyncConnection,
        session_id: str,
        items: list[TimelineItem],
    ) -> None:
        """Replace one complete timeline inside the caller's transaction."""

        await conn.execute(
            delete(timeline_items).where(timeline_items.c.session_id == session_id)
        )
        if not items:
            return
        sorted_items = sorted(
            items,
            key=lambda value: (value.orderSeq, value.updatedSeq, value.id),
        )
        await conn.execute(
            insert(timeline_items),
            [self._row_values(item) for item in sorted_items],
        )

    async def latest_item(self, session_id: str) -> TimelineItem | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    timeline_items.select()
                    .where(timeline_items.c.session_id == session_id)
                    .order_by(
                        timeline_items.c.item_time.desc(),
                        timeline_items.c.order_seq.desc(),
                        timeline_items.c.updated_seq.desc(),
                    )
                    .limit(1)
                )
            ).mappings().first()
        return TimelineItem.model_validate_json(row["payload_json"]) if row is not None else None

    async def upsert_one(self, conn: AsyncConnection, item: TimelineItem) -> None:
        """Insert-or-update a single row by composite PK (session_id, id).

        Hot path: called per streaming Codex delta. Avoids the O(N) DELETE-all
        + INSERT-all pattern that `replace()` does. The dialect-specific
        upsert keeps it to one row mutation.
        """
        await self.upsert_many(conn, [item])

    async def upsert_many(
        self,
        conn: AsyncConnection,
        items: list[TimelineItem],
    ) -> None:
        """Insert or update one Runtime timeline batch by stable IDs."""

        if not items:
            return
        values = [self._row_values(item) for item in items]
        hidden_ids = set(
            (
                await conn.execute(
                    select(timeline_item_hides.c.item_id).where(
                        timeline_item_hides.c.session_id == items[0].sessionId,
                        timeline_item_hides.c.item_id.in_({item.id for item in items}),
                    )
                )
            ).scalars()
        )
        # A local tombstone must never be cleared by a Runtime upsert. The
        # row itself remains useful for reconciliation, but stays hidden.
        if hidden_ids:
            values = [
                value
                for value in values
                if value["id"] not in hidden_ids
            ]
        if not values:
            return
        if self._backend == SQLITE_BACKEND:
            stmt = sqlite_insert(timeline_items)
            update_cols = {
                key: stmt.excluded[key]
                for key in values[0]
                if key not in ("session_id", "id")
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["session_id", "id"],
                set_=update_cols,
                where=stmt.excluded.updated_seq >= timeline_items.c.updated_seq,
            )
        else:
            stmt = pg_insert(timeline_items)
            update_cols = {
                key: stmt.excluded[key]
                for key in values[0]
                if key not in ("session_id", "id")
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["session_id", "id"],
                set_=update_cols,
                where=stmt.excluded.updated_seq >= timeline_items.c.updated_seq,
            )
        await conn.execute(stmt, values)

    async def read_one(
        self, session_id: str, item_id: str
    ) -> TimelineItem | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    timeline_items.select().where(
                        timeline_items.c.session_id == session_id,
                        timeline_items.c.id == item_id,
                    )
                )
            ).mappings().first()
        return TimelineItem.model_validate_json(row["payload_json"]) if row is not None else None

    async def read_many(
        self,
        session_id: str,
        item_ids: set[str],
    ) -> list[TimelineItem]:
        """Read only the rows touched by one incremental Runtime sync."""

        if not item_ids:
            return []
        rows = []
        item_id_list = list(item_ids)
        async with self._engine.connect() as conn:
            for offset in range(0, len(item_id_list), 500):
                chunk = item_id_list[offset : offset + 500]
                rows.extend(
                    (
                        await conn.execute(
                            timeline_items.select().where(
                                timeline_items.c.session_id == session_id,
                                timeline_items.c.id.in_(chunk),
                            )
                        )
                    ).mappings().all()
                )
        return [TimelineItem.model_validate_json(row["payload_json"]) for row in rows]

    async def list_since(
        self, session_id: str, *, after_seq: int, limit: int
    ) -> tuple[list[TimelineItem], bool]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    timeline_items.select()
                    .where(
                        timeline_items.c.hidden_at.is_(None),
                        timeline_items.c.session_id == session_id,
                        timeline_items.c.updated_seq > after_seq,
                    )
                    .order_by(timeline_items.c.updated_seq)
                    .limit(limit + 1)
                )
            ).mappings().all()
        has_more = len(rows) > limit
        items = [TimelineItem.model_validate_json(row["payload_json"]) for row in rows[:limit]]
        return items, has_more

    async def list_latest(self, session_id: str, *, limit: int) -> tuple[list[TimelineItem], bool]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    timeline_items.select()
                    .where(timeline_items.c.hidden_at.is_(None))
                    .where(timeline_items.c.session_id == session_id)
                    .order_by(
                        timeline_items.c.order_seq.desc(),
                        timeline_items.c.updated_seq.desc(),
                        timeline_items.c.id.desc(),
                    )
                    .limit(limit + 1)
                )
            ).mappings().all()
        has_more = len(rows) > limit
        items = [TimelineItem.model_validate_json(row["payload_json"]) for row in rows[:limit]]
        items.reverse()
        return items, has_more

    async def list_before_order_seq(
        self, session_id: str, *, before_order_seq: int, limit: int
    ) -> tuple[list[TimelineItem], bool]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    timeline_items.select()
                    .where(timeline_items.c.hidden_at.is_(None))
                    .where(
                        timeline_items.c.session_id == session_id,
                        timeline_items.c.order_seq < before_order_seq,
                    )
                    .order_by(
                        timeline_items.c.order_seq.desc(),
                        timeline_items.c.updated_seq.desc(),
                        timeline_items.c.id.desc(),
                    )
                    .limit(limit + 1)
                )
            ).mappings().all()
        has_more = len(rows) > limit
        items = [TimelineItem.model_validate_json(row["payload_json"]) for row in rows[:limit]]
        items.reverse()
        return items, has_more

    async def set_hidden(
        self, session_id: str, item_id: str, *, hidden: bool
    ) -> TimelineItem | None:
        """Mark an item hidden, or visible again.

        Deletion is a local mark rather than a row removal: the runtime keeps
        reporting the item, so a removed row would simply reappear on the next
        sync. The mark is therefore stored in its own column, outside the set the
        runtime write path sets, and survives later updates to the item.

        Returns the item as the reader will now see it, or None when the item
        does not exist in this session.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(timeline_items)
                .where(
                    timeline_items.c.session_id == session_id,
                    timeline_items.c.id == item_id,
                )
                .values(hidden_at=utc_now() if hidden else None)
            )
            if hidden:
                if self._backend == SQLITE_BACKEND:
                    statement = sqlite_insert(timeline_item_hides).values(
                        session_id=session_id,
                        item_id=item_id,
                        hidden_at=utc_now(),
                    ).prefix_with("OR IGNORE")
                else:
                    statement = pg_insert(timeline_item_hides).values(
                        session_id=session_id,
                        item_id=item_id,
                        hidden_at=utc_now(),
                    ).on_conflict_do_nothing(
                        index_elements=["session_id", "item_id"]
                    )
                await conn.execute(statement)
            elif result.rowcount != 1:
                await conn.execute(
                    delete(timeline_item_hides).where(
                        timeline_item_hides.c.session_id == session_id,
                        timeline_item_hides.c.item_id == item_id,
                    )
                )
                return None
            if not hidden:
                await conn.execute(
                    delete(timeline_item_hides).where(
                        timeline_item_hides.c.session_id == session_id,
                        timeline_item_hides.c.item_id == item_id,
                    )
                )
        return await self.read_one(session_id, item_id)

    async def hidden_item_ids(self, session_id: str) -> set[str]:
        """Ids the reader has deleted, so a re-sync can avoid resurrecting them."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(timeline_items.c.id).where(
                        timeline_items.c.session_id == session_id,
                        timeline_items.c.hidden_at.is_not(None),
                    )
                )
            ).all()
            tombstones = (
                await conn.execute(
                    select(timeline_item_hides.c.item_id).where(
                        timeline_item_hides.c.session_id == session_id,
                    )
                )
            ).all()
        return {row[0] for row in rows} | {row[0] for row in tombstones}

    def _row_values(self, item: TimelineItem) -> dict[str, Any]:
        return {
            "session_id": item.sessionId,
            "id": item.id,
            "type": item.type,
            "status": item.status,
            "role": item.role,
            "order_seq": item.orderSeq,
            "updated_seq": item.updatedSeq,
            "item_time": _item_time(item),
            "payload_json": _json_dumps(item.model_dump(exclude_none=True)),
        }


def _item_time(item: TimelineItem) -> str | None:
    values = [value for value in (item.createdAt, item.completedAt, item.updatedAt) if value]
    return max(values) if values else None
