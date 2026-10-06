from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from connector.runtimes.openscience import provider_config

# The runtime protocol this connector speaks. The server reports its own in
# `/runtime/capabilities`; an unlisted version is refused rather than guessed at.
SUPPORTED_PROTOCOL_VERSIONS = ("1.0",)

CAPABILITIES_PATH = "/runtime/capabilities"
HEALTH_PATH = "/global/health"

UNAVAILABLE_REASON = "请先打开 OpenScience，再在 Agents Anywhere 中连接。"


@dataclass(frozen=True, slots=True)
class OpenScienceEndpoint:
    base_url: str
    port: int
    source: str
    run_id: str | None = None
    pid: int | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class OpenScienceCapabilities:
    protocol_version: str
    server_version: str | None = None
    idempotent_prompts: bool = False
    rich_inputs: bool = False
    run_snapshots: bool = False
    event_retention: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OpenScienceDiscovery:
    available: bool
    configured: bool
    endpoint: OpenScienceEndpoint | None
    capabilities: OpenScienceCapabilities | None = None
    reason: str | None = None
    metadata: dict[str, Any] | None = None


def _static_metadata(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataRoot": str(provider_config.data_root(values)),
        "desktopServerRecord": str(provider_config.desktop_server_path(values)),
        "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
        "implementationType": "local-service",
    }


def _headers(values: Mapping[str, Any]) -> dict[str, str]:
    headers = {"accept": "application/json"}
    token = values.get("authToken")
    if isinstance(token, str) and token:
        headers["authorization"] = f"Bearer {token}"
    directory = values.get("directory")
    if isinstance(directory, str) and directory:
        headers["x-openscience-directory"] = directory
    return headers


def _timeout(values: Mapping[str, Any], key: str = "discoveryTimeoutMs") -> float:
    value = values.get(key)
    seconds = value / 1000 if isinstance(value, int) and not isinstance(value, bool) else 2.0
    return max(0.1, seconds)


async def fetch_json(
    url: str,
    values: Mapping[str, Any],
    *,
    timeout_key: str = "discoveryTimeoutMs",
) -> tuple[int, dict[str, Any] | None]:
    """GET a JSON object, or report that the answer was not one.

    A status code alone is not evidence. OpenCode is a different local service
    that also defaults to port 4096 and answers *every* unmatched path with its
    web UI as `text/html` — including this one. Requiring a JSON content type
    and a JSON object body is what keeps the connector from attaching to it.
    """

    async with httpx.AsyncClient(timeout=_timeout(values, timeout_key)) as client:
        response = await client.get(url, headers=_headers(values))
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return response.status_code, None
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return response.status_code, None
    return response.status_code, body if isinstance(body, dict) else None


def parse_capabilities(body: Mapping[str, Any] | None) -> OpenScienceCapabilities | None:
    if not isinstance(body, Mapping):
        return None
    protocol = body.get("protocolVersion")
    if not isinstance(protocol, str) or protocol not in SUPPORTED_PROTOCOL_VERSIONS:
        return None
    server_version = body.get("serverVersion")
    retention = body.get("eventRetention")
    return OpenScienceCapabilities(
        protocol_version=protocol,
        server_version=server_version if isinstance(server_version, str) else None,
        idempotent_prompts=body.get("idempotentPrompts") is True,
        rich_inputs=body.get("richInputs") is True,
        run_snapshots=body.get("runSnapshots") is True,
        event_retention=retention if isinstance(retention, int) and not isinstance(retention, bool) else 0,
        raw=dict(body),
    )


async def probe_capabilities(
    base_url: str,
    values: Mapping[str, Any],
) -> OpenScienceCapabilities | None:
    """Return the server's capabilities only when it really speaks this protocol."""

    try:
        status, body = await fetch_json(f"{base_url.rstrip('/')}{CAPABILITIES_PATH}", values)
    except (httpx.HTTPError, OSError):
        return None
    if status != 200:
        return None
    return parse_capabilities(body)


async def _health_run_id(base_url: str, values: Mapping[str, Any]) -> str | None:
    try:
        status, body = await fetch_json(f"{base_url.rstrip('/')}{HEALTH_PATH}", values)
    except (httpx.HTTPError, OSError):
        return None
    if status != 200 or not isinstance(body, Mapping):
        return None
    if body.get("healthy") is not True:
        return None
    run_id = body.get("runId")
    return run_id if isinstance(run_id, str) else None


