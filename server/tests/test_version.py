"""Keep the release version declared in one place.

The server version is reported by `/api/v2/health` and compared against client
builds, so a mismatch between the packaging metadata and the runtime constant
would misreport what is actually deployed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent_server.core.version import SERVER_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_matches_the_server_version_constant() -> None:
    pyproject = (REPO_ROOT / "server" / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, flags=re.MULTILINE)
    assert declared is not None, "server/pyproject.toml must declare a version"
    assert declared.group(1) == SERVER_VERSION


def test_desktop_client_ships_the_same_release_version() -> None:
    """Desktop and server are released together, so their versions must agree."""
    package = json.loads(
        (REPO_ROOT / "desktop-workbench" / "package.json").read_text(encoding="utf-8")
    )
    assert package["version"] == SERVER_VERSION


def test_android_client_ships_the_same_release_version() -> None:
    gradle = (REPO_ROOT / "android" / "app" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    version_name = re.search(r'versionName = "([^"]+)"', gradle)
    assert version_name is not None, "android/app/build.gradle.kts needs a versionName"
    assert version_name.group(1) == SERVER_VERSION
