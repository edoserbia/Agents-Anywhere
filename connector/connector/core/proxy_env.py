"""Make the process's proxy environment safe for httpx to read.

`httpx` turns the bypass list into URL patterns by mounting every entry —
`::1` becomes `all://::1` — and an entry it cannot parse makes *client
construction itself* raise:

    httpx.InvalidURL: Invalid port: ':1]'

Nothing about the machine is wrong; it is an over-eager bypass list, and on
macOS `urllib` merges the environment's `NO_PROXY` with the system proxy
configuration, so an entry can appear without anyone writing it in the shell.
The Connector would then not start at all when launched from such an
environment, while the same build launched from the desktop app (which inherits
no shell environment) starts fine — a difference nobody would guess at.

Only entries httpx cannot use are dropped. Genuine bypass entries survive, and
`HTTP_PROXY`/`HTTPS_PROXY` are never touched: a deployment that needs a proxy to
reach the Server keeps it.
"""

from __future__ import annotations

import os

# Both spellings: `urllib` folds them into one key, and either can be the one
# that carries the unparseable entry.
PROXY_VARIABLES = ("NO_PROXY", "no_proxy")


def normalize_no_proxy() -> list[str]:
    """Drop bypass entries httpx cannot parse. Returns the variables changed.

    Idempotent: once normalized there is nothing left to remove, so this is safe
    to call before every client construction.
    """

    if not _proxy_support_available():
        return []
    present = [name for name in PROXY_VARIABLES if name in os.environ]
    if not present:
        return []
    # urllib folds both spellings into one key, so they are normalized together:
    # testing one while the other still carries an unusable entry would condemn
    # every entry in the list.
    entries: list[str] = []
    for name in present:
        for entry in os.environ[name].split(","):
            if entry not in entries:
                entries.append(entry)
    kept: list[str] = []
    for entry in entries:
        if _bypass_list_parses(present, [*kept, entry]):
            kept.append(entry)
    if len(kept) == len(entries):
        return []
    for name in present:
        os.environ[name] = ",".join(kept)
    return present


def _bypass_list_parses(names: list[str], entries: list[str]) -> bool:
    """Ask httpx itself whether it can use this bypass list.

    Testing against the library's own parsing rather than a copy of its rules is
    what keeps this correct across versions. The failure happens in two steps —
    the bypass list becomes mount patterns, and each pattern is parsed when the
    client is built — so both are performed here.
    """

    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ[name] = ",".join(entries)
    try:
        for pattern in _environment_mounts():
            _url_pattern(pattern)
        return True
    except Exception:  # noqa: BLE001 - an unusable entry is exactly the point
        return False
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _environment_mounts() -> dict:
    from httpx._utils import get_environment_proxies

    return get_environment_proxies()


def _url_pattern(pattern: str):
    from httpx._utils import URLPattern

    return URLPattern(pattern)


def _proxy_support_available() -> bool:
    """Whether the internals this relies on are where they were."""

    try:
        _environment_mounts, _url_pattern  # noqa: B018 - import check only
        _environment_mounts()
    except Exception:  # noqa: BLE001 - a moved helper must not break startup
        return False
    return True
