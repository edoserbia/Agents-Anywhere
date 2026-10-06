from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from connector.runtimes.openscience import discovery, provider_config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep the developer's real ~/.openscience out of every resolution test."""

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return home


def values(**overrides):
    return provider_config.normalized_config_values(overrides)


def write_record(root: Path, **payload):
    record = {"schema": 1, "port": 41999, "pid": 4242, "version": "2.0.147",
              "run_id": "run-1", "started_at": "2026-10-06T00:00:00Z", **payload}
    root.mkdir(parents=True, exist_ok=True)
    (root / "desktop-server.json").write_text(json.dumps(record), encoding="utf-8")


# --- data root -------------------------------------------------------------


def test_data_root_follows_the_config_link(tmp_path, monkeypatch):
    """The app relocates its data root behind a link, so the link wins."""

    config = tmp_path / "config" / "openscience"
    target = tmp_path / "relocated"
    config.mkdir(parents=True)
    target.mkdir()
    (config / "data-root").symlink_to(target)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert provider_config.data_root(values()) == Path(target).resolve()


def test_data_root_falls_back_to_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert provider_config.data_root(values()) == (tmp_path / "data" / "openscience").resolve()


def test_data_root_falls_back_to_the_default_location(tmp_path, isolated_home):
    """A server that never created the link still keeps its data here."""

    (isolated_home / ".openscience").mkdir()

    assert provider_config.data_root(values()) == (isolated_home / ".openscience").resolve()


def test_explicit_data_root_overrides_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored"))

    assert provider_config.data_root(values(dataRoot=str(tmp_path / "mine"))) == Path(
        tmp_path / "mine"
    ).resolve()


# --- capability probe ------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, content_type, body):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    """Answers each URL from a table and records what was asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.requested: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url, headers=None):
        self.requested.append(url)
        answer = self.routes.get(url)
        if answer is None:
            raise discovery.httpx.ConnectError(f"connection refused: {url}")
        if isinstance(answer, Exception):
            raise answer
        return answer


def patch_http(monkeypatch, routes):
    client = FakeClient(routes)
    monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **_kwargs: client)
    return client


def test_probe_capabilities_accepts_the_runtime_protocol(monkeypatch):
    patch_http(monkeypatch, {
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(
            200, "application/json", {"protocolVersion": "1.0", "serverVersion": "local",
                                      "eventRetention": 2048}
        ),
    })

    capabilities = asyncio.run(discovery.probe_capabilities("http://127.0.0.1:41999", values()))

    assert capabilities is not None
    assert capabilities.protocol_version == "1.0"
    assert capabilities.event_retention == 2048


def test_a_web_ui_answering_the_path_is_not_an_openscience_server(monkeypatch):
    """OpenCode defaults to the same port and answers every path with HTML.

    A status-code check alone would attach the Connector to the wrong product,
    so the probe requires a JSON body carrying a protocol version.
    """

    patch_http(monkeypatch, {
        "http://127.0.0.1:4096/runtime/capabilities": FakeResponse(
            200, "text/html", "<!doctype html><title>OpenCode</title>"
        ),
    })

    assert asyncio.run(discovery.probe_capabilities("http://127.0.0.1:4096", values())) is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"protocolVersion": 1},
        {"protocolVersion": "2.0"},
        {"protocolVersion": "1.0.0"},
    ],
)
def test_only_a_supported_protocol_version_is_accepted(monkeypatch, body):
    patch_http(monkeypatch, {
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(200, "application/json", body),
    })

    assert discovery.parse_capabilities(body) is None


def test_json_that_is_not_an_object_is_refused(monkeypatch):
    patch_http(monkeypatch, {
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(200, "application/json", ["1.0"]),
    })

    assert asyncio.run(discovery.probe_capabilities("http://127.0.0.1:41999", values())) is None


# --- discovery -------------------------------------------------------------


def test_discovery_does_not_touch_the_network(monkeypatch):
    """The device list asks which types are supported, nothing more."""

    def explode(**_kwargs):
        raise AssertionError("discovery must not perform I/O")

    monkeypatch.setattr(discovery.httpx, "AsyncClient", explode)

    result = asyncio.run(discovery.discover(values()))

    assert result.available is True
    assert result.endpoint is None


