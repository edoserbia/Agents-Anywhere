from __future__ import annotations

from typing import Final

from agent_server.core.protocol import ProtocolCapability, ProtocolCapabilitySet

SESSION_SEND_MESSAGE: Final = "session.send_message"
SESSION_INTERRUPT: Final = "session.interrupt"
SESSION_STEER: Final = "session.steer"
SESSION_COMMANDS: Final = "session.commands"
SESSION_INTERACTION_APPROVAL: Final = "session.interaction.approval"
RUNTIME_ATTACHMENT: Final = "runtime.attachment"
RUNTIME_CONFIG: Final = "runtime.config"
CATALOG_MODEL: Final = "catalog.model"
CATALOG_PERMISSION: Final = "catalog.permission"
CATALOG_EFFORT: Final = "catalog.effort"


def find_capability(
    capability_set: ProtocolCapabilitySet,
    capability_id: str,
) -> ProtocolCapability | None:
    return next(
        (
            capability
            for capability in capability_set.capabilities
            if capability.capabilityId == capability_id
        ),
        None,
    )


def capability_is_usable(
    capability_set: ProtocolCapabilitySet,
    capability_id: str,
) -> bool:
    capability = find_capability(capability_set, capability_id)
    return bool(
        capability is not None
        and capability.supported
        and capability.available
        and capability.allowed
    )


# Human-readable text for the reasons clients already localize. The reason is
# still carried as the error code, so an unmapped reason is never flattened:
# clients can key on the code and fall back to the message.
_CAPABILITY_REASON_MESSAGES: dict[str, str] = {
    "session_not_taken_over": "session is read-only until takeover is enabled",
    "connector_offline": "connector is offline",
    "runtime_capability_unsupported": "the runtime does not support this action",
    "runtime_capability_unavailable": "the runtime is not currently available",
}


def capability_unavailable_message(capability_id: str, reason: str | None) -> str:
    if reason is not None and reason in _CAPABILITY_REASON_MESSAGES:
        return _CAPABILITY_REASON_MESSAGES[reason]
    if reason:
        return f"session capability {capability_id} is unavailable: {reason}"
    return f"session capability is unavailable: {capability_id}"


def capability_unavailable_detail(
    capability_set: ProtocolCapabilitySet,
    capability_id: str,
) -> dict[str, str]:
    """Build the 409 detail for a blocked action, preserving the true reason."""
    capability = find_capability(capability_set, capability_id)
    reason = capability.unavailableReason if capability is not None else None
    return {
        "code": reason or "session_capability_unavailable",
        "message": capability_unavailable_message(capability_id, reason),
        "capabilityId": capability_id,
    }
