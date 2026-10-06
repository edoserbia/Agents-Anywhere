"""Provider for the OpenScience runtime type.

The provider describes a type and validates how to reach it; it never starts
anything. OpenScience is an application the user runs, and the Connector is a
client of it, so "available" here means "an OpenScience server is reachable
with this configuration" — never "we launched one".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from jsonschema import Draft202012Validator

from connector.logging import logger
from connector.runtime_protocol import (
    AgentRuntime,
    RuntimeConfig,
    RuntimeConfigSchema,
    RuntimeInvalidRequestError,
    RuntimeProvider,
    RuntimeResourceClaim,
    RuntimeSourceKey,
    RuntimeTypeDescriptor,
)
from connector.runtime_protocol.filesystem import filesystem_resource_key
from connector.runtime_protocol.host import RuntimeHostClient
from connector.runtimes.openscience import discovery, provider_config
from connector.runtimes.openscience.runtime import (
    OpenScienceRuntime,
    runtime_capabilities,
)

OPENSCIENCE_CONFIG_SCHEMA_REVISION = 1
RUNTIME_TYPE = "openscience"

Discovery = Callable[[dict[str, Any]], Awaitable[discovery.OpenScienceDiscovery]]
Probe = Callable[[dict[str, Any]], Awaitable[discovery.OpenScienceDiscovery]]


class OpenScienceProvider(RuntimeProvider):
    def __init__(
        self,
        discoverer: Discovery | None = None,
        prober: Probe | None = None,
    ) -> None:
        self._discoverer = discoverer or discovery.discover
        # Reachability belongs to configuration and start. An injected
        # discoverer drives both so a single fake covers the whole surface.
        self._prober = prober or discoverer or discovery.probe
        self._last_discovery: discovery.OpenScienceDiscovery | None = None
        self._last_values = provider_config.default_config_values()

    def _remember(self, result: discovery.OpenScienceDiscovery) -> None:
        self._last_discovery = result

    @property
    def runtime(self) -> str:
        return RUNTIME_TYPE

    @property
    def runtime_type(self) -> str:
        return RUNTIME_TYPE

    @property
    def implementation_type(self) -> str:
        return "local-service"

    @property
    def display_name(self) -> str:
        return "OpenScience"

    @property
    def description(self) -> str:
        return "Attach to a running OpenScience server"

    async def discover(self) -> RuntimeTypeDescriptor:
        """Report the supported runtime type. Reachability is not discovery."""

        values = self._last_values
        result = await self._discoverer(values)
        self._remember(result)
        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "protocolVersion": discovery.SUPPORTED_PROTOCOL_VERSIONS[0],
                "implementationType": "local-service",
                "configured": result.configured,
                "attachOnly": True,
            }
        )
        return RuntimeTypeDescriptor(
            runtime_type=self.runtime_type,
            display_name=self.display_name,
            description=self.description,
            implementation_type=self.implementation_type,
            available=result.available,
            capabilities=runtime_capabilities(),
            reason=None if result.available else result.reason or discovery.UNAVAILABLE_REASON,
            config_schema=self._config_schema(),
            instance_policy=self.instance_policy,
            max_instances=self.max_instances,
            recommended=False,
            metadata=metadata,
        )

    async def get_config_schema(self) -> RuntimeConfigSchema:
        return self._config_schema()

    def _config_schema(self) -> RuntimeConfigSchema:
        return RuntimeConfigSchema(
            runtime=self.runtime,
            revision=OPENSCIENCE_CONFIG_SCHEMA_REVISION,
            schema=provider_config.openscience_config_schema(),
            ui_schema={
                "order": [
                    "baseUrl",
                    "directory",
                    "authToken",
                    "dataRoot",
                    "discoveryTimeoutMs",
                    "requestTimeoutMs",
                ],
                "directory": {"component": "path"},
                "dataRoot": {"component": "path"},
                "authToken": {"component": "password"},
            },
            defaults=provider_config.default_config_values(),
            metadata={
                "protocolVersion": discovery.SUPPORTED_PROTOCOL_VERSIONS[0],
                "implementationType": "local-service",
                "attachOnly": True,
            },
        )

    async def validate_config(self, values: Mapping[str, Any]) -> RuntimeConfig:
        raw = dict(values)
        schema_info = self._config_schema()
        errors = sorted(
            Draft202012Validator(schema_info.schema).iter_errors(raw),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            path = "/" + "/".join(str(part) for part in errors[0].absolute_path)
            raise RuntimeInvalidRequestError(
                f"openscience config is invalid at {path or '/'}: {errors[0].message}"
            )
        normalized = provider_config.normalized_config_values(raw)
        result = await self._probe(normalized)
        self._remember(result)
        self._last_values = normalized
        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "protocolVersion": discovery.SUPPORTED_PROTOCOL_VERSIONS[0],
                "implementationType": "local-service",
                "configured": result.configured,
                "attached": result.available,
                "attachOnly": True,
                "readOnly": False,
            }
        )
        capabilities = result.capabilities
        if capabilities is not None:
            metadata["serverVersion"] = capabilities.server_version
            metadata["eventRetention"] = capabilities.event_retention
            metadata["crashRecovery"] = capabilities.raw.get("crashRecovery")
        endpoint = result.endpoint
        if endpoint is not None:
            metadata["endpointSource"] = endpoint.source
            metadata["baseUrl"] = endpoint.base_url
        # An unreachable server is a temporary state, not an invalid
        # configuration: the runtime owns reconnection and re-reads the
        # endpoint when OpenScience appears.
        return RuntimeConfig(
            runtime=self.runtime,
            revision=OPENSCIENCE_CONFIG_SCHEMA_REVISION,
            values=normalized,
            schema=schema_info.schema,
            ui_schema=schema_info.ui_schema,
            metadata=metadata,
        )

    async def _probe(self, values: dict[str, Any]) -> discovery.OpenScienceDiscovery:
        try:
            return await self._prober(values)
        except Exception as error:  # noqa: BLE001 - configuration stays usable while offline
            logger.warning(
                "openscience probe failed error_type={}", type(error).__name__
            )
            return discovery.OpenScienceDiscovery(
                False, False, None, reason=discovery.UNAVAILABLE_REASON
            )

    async def create_runtime(
        self,
        config: RuntimeConfig,
        host: RuntimeHostClient,
    ) -> AgentRuntime:
        return OpenScienceRuntime(config=config, host=host)

    def resource_claims(
        self,
        config: RuntimeConfig,
    ) -> tuple[RuntimeResourceClaim, ...]:
        """Claim the server this instance attaches to.

        Two connector instances reading one OpenScience server would publish
        the same native sessions under two platform identities, so the source
        is exclusive. Nothing is claimed on the server process itself — the
        workbench keeps running and keeps owning its own work.
        """

        values = dict(config.values)
        base_url = values.get("baseUrl")
        if isinstance(base_url, str) and base_url:
            normalized = base_url.rstrip("/")
            return (
                RuntimeResourceClaim(
                    kind="openscience_server",
                    key=normalized,
                    label=f"OpenScience server {normalized!r}",
                ),
            )
        root = str(provider_config.data_root(values))
        return (
            RuntimeResourceClaim(
                kind="openscience_data_root",
                key=filesystem_resource_key(root),
                label=f"OpenScience data root {root!r}",
            ),
        )

    def session_source_key(self, config: RuntimeConfig) -> RuntimeSourceKey:
        """Identify where this instance's sessions come from.

        The identity is the configured server, or the data root whose desktop
        record advertises one. Neither embeds a token, and the desktop record
        is re-read on every reconnect so a new random port does not create a
        new source.
        """

        values = dict(config.values)
        base_url = values.get("baseUrl")
        if isinstance(base_url, str) and base_url:
            return RuntimeSourceKey(
                kind="openscience_server", key=base_url.rstrip("/")
            )
        return RuntimeSourceKey(
            kind="openscience_data_root",
            key=filesystem_resource_key(str(provider_config.data_root(values))),
        )
