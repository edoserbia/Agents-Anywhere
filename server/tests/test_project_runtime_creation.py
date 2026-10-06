"""Project creation through a runtime that owns the workspace path.

A runtime advertising ``project.create`` generates the directory itself, so the
client names only the project and the server stores whatever the runtime
answered. These tests pin both halves of that: the path really comes from the
connector, and the ordinary client-supplied path is untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from agent_server.app import create_app
from agent_server.services.device_runtimes import DeviceRuntimeService
from conftest import ApiV2TestClient as TestClient
from runtime_fixtures import seed_runtime_inventory

ADMIN_USER = "user1"
ADMIN_PASSWORD = "secret"

def worktree_for(name: str) -> str:
    """The generated directory a real server would answer with, per project."""

    return f"/e/wzj/opensciense_projects/{name}-b472bf7e-cbd4-417d-8d6c-5a2f3042ab6d"


class FakeRpc:
    """A connector whose runtime creates project workspaces."""

    def __init__(self) -> None:
        self.online = True
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.project_result: dict[str, Any] | None = None

    async def set_runtime_ingress_enabled(
        self, connector_id: str, runtime_id: str, enabled: bool
    ) -> None:
        pass

    async def is_online(self, _connector_id: str) -> bool:
        return self.online

    async def request(
        self,
        connector_id: str,
        method: str,
        params: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.requests.append((connector_id, method, params))
        if method == "runtime.createProject":
            if self.project_result is not None:
                return self.project_result
            return {
                "project": {
                    "projectId": f"prj_{params['name']}",
                    "name": params["name"],
                    "worktree": worktree_for(params["name"]),
                    "metadata": {"origin": "openscience"},
                },
                "runtime": params["runtime"],
                "runtimeId": params["runtimeId"],
            }
        return {"ok": True}

    async def request_bound(
        self,
        connector_id: str,
        method: str,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> tuple[dict[str, Any], str]:
        return (
            await self.request(connector_id, method, params, **kwargs),
            "fake-connection",
        )

    async def is_connection_id_current(
        self,
        _connector_id: str,
        connection_id: str,
    ) -> bool:
        return connection_id == "fake-connection" and self.online

    def project_requests(self) -> list[dict[str, Any]]:
        return [
            params
            for _, method, params in self.requests
            if method == "runtime.createProject"
        ]


def _auth_headers(client: TestClient) -> dict[str, str]:
    config = client.get("/auth/config").json()
    payload: dict[str, Any] = {
        "email": f"{ADMIN_USER}@example.com",
        "displayName": ADMIN_USER,
        "password": ADMIN_PASSWORD,
    }
    if config["needsBootstrap"]:
        payload["setupToken"] = client.app.state.setup_token.peek()
    response = client.post("/auth/register", json=payload)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


def _inventory(*, create_project: bool) -> dict[str, Any]:
    return {
        "runtimes": [
            {
                "runtimeId": "openscience",
                "runtimeType": "openscience",
                "displayName": "OpenScience",
                "discovery": {"baseUrl": "http://10.168.1.104:4096"},
                "schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"baseUrl": {"type": "string"}},
                    "additionalProperties": False,
                },
                "uiSchema": {"baseUrl": {"component": "text"}},
                "defaults": {"baseUrl": "http://10.168.1.104:4096"},
                "status": "stopped",
                "configured": True,
                "capabilities": {
                    "modelCatalog": True,
                    "createProject": create_project,
                },
                "metadata": {},
            }
        ]
    }


def _make_client(
    tmp_path: Any, *, create_project: bool = True
) -> tuple[TestClient, FakeRpc, str, str, dict[str, str]]:
    """A signed-in client with one configured, running OpenScience runtime."""

    app = create_app(tmp_path / "test.sqlite3")
    client = TestClient(app)
    headers = _auth_headers(client)
    created = client.post("/connectors", headers=headers, json={"name": "dev"})
    assert created.status_code == 200, created.text
    connector_id = created.json()["connector"]["id"]
    rpc = FakeRpc()
    app.state.rpc = rpc
    app.state.device_runtime_service = DeviceRuntimeService(app.state.store, rpc)
    asyncio.run(
        seed_runtime_inventory(app.state.store, connector_id, _inventory(create_project=create_project))
    )
    configured = client.put(
        f"/connectors/{connector_id}/runtimes/openscience/config",
        headers=headers,
        json={"config": {"baseUrl": "http://10.168.1.104:4096"}},
    )
    assert configured.status_code == 200, configured.text
    activated = client.put(
        f"/connectors/{connector_id}/runtimes/openscience/active",
        headers=headers,
        json={"active": True},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "running"
    rpc.requests.clear()
    return client, rpc, connector_id, "openscience", headers


def test_runtime_creation_stores_the_generated_worktree(tmp_path: Any) -> None:
    client, rpc, connector_id, runtime_id, headers = _make_client(tmp_path)

    response = client.post(
        "/projects",
        headers=headers,
        json={"connectorId": connector_id, "name": "analysis", "runtimeId": runtime_id},
    )

    assert response.status_code == 200, response.text
    project = response.json()["project"]
    assert project["workspacePath"] == worktree_for("analysis")
    assert project["name"] == "analysis"
    requests = rpc.project_requests()
    assert len(requests) == 1
    assert requests[0]["runtime"] == "openscience"
    assert requests[0]["runtimeId"] == runtime_id
    assert requests[0]["name"] == "analysis"
    assert "limit" not in requests[0]
    # The idempotency key is a real UUID: OpenScience validates it as one.
    assert str(UUID(requests[0]["operationId"])) == requests[0]["operationId"]


def test_runtime_creation_replays_the_same_operation_for_a_retry(tmp_path: Any) -> None:
    """A retry must replay OpenScience's project, not create a second one."""

    client, rpc, connector_id, runtime_id, headers = _make_client(tmp_path)

    first = client.post(
        "/projects",
        headers=headers,
        json={"connectorId": connector_id, "name": "analysis", "runtimeId": runtime_id},
    )
    assert first.status_code == 200, first.text
    retried = client.post(
        "/projects",
        headers=headers,
        json={"connectorId": connector_id, "name": "analysis", "runtimeId": runtime_id},
    )
    # The connector was asked with the same key, so OpenScience replays its
    # project instead of creating a second one; the store then resolves the same
    # workspace to the same project rather than duplicating it.
    assert retried.status_code == 200, retried.text
    assert retried.json()["project"]["id"] == first.json()["project"]["id"]
    other = client.post(
        "/projects",
        headers=headers,
        json={"connectorId": connector_id, "name": "other", "runtimeId": runtime_id},
    )
    assert other.status_code == 200, other.text

    operation_ids = [params["operationId"] for params in rpc.project_requests()]
    assert operation_ids[0] == operation_ids[1]
    assert operation_ids[2] != operation_ids[0]