def test_probe_reports_a_reason_when_nothing_is_running(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    patch_http(monkeypatch, {})

    result = asyncio.run(discovery.probe(values()))

    assert result.available is False
    assert result.reason == discovery.UNAVAILABLE_REASON


# --- desktop record --------------------------------------------------------


def test_desktop_record_is_used_when_the_run_id_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    write_record(tmp_path / "data" / "openscience")
    patch_http(monkeypatch, {
        "http://127.0.0.1:41999/global/health": FakeResponse(
            200, "application/json", {"healthy": True, "version": "2.0.147", "runId": "run-1"}
        ),
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(
            200, "application/json", {"protocolVersion": "1.0"}
        ),
    })

    result = asyncio.run(discovery.probe(values()))

    assert result.available is True
    assert result.endpoint is not None
    assert result.endpoint.source == "desktop-record"
    assert result.endpoint.run_id == "run-1"


def test_a_stale_record_cannot_claim_a_port_it_no_longer_owns(tmp_path, monkeypatch):
    """The record names its own run id, so a dead sidecar's record is inert."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    write_record(tmp_path / "data" / "openscience", run_id="run-gone")
    patch_http(monkeypatch, {
        "http://127.0.0.1:41999/global/health": FakeResponse(
            200, "application/json", {"healthy": True, "version": "other", "runId": "run-other"}
        ),
    })

    assert asyncio.run(discovery._endpoint_from_record(values())) is None


def test_a_record_without_a_run_id_is_still_verified_by_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    write_record(tmp_path / "data" / "openscience", run_id=None)
    patch_http(monkeypatch, {
        "http://127.0.0.1:41999/global/health": FakeResponse(
            200, "application/json", {"healthy": True, "version": "2.0.147"}
        ),
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(
            200, "application/json", {"protocolVersion": "1.0"}
        ),
    })

    found = asyncio.run(discovery._endpoint_from_record(values()))

    assert found is not None
    assert found[0].run_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 2, "port": 4096},
        {"schema": 1},
        {"schema": 1, "port": "4096"},
        {"schema": 1, "port": 0},
        {"schema": 1, "port": 70_000},
    ],
)
def test_malformed_records_are_ignored(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "data" / "openscience"
    root.mkdir(parents=True)
    (root / "desktop-server.json").write_text(json.dumps(payload), encoding="utf-8")

    assert discovery.read_desktop_record(values()) is None


# --- configured URL --------------------------------------------------------


def test_configured_url_is_the_only_candidate(monkeypatch):
    client = patch_http(monkeypatch, {
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(
            200, "application/json", {"protocolVersion": "1.0"}
        ),
        "http://127.0.0.1:41999/global/health": FakeResponse(
            200, "application/json", {"healthy": True, "version": "2.0.147", "runId": "run-1"}
        ),
    })

    result = asyncio.run(discovery.probe(values(baseUrl="http://127.0.0.1:41999")))

    assert result.available is True
    assert result.endpoint.source == "configured"
    assert result.endpoint.port == 41999
    assert all("4096" not in url for url in client.requested)


def test_a_configured_url_pointing_at_another_product_is_refused(monkeypatch):
    patch_http(monkeypatch, {
        "http://127.0.0.1:4096/runtime/capabilities": FakeResponse(
            200, "text/html", "<!doctype html>"
        ),
    })

    result = asyncio.run(discovery.probe(values(baseUrl="http://127.0.0.1:4096")))

    assert result.available is False
    assert "4096" in (result.reason or "")


def test_a_configured_url_with_a_trailing_slash_is_normalized(monkeypatch):
    patch_http(monkeypatch, {
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(
            200, "application/json", {"protocolVersion": "1.0"}
        ),
        "http://127.0.0.1:41999/global/health": FakeResponse(
            200, "application/json", {"healthy": True}
        ),
    })

    result = asyncio.run(discovery.probe(values(baseUrl="http://127.0.0.1:41999/")))

    assert result.endpoint.base_url == "http://127.0.0.1:41999"


# --- request headers -------------------------------------------------------


def test_configured_credentials_and_directory_are_sent(monkeypatch):
    seen: dict[str, str] = {}

    class RecordingClient(FakeClient):
        async def get(self, url, headers=None):
            seen.update(headers or {})
            return await super().get(url, headers)

    client = RecordingClient({
        "http://127.0.0.1:41999/runtime/capabilities": FakeResponse(
            200, "application/json", {"protocolVersion": "1.0"}
        ),
    })
    monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **_kwargs: client)

    asyncio.run(
        discovery.probe_capabilities(
            "http://127.0.0.1:41999",
            values(authToken="secret-token", directory="/tmp/project"),
        )
    )

    assert seen["authorization"] == "Bearer secret-token"
    assert seen["x-openscience-directory"] == str(Path("/tmp/project").resolve())


def test_no_credentials_means_no_authorization_header():
    headers = discovery._headers(values())

    assert "authorization" not in headers
    assert "x-openscience-directory" not in headers


# --- config validation -----------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"baseUrl": "127.0.0.1:41999"},
        {"directory": "relative/path"},
        {"dataRoot": "relative"},
        {"discoveryTimeoutMs": 10},
        {"requestTimeoutMs": True},
    ],
)
def test_invalid_config_is_rejected(overrides):
    from connector.runtime_protocol import RuntimeInvalidRequestError

    with pytest.raises(RuntimeInvalidRequestError):
        provider_config.normalized_config_values(overrides)


def test_blank_strings_are_dropped_rather_than_stored():
    result = provider_config.normalized_config_values({"baseUrl": "   ", "authToken": ""})

    assert "baseUrl" not in result
    assert "authToken" not in result