def read_desktop_record(values: Mapping[str, Any]) -> dict[str, Any] | None:
    path = provider_config.desktop_server_path(values)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        return None
    port = payload.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65_536:
        return None
    return payload


async def _endpoint_from_record(
    values: Mapping[str, Any],
) -> tuple[OpenScienceEndpoint, OpenScienceCapabilities] | None:
    """Attach to the desktop sidecar through the record it advertises.

    The record names the `runId` the server answers `/global/health` with, so a
    record left behind by a dead sidecar cannot make the connector adopt
    whatever else has since taken that port.
    """

    record = read_desktop_record(values)
    if record is None:
        return None
    port = int(record["port"])
    base_url = f"http://127.0.0.1:{port}"
    expected_run_id = record.get("run_id")
    if (
        isinstance(expected_run_id, str)
        and expected_run_id
        and await _health_run_id(base_url, values) != expected_run_id
    ):
        return None
    capabilities = await probe_capabilities(base_url, values)
    if capabilities is None:
        return None
    return (
        OpenScienceEndpoint(
            base_url=base_url,
            port=port,
            source="desktop-record",
            run_id=expected_run_id if isinstance(expected_run_id, str) else None,
            pid=record.get("pid") if isinstance(record.get("pid"), int) else None,
            version=record.get("version") if isinstance(record.get("version"), str) else None,
        ),
        capabilities,
    )


async def _endpoint_from_well_known_ports(
    values: Mapping[str, Any],
) -> tuple[OpenScienceEndpoint, OpenScienceCapabilities] | None:
    """Fall back to the ports a terminal `openscience serve` uses.

    A terminal server advertises nothing, so these are probed directly. The
    capabilities check still has the final say, which is what keeps a plain
    OpenCode server on 4096 from being mistaken for OpenScience.
    """

    for port in provider_config.WELL_KNOWN_PORTS:
        base_url = f"http://127.0.0.1:{port}"
        capabilities = await probe_capabilities(base_url, values)
        if capabilities is None:
            continue
        return (
            OpenScienceEndpoint(
                base_url=base_url,
                port=port,
                source="well-known-port",
                run_id=await _health_run_id(base_url, values),
                version=capabilities.server_version,
            ),
            capabilities,
        )
    return None


async def discover(values: dict[str, Any]) -> OpenScienceDiscovery:
    """Report that this connector supports the OpenScience runtime type.

    The device list only asks which runtime types a connector supports, so
    discovery must not touch the network. Reachability belongs to probe().
    """

    return OpenScienceDiscovery(
        True,
        True,
        None,
        metadata=_static_metadata(values),
    )


async def probe(values: dict[str, Any]) -> OpenScienceDiscovery:
    """Attach to a running OpenScience server. Configuration and start only."""

    configured_url = values.get("baseUrl")
    if isinstance(configured_url, str) and configured_url:
        base_url = configured_url.rstrip("/")
        capabilities = await probe_capabilities(base_url, values)
        if capabilities is None:
            return OpenScienceDiscovery(
                False,
                False,
                None,
                reason=f"无法连接到 {base_url}，或它没有提供 OpenScience 运行时协议。",
                metadata=_static_metadata(values),
            )
        port = 0
        if "://" in base_url:
            tail = base_url.split("://", 1)[1]
            host_part = tail.split("/", 1)[0]
            if ":" in host_part:
                raw_port = host_part.rsplit(":", 1)[1]
                if raw_port.isdigit():
                    port = int(raw_port)
        return OpenScienceDiscovery(
            True,
            True,
            OpenScienceEndpoint(base_url=base_url, port=port, source="configured",
                                run_id=await _health_run_id(base_url, values),
                                version=capabilities.server_version),
            capabilities,
            metadata=_static_metadata(values),
        )

    for finder in (_endpoint_from_record, _endpoint_from_well_known_ports):
        try:
            found = await finder(values)
        except (httpx.HTTPError, OSError):
            found = None
        if found is None:
            continue
        endpoint, capabilities = found
        metadata = _static_metadata(values)
        metadata.update(
            {
                "endpointSource": endpoint.source,
                "serverVersion": capabilities.server_version,
                "eventRetention": capabilities.event_retention,
            }
        )
        return OpenScienceDiscovery(True, True, endpoint, capabilities, metadata=metadata)

    return OpenScienceDiscovery(
        False,
        False,
        None,
        reason=UNAVAILABLE_REASON,
        metadata=_static_metadata(values),
    )
