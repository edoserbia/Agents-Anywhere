"""The compact command supported by AA's native Codex runtime."""

from connector.runtime_protocol import RuntimeCommand

IDLE = ("idle", "error")
ALIASES = {"compact-thread": "compact"}


def list_codex_commands(
    external_session_id: str | None,
    client_available: bool,
    query: str | None = None,
    limit: int = 50,
    *,
    status: str = "idle",
    source_availability: str | None = None,
) -> tuple[RuntimeCommand, ...]:
    reason = None
    if not client_available:
        reason = "codex_unavailable"
    elif not external_session_id:
        reason = "session_unloaded"
    elif source_availability in {"archived", "unavailable", "deleted", "missing"}:
        reason = f"session_{source_availability}"
    elif status not in IDLE:
        reason = f"session_{status}"
    command = RuntimeCommand(
        id="compact",
        title="Compact",
        description="Compact the session context.",
        aliases=tuple(ALIASES),
        enabled=reason is None,
        disabled_reason=reason,
        metadata={
            "ui": {
                "kind": "execute",
                "allowedStatuses": list(IDLE),
                "acceptsMultiline": False,
            }
        },
    )
    needle = (query or "").strip().casefold()
    return tuple(
        c
        for c in (command,)
        if not needle
        or needle
        in " ".join((c.id, c.title, c.description or "", *c.aliases)).casefold()
    )[: max(0, min(limit, 1000))]
