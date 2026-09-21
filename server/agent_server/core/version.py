"""Single source of truth for the server release version.

`/api/v2/health` reports this value, and operators compare it against the client
build they shipped. It is declared once here; `pyproject.toml` carries the same
number for packaging, and `tests/test_version.py` fails if the two ever drift.
"""

from __future__ import annotations

from typing import Final

SERVER_VERSION: Final = "2.0.12"