def test_client_supplied_path_is_unchanged_without_a_runtime(tmp_path: Any) -> None:
    client, rpc, connector_id, _, headers = _make_client(tmp_path)

    response = client.post(
        "/projects",
        headers=headers,
        json={
            "connectorId": connector_id,
            "name": "repo",
            "workspacePath": "/work/repo",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["project"]["workspacePath"] == "/work/repo"
    assert rpc.project_requests() == []


def test_a_runtime_without_the_capability_is_refused(tmp_path: Any) -> None:
    client, rpc, connector_id, runtime_id, headers = _make_client(
        tmp_path, create_project=False
    )

    response = client.post(
        "/projects",
        headers=headers,
        json={"connectorId": connector_id, "name": "analysis", "runtimeId": runtime_id},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "runtime_project_creation_unsupported"
    assert rpc.project_requests() == []


def test_a_runtime_answer_without_a_worktree_is_a_bad_gateway(tmp_path: Any) -> None:
    client, rpc, connector_id, runtime_id, headers = _make_client(tmp_path)
    rpc.project_result = {"project": {"projectId": "prj_1", "name": "analysis"}}

    response = client.post(
        "/projects",
        headers=headers,
        json={"connectorId": connector_id, "name": "analysis", "runtimeId": runtime_id},
    )

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "invalid_runtime_project"


def test_the_two_workspace_sources_are_mutually_exclusive(tmp_path: Any) -> None:
    client, _, connector_id, runtime_id, headers = _make_client(tmp_path)

    neither = client.post(
        "/projects",
        headers=headers,
        json={"connectorId": connector_id, "name": "analysis"},
    )
    both = client.post(
        "/projects",
        headers=headers,
        json={
            "connectorId": connector_id,
            "name": "analysis",
            "runtimeId": runtime_id,
            "workspacePath": "/work/repo",
        },
    )

    assert neither.status_code == 422, neither.text
    assert both.status_code == 422, both.text
