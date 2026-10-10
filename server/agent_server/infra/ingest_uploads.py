"""Resumable ingest uploads.

A complete timeline snapshot is atomic: the Server replaces a session's whole
timeline with it, and `connector_notifications.TimelineNotificationHandler`
enforces that. It therefore cannot be split into independently applied pieces.
On a slow uplink a large snapshot is also the request most likely to be
interrupted — measured on the deployment this was written for, a 14 MB history
takes about twelve minutes at 19.4 KB/s — and re-sending it from zero on every
drop is what turns a flaky link into a session that never finishes its first
sync.

So the bytes are staged instead. The Connector sends the body in pieces, each
one answered with the total received, and the Server parses and applies the
payload only once all of it has arrived. An interrupted transfer resumes at the
last acknowledged offset rather than restarting.

Pieces are keyed by an unguessable identifier and bound to the Connector that
created them, are swept on a time-to-live, and are capped in size. Nothing here
interprets the payload; that stays with the ingest service.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

# A body larger than this is refused outright rather than staged, so an
# unauthenticated-looking flood cannot fill the disk.
DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# How long an unfinished transfer is kept before it is swept.
DEFAULT_UPLOAD_TTL_SECONDS = 3600.0

# Live staged transfers one Connector may hold. A working Connector stages one
# body at a time, so this is generous; it exists so the time-to-live is not the
# only thing standing between a misbehaving client and the disk.
DEFAULT_MAX_UPLOADS_PER_CONNECTOR = 8

_UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class IngestUploadError(Exception):
    """Base class for staged-upload failures."""


class IngestUploadNotFound(IngestUploadError):
    """No such upload, or it expired, or it belongs to another Connector."""


class IngestUploadConflict(IngestUploadError):
    """The piece does not continue from what has already been received."""

    def __init__(self, received_bytes: int) -> None:
        super().__init__("upload piece does not continue the staged body")
        self.received_bytes = received_bytes


class IngestUploadTooLarge(IngestUploadError):
    """The staged body would exceed the cap."""


@dataclass(frozen=True, slots=True)
class UploadHandle:
    upload_id: str
    received_bytes: int


class IngestUploadStore:
    """Stage request bodies on disk so a dropped transfer can resume."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        ttl_seconds: float = DEFAULT_UPLOAD_TTL_SECONDS,
        max_uploads_per_connector: int = DEFAULT_MAX_UPLOADS_PER_CONNECTOR,
        clock=time.time,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.max_uploads_per_connector = max_uploads_per_connector
        self._clock = clock
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, connector_id: str, encoding: str | None = None) -> UploadHandle:
        """Open a transfer.

        The encoding of the staged bytes travels with them: a Connector
        compresses a large body before staging it, and the Server must know how
        to read what it assembled.
        """

        self.sweep()
        self._evict_oldest_for(connector_id)
        upload_id = secrets.token_urlsafe(24)
        self._meta_path(upload_id).write_text(
            json.dumps({
                "connectorId": connector_id,
                "createdAt": self._clock(),
                "encoding": encoding,
            }),
            encoding="utf-8",
        )
        self._body_path(upload_id).write_bytes(b"")
        return UploadHandle(upload_id=upload_id, received_bytes=0)

    def append(
        self, connector_id: str, upload_id: str, offset: int, chunk: bytes
    ) -> UploadHandle:
        meta = self._require(connector_id, upload_id)
        received = self._received(upload_id)
        if offset != received:
            raise IngestUploadConflict(received)
        if received + len(chunk) > self.max_bytes:
            raise IngestUploadTooLarge("staged upload exceeds the size limit")
        with self._body_path(upload_id).open("ab") as body:
            body.write(chunk)
        # Touch the metadata so a long transfer is not swept while it runs.
        meta["createdAt"] = self._clock()
        self._meta_path(upload_id).write_text(json.dumps(meta), encoding="utf-8")
        return UploadHandle(upload_id=upload_id, received_bytes=received + len(chunk))

    def received(self, connector_id: str, upload_id: str) -> UploadHandle:
        self._require(connector_id, upload_id)
        return UploadHandle(upload_id=upload_id, received_bytes=self._received(upload_id))

    def read(self, connector_id: str, upload_id: str) -> tuple[bytes, str | None]:
        meta = self._require(connector_id, upload_id)
        encoding = meta.get("encoding")
        return (
            self._body_path(upload_id).read_bytes(),
            encoding if isinstance(encoding, str) else None,
        )

    def discard(self, connector_id: str, upload_id: str) -> None:
        self._require(connector_id, upload_id)
        self._remove(upload_id)

    def _evict_oldest_for(self, connector_id: str) -> int:
        """Keep one Connector's staged transfers bounded.

        The time-to-live alone bounds disk only if the rate is low: a Connector
        that keeps opening transfers inside the window would otherwise hold as
        many as it liked, each up to the per-transfer maximum. The oldest goes
        first, and a working Connector stages one body at a time, so the ones it
        still needs are the newest.
        """

        live: list[tuple[float, str]] = []
        for meta_path in self.root.glob("*.meta"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("connectorId") == connector_id:
                live.append((float(meta.get("createdAt", 0.0)), meta_path.stem))
        removed = 0
        for _, upload_id in sorted(live)[: max(0, len(live) - self.max_uploads_per_connector + 1)]:
            self._remove(upload_id)
            removed += 1
        return removed

    def sweep(self) -> int:
        """Drop staged transfers that were never finished."""

        deadline = self._clock() - self.ttl_seconds
        removed = 0
        for meta_path in self.root.glob("*.meta"):
            upload_id = meta_path.stem
            try:
                created_at = float(json.loads(meta_path.read_text(encoding="utf-8"))["createdAt"])
            except (OSError, ValueError, KeyError, TypeError):
                created_at = 0.0
            if created_at < deadline:
                self._remove(upload_id)
                removed += 1
        return removed

    def _require(self, connector_id: str, upload_id: str) -> dict:
        if not _UPLOAD_ID_PATTERN.match(upload_id):
            raise IngestUploadNotFound("unknown upload")
        try:
            meta = json.loads(self._meta_path(upload_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise IngestUploadNotFound("unknown upload") from None
        if not isinstance(meta, dict) or meta.get("connectorId") != connector_id:
            # Another Connector's transfer is not this Connector's to touch, and
            # a wrong guess must look exactly like a missing upload.
            raise IngestUploadNotFound("unknown upload")
        return meta

    def _received(self, upload_id: str) -> int:
        try:
            return self._body_path(upload_id).stat().st_size
        except OSError:
            raise IngestUploadNotFound("unknown upload") from None

    def _remove(self, upload_id: str) -> None:
        for path in (self._body_path(upload_id), self._meta_path(upload_id)):
            try:
                path.unlink()
            except OSError:
                pass

    def _body_path(self, upload_id: str) -> Path:
        return self.root / f"{upload_id}.part"

    def _meta_path(self, upload_id: str) -> Path:
        return self.root / f"{upload_id}.meta"
