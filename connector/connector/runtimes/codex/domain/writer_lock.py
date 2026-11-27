"""Describe the Codex process that currently holds a thread's writer lock.

Codex allows one writer per thread and guards it with
``$CODEX_HOME/thread-writer-locks/<thread-id>.lock``. A Connector that loses
that race can only tell the user to release the thread somewhere else, so being
able to name the holding process is the difference between an actionable error
and "exit your Codex client, whichever one that is".

The lock file stays on disk after its holder exits, so existence alone proves
nothing; the owner is identified by the process whose open-file table still
holds it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psutil

WRITER_LOCK_DIRECTORY = "thread-writer-locks"


def writer_lock_path(
    codex_home: str | os.PathLike[str],
    thread_id: str,
) -> Path:
    return Path(codex_home) / WRITER_LOCK_DIRECTORY / f"{thread_id}.lock"


def describe_thread_writer(
    codex_home: str | os.PathLike[str] | None,
    thread_id: str,
) -> str | None:
    """Return ``<name> (pid N: <command>)`` for the lock holder, if visible.

    Returns ``None`` when the lock is free, missing, or owned by a process this
    user cannot inspect.
    """

    if not codex_home or not thread_id:
        return None
    lock = writer_lock_path(codex_home, thread_id)
    try:
        target = lock.resolve(strict=True)
    except OSError:
        return None
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if not any(_same_file(opened.path, target) for opened in process.open_files()):
                continue
        except (psutil.Error, OSError):
            continue
        return _describe(process.info)
    return None


def _same_file(opened: str, target: Path) -> bool:
    try:
        return Path(opened).resolve() == target
    except (OSError, ValueError):
        return False


def _describe(info: Mapping[str, Any]) -> str:
    name = info.get("name") or "codex"
    pid = info.get("pid")
    command = " ".join(info.get("cmdline") or ())
    command = _executable_name(command) or name
    if isinstance(pid, int):
        return f"{name} (pid {pid}: {command})"
    return f"{name} ({command})"


def _executable_name(command: str) -> str:
    """Keep the short command that identifies the client, not its full argv."""

    if not command:
        return ""
    limits = 120
    return command if len(command) <= limits else command[: limits - 1] + "…"
