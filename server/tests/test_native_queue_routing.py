from __future__ import annotations

import asyncio
from typing import Any

from agent_server.core.models import (
    MessageCreateRequest,
    QueueItemUpdateRequest,
    SessionView,
)
from agent_server.services.session_run import SessionRunService


class _Store:
    async def get_session(self, session_id: str, *, user_id: str) -> SessionView:
        return SessionView(
            id=session_id,
            userId=user_id,
            connectorId="conn_1",
            projectId="proj_1",
            runtime="dsh",
            runtimeId="rti_1",
            status="running",
            connectorStatus="online",
            takeover=True,
            updatedSeq=1,
        )


class _DeviceRuntimes:
    async def ensure_session_routable(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Queue:
    async def list(self, session_id: str) -> list[Any]:
        return []


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def is_online(self, connector_id: str) -> bool:
        return True

    async def request(self, connector_id: str, method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((method, params))
        if method == "session.state":
            return {
                "state": {
                    "sessionId": params["sessionId"],
                    "runtime": "dsh",
                    "runtimeId": "rti_1",
                    "status": "running",
                },
            }
        if method == "session.capabilities":
            return {
                "capabilitySet": {
                    "revision": 1,
                    "capabilities": [
                        {
                            "capabilityId": "session.send_message",
                            "scope": "session",
                            "runtime": "dsh",
                            "runtimeId": "rti_1",
                            "sessionId": params["sessionId"],
                            "supported": True,
                            "available": True,
                            "allowed": True,
                        },
                        {
                            "capabilityId": "session.queue",
                            "scope": "session",
                            "runtime": "dsh",
                            "runtimeId": "rti_1",
                            "sessionId": params["sessionId"],
                            "supported": True,
                            "available": True,
                            "allowed": True,
                        },
                    ],
                },
            }
        if method == "session.queue":
            return {
                "runtime": "dsh",
                "runtimeId": "rti_1",
                "accepted": True,
            }
        raise AssertionError(f"unexpected connector call: {method}")


def test_busy_send_enters_the_runtime_native_queue() -> None:
    async def run() -> None:
        manager = _Manager()
        service = SessionRunService(
            _Store(), manager, _DeviceRuntimes(), message_queue=_Queue()
        )
        result = await service.send_message(
            "sess_1",
            MessageCreateRequest(
                content="follow up",
                clientMessageId="cm_1",
                queueWhenBusy=True,
            ),
            user_id="usr_1",
        )
        assert result.result["queued"] is True
        assert [method for method, _ in manager.calls] == [
            "session.state",
            "session.capabilities",
            "session.capabilities",
            "session.queue",
        ]
        queue_call = manager.calls[-1]
        assert queue_call[1]["content"] == "follow up"
        assert queue_call[1]["clientMessageId"] == "cm_1"

    asyncio.run(run())

class _NativeCrudManager(_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.native_items: list[dict[str, Any]] = [
            {
                "id": "native-1",
                "content": "first",
                "position": 0,
                "status": "queued",
                "clientMessageId": "cm-native-1",
            },
            {
                "id": "native-2",
                "content": "second",
                "position": 1,
                "status": "queued",
                "clientMessageId": "cm-native-2",
            },
        ]

    async def request(self, connector_id: str, method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((method, params))
        if method == "session.queue.list":
            return {"runtime": "dsh", "items": list(self.native_items)}
        if method == "session.queue.update":
            item = next(item for item in self.native_items if item["id"] == params["itemId"])
            action = params.get("action", {})
            if action.get("kind") == "steer":
                self.native_items.remove(item)
            else:
                item["content"] = params["content"]
            return {"accepted": True, "itemId": params["itemId"]}
        if method == "session.queue.delete":
            self.native_items = [item for item in self.native_items if item["id"] != params["itemId"]]
            return {"accepted": True, "itemId": params["itemId"]}
        return await super().request(connector_id, method, params, **kwargs)


def test_native_queue_list_edit_delete_and_insert_are_routed_to_runtime() -> None:
    async def run() -> None:
        manager = _NativeCrudManager()
        service = SessionRunService(
            _Store(), manager, _DeviceRuntimes(), message_queue=_Queue()
        )

        listed = await service.list_queue("sess_1", user_id="usr_1")
        assert [item["id"] for item in listed] == ["native-1", "native-2"]

        updated = await service.update_queue_item(
            "sess_1",
            "native-1",
            QueueItemUpdateRequest(content="edited"),
            user_id="usr_1",
        )
        assert updated["content"] == "edited"
        assert any(
            method == "session.queue.update" and params.get("content") == "edited"
            for method, params in manager.calls
        )

        removed = await service.remove_queue_item(
            "sess_1", "native-2", user_id="usr_1"
        )
        assert removed["id"] == "native-2"
        assert manager.calls[-1][0] == "session.queue.delete"

        remaining = await service.insert_queue_item(
            "sess_1", "native-1", user_id="usr_1"
        )
        assert remaining == []
        assert any(
            method == "session.queue.update" and params.get("action") == {"kind": "steer"}
            for method, params in manager.calls
        )

    asyncio.run(run())
