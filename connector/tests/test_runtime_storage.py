import asyncio
from unittest.mock import AsyncMock

import pytest

from connector.core.json_kv import JsonKeyValueStore
from connector.runtime_protocol import RuntimeInstanceHost, RuntimeInstanceSpec, RuntimeSourceKey
from connector.server.runtime_host import ConnectorRuntimeHost
from connector.server.runtime_storage import RuntimeStorageManager
from connector.server.sync_state import JsonSyncStateStore


def test_full_copy_once_isolation_and_flush(tmp_path, monkeypatch):
    async def run():
        legacy = JsonSyncStateStore(tmp_path / "connector-state.json")
        old_kv = JsonKeyValueStore(tmp_path / "connector-kv.json")
        monkeypatch.setenv("AGENT_CONNECTOR_KV_FILE", str(old_kv.path))
        base = ConnectorRuntimeHost("conn", AsyncMock(), AsyncMock(), legacy)
        spec = RuntimeInstanceSpec(runtime_id="rti_a", runtime_type="dsh", name="A")
        old = RuntimeInstanceHost(base, spec)
        await old.sync_state_write("dsh/history/a", {"seq": 3})
        old_kv.set("legacy-value", {"a": 1})
        bound = await base.prepare_runtime_host("rti_a")
        scoped = RuntimeInstanceHost(bound, spec)
        assert await scoped.sync_state_read("dsh/history/a") == {"seq": 3}
        assert bound.runtime_kv.get("legacy-value") == {"a": 1}
        target = tmp_path / "conn" / "rti_a"
        assert (target / "sync-state.json").exists()
        assert (target / "kv.json").exists()
        await scoped.sync_state_delete("dsh/history/a")
        bound.runtime_kv.delete("legacy-value")
        await scoped.sync_state_write("dsh/history/new", {"seq": 9})
        assert base.flush_runtime_storage()
        # Neither a stopped runtime nor a fresh manager loses dirty state or
        # resurrects deletions from the retained legacy files.
        fresh = ConnectorRuntimeHost("conn", AsyncMock(), AsyncMock(), JsonSyncStateStore(legacy.path))
        restored = await fresh.prepare_runtime_host("rti_a")
        restored_scope = RuntimeInstanceHost(restored, spec)
        assert await restored_scope.sync_state_read("dsh/history/a") is None
        assert await restored_scope.sync_state_read("dsh/history/new") == {"seq": 9}
        assert restored.runtime_kv.get("legacy-value") is None
        assert old_kv.get("legacy-value") == {"a": 1}
        other = await fresh.prepare_runtime_host("rti_b")
        other_scope = RuntimeInstanceHost(other, RuntimeInstanceSpec(runtime_id="rti_b", runtime_type="dsh", name="B"))
        assert await other_scope.sync_state_read("dsh/history/new") is None
        other.runtime_kv.set("same-key", {"owner": "b"})
        assert restored.runtime_kv.get("same-key") is None
        another_connector = RuntimeStorageManager(legacy, old_kv.path)
        _, another_kv = another_connector.prepare("other-connector", "rti_a")
        assert another_kv.get("same-key") is None
    asyncio.run(run())


def test_copy_failure_can_retry_without_publishing_partial_directory(tmp_path, monkeypatch):
    legacy = JsonSyncStateStore(tmp_path / "connector-state.json")
    kv_path = tmp_path / "connector-kv.json"
    kv_path.write_text("broken")
    manager = RuntimeStorageManager(legacy, kv_path)
    with pytest.raises(RuntimeError):
        manager.prepare("conn", "rti_a")
    assert not (tmp_path / "conn" / "rti_a").exists()
    kv_path.write_text('{"version":1,"values":{}}')
    state, kv = manager.prepare("conn", "rti_a")
    assert state.path.exists() and kv.path.exists()
    assert manager.prepare("conn", "rti_a") == (state, kv)


