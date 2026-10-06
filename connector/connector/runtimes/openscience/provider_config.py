from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from connector.runtime_protocol import RuntimeInvalidRequestError
from connector.runtime_protocol.filesystem import canonical_path

DEFAULT_DISCOVERY_TIMEOUT_MS = 2_000
DEFAULT_REQUEST_TIMEOUT_MS = 60_000
DEFAULT_RECONNECT_SECONDS = 2.0

# The desktop sidecar picks a random port and advertises it; a terminal
# `openscience serve` takes these two instead and advertises nothing.
WELL_KNOWN_PORTS = (4096, 4097)


def openscience_config_schema() -> dict[str, Any]:
    positive_timeout = {"type": "integer", "minimum": 100, "maximum": 600_000}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "baseUrl": {
                "type": "string",
                "maxLength": 2048,
                "title": "OpenScience 地址",
                "description": "留空时自动发现本机正在运行的 OpenScience；填写后只连接该地址。",
            },
            "directory": {
                "type": "string",
                "maxLength": 4096,
                "title": "项目目录",
                "description": "OpenScience 服务端上的绝对路径，作为会话的默认项目上下文。",
            },
            "authToken": {
                "type": "string",
                "maxLength": 4096,
                "title": "访问令牌",
                "description": "服务端设置了 OPENSCIENCE_AUTH_TOKEN 时填写；留空表示未启用。",
            },
            "dataRoot": {
                "type": "string",
                "maxLength": 4096,
                "title": "数据目录",
                "description": "留空时按 XDG 规则解析 OpenScience 的数据根，用于读取桌面端的服务记录。",
            },
            "discoveryTimeoutMs": {
                **positive_timeout,
                "default": DEFAULT_DISCOVERY_TIMEOUT_MS,
            },
            "requestTimeoutMs": {
                **positive_timeout,
                "default": DEFAULT_REQUEST_TIMEOUT_MS,
            },
        },
        "additionalProperties": False,
    }


def default_config_values() -> dict[str, Any]:
    return {
        "discoveryTimeoutMs": DEFAULT_DISCOVERY_TIMEOUT_MS,
        "requestTimeoutMs": DEFAULT_REQUEST_TIMEOUT_MS,
    }


def normalized_config_values(raw: dict[str, Any]) -> dict[str, Any]:
    values = {**default_config_values(), **raw}
    for key in ("baseUrl", "authToken", "directory", "dataRoot"):
        value = values.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise RuntimeInvalidRequestError(f"{key} must be a string")
        stripped = value.strip()
        if stripped:
            values[key] = stripped
        else:
            values.pop(key)
    base_url = values.get("baseUrl")
    if base_url is not None and not base_url.startswith(("http://", "https://")):
        raise RuntimeInvalidRequestError("baseUrl must start with http:// or https://")
    directory = values.get("directory")
    if directory is not None:
        if not Path(directory).expanduser().is_absolute():
            raise RuntimeInvalidRequestError("directory must be an absolute path")
        values["directory"] = canonical_path(directory)
    data_root = values.get("dataRoot")
    if data_root is not None:
        if not Path(data_root).expanduser().is_absolute():
            raise RuntimeInvalidRequestError("dataRoot must be an absolute path")
        values["dataRoot"] = canonical_path(data_root)
    for key in ("discoveryTimeoutMs", "requestTimeoutMs"):
        value = values.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 100 <= value <= 600_000
        ):
            raise RuntimeInvalidRequestError(
                f"{key} must be an integer between 100 and 600000"
            )
    return values


def config_dir() -> Path:
    """Where OpenScience keeps its configuration, following XDG."""

    override = os.environ.get("XDG_CONFIG_HOME")
    base = Path(override) if override else Path.home() / ".config"
    return Path(canonical_path(base / "openscience"))


def data_root(values: dict[str, Any]) -> Path:
    """Resolve the OpenScience data root the same way its CLI does.

    The root is reachable through a `data-root` link in the config directory so
    the app can relocate it without every process recomputing paths, and that
    link is authoritative when it exists. A server that never created it falls
    back to the default `~/.openscience`, and only then to the XDG data
    directory that earlier releases used.
    """

    configured = values.get("dataRoot")
    if isinstance(configured, str):
        return Path(canonical_path(configured))
    link = config_dir() / "data-root"
    try:
        if link.is_symlink():
            return Path(canonical_path(link))
    except OSError:
        pass
    default = Path.home() / ".openscience"
    if default.is_dir():
        return Path(canonical_path(default))
    override = os.environ.get("XDG_DATA_HOME")
    base = Path(override) if override else Path.home() / ".local" / "share"
    return Path(canonical_path(base / "openscience"))


def desktop_server_path(values: dict[str, Any]) -> Path:
    return Path(canonical_path(data_root(values) / "desktop-server.json"))
