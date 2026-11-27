"""Writer-lock holder identification for Codex threads.

Codex leaves the lock file behind after its holder exits, so these tests pin the
difference between "file exists" and "a process still holds it": the error the
user sees must name the client that actually has to be closed.
"""

from __future__ import annotations

import subprocess
import sys
import time

from connector.runtimes.codex.domain.writer_lock import (
    describe_thread_writer,
    writer_lock_path,
)
from connector.runtimes.codex.sdk.client import CodexSdkClient

THREAD_ID = "01a20d36-d755-7a31-8901-61916dc54569"


def test_missing_lock_reports_no_holder(tmp_path):
    assert describe_thread_writer(tmp_path, THREAD_ID) is None


def test_lock_file_left_behind_reports_no_holder(tmp_path):
    path = writer_lock_path(tmp_path, THREAD_ID)
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    assert describe_thread_writer(tmp_path, THREAD_ID) is None


def test_open_lock_names_the_holding_process(tmp_path):
    path = writer_lock_path(tmp_path, THREAD_ID)
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    script = (
        "import sys, time\n"
        "handle = open(sys.argv[1], 'rb')\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        described = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and described is None:
            described = describe_thread_writer(tmp_path, THREAD_ID)
            if described is None:
                time.sleep(0.05)
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    assert described is not None
    assert f"pid {holder.pid}" in described
    assert "python" in described


def test_conflict_message_names_the_holder_or_stays_silent():
    client = object.__new__(CodexSdkClient)
    client._writer_hint = lambda thread_id: "codex (pid 42: codex resume)"
    assert client._writer_hint_message(THREAD_ID) == (
        "当前占用者：codex (pid 42: codex resume)。"
    )

    client._writer_hint = lambda thread_id: None
    assert client._writer_hint_message(THREAD_ID) == ""

    client._writer_hint = None
    assert client._writer_hint_message(THREAD_ID) == ""

    def broken(thread_id: str) -> str | None:
        raise PermissionError("process table unavailable")

    client._writer_hint = broken
    assert client._writer_hint_message(THREAD_ID) == ""