@pytest.mark.parametrize("identity", ["../escape", "/absolute", "..", "a/b", "a\\b"])
def test_directory_identity_cannot_escape_root(tmp_path, identity):
    manager = RuntimeStorageManager(JsonSyncStateStore(tmp_path / "state.json"))
    with pytest.raises(ValueError):
        manager.prepare("conn", identity)


def test_source_isolation_preserves_existing_keys(tmp_path):
    async def run():
        base = ConnectorRuntimeHost("conn", AsyncMock(), AsyncMock(), JsonSyncStateStore(tmp_path / "state.json"))
        bound = await base.prepare_runtime_host("rti_a")
        spec = RuntimeInstanceSpec(runtime_id="rti_a", runtime_type="dsh", name="A")
        a = RuntimeInstanceHost(bound, spec, RuntimeSourceKey(kind="home", key="/a"))
        b = RuntimeInstanceHost(bound, spec, RuntimeSourceKey(kind="home", key="/b"))
        await a.sync_state_write("dsh/history/x", {"seq": 1})
        assert await b.sync_state_read("dsh/history/x") is None
    asyncio.run(run())


def test_runtimes_sync_concurrently_so_one_cannot_starve_another():
    """A slow runtime must not block the others' acknowledgement budgets.

    The scanner ran runtimes one after another. A single Codex session read was
    measured at 61s — longer than the DSH bridge's entire 60s batch-ACK budget —
    so while Codex held the loop DSH could not acknowledge its event stream, and
    the bridge restarted it from the first batch indefinitely. That is what made
    a task submitted in DSH Desktop never reach the platform.
    """
    import asyncio
    import inspect

    from connector.server import runtime_sync

    source = inspect.getsource(runtime_sync.RuntimeSyncRunner.sync_existing_once)
    assert "asyncio.gather(" in source, "runtimes sync concurrently"
    assert "_sync_one_runtime" in source, "each runtime has its own task"
    assert "for runtime_id in self.supervisor.runtimes:" not in source, (
        "the serial loop must be gone"
    )

    # Concurrent execution is the point: two runtimes must overlap.
    order: list[str] = []

    class _Service(runtime_sync.RuntimeSyncRunner):
        async def _sync_one_runtime(self, runtime_id):  # type: ignore[override]
            order.append(f"start:{runtime_id}")
            await asyncio.sleep(0.05)
            order.append(f"end:{runtime_id}")

    async def run() -> None:
        service = _Service.__new__(_Service)
        service.supervisor = type("S", (), {"runtimes": ("a", "b")})()
        service.flush_sync_state = None
        await _Service.sync_existing_once(service)
        assert order[:2] == ["start:a", "start:b"], "both start before either ends"

    asyncio.run(run())


def test_ingest_upload_allows_more_than_a_single_minute():
    """A large snapshot must be able to finish uploading.

    The upload budget used to be a fixed 60s, which assumes the whole snapshot
    fits in a minute. On an asymmetric link it does not: measured here, upload
    runs at ~19 KB/s against 216 KB/s down, so one megabyte already takes ~54s
    and every larger history failed on every attempt — the snapshot never
    reached the server, so the task never appeared in the platform.

    The budget is generous now, and overridable for a deployment that wants it
    tighter.
    """
    import os

    from connector.server.ingest import ConnectorIngestClient

    ingest = ConnectorIngestClient.__new__(ConnectorIngestClient)
    # Default: long enough for a multi-megabyte snapshot on a slow uplink.
    assert ingest._upload_timeout_seconds() >= 600

    # Overridable, and a malformed value falls back rather than crashing.
    os.environ["AA_CONNECTOR_INGEST_TIMEOUT_SECONDS"] = "120"
    try:
        assert ingest._upload_timeout_seconds() == 120
        os.environ["AA_CONNECTOR_INGEST_TIMEOUT_SECONDS"] = "not-a-number"
        assert ingest._upload_timeout_seconds() >= 600, "bad input falls back"
    finally:
        os.environ.pop("AA_CONNECTOR_INGEST_TIMEOUT_SECONDS", None)
