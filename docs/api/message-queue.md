# Session message queue

A session runs one turn at a time. Sending a message while a turn is running
used to be rejected, so the message was simply lost. The queue accepts it,
holds it durably, and dispatches it when the current turn finishes.

This applies to every runtime (Codex, Claude, DSH). A runtime that supports
mid-turn steering (`session.steer`) is unaffected: there the message is steered
into the running turn instead of queued.

## Why a queue and not steering

Steering is a runtime capability, not a platform one. DSH declares
`steerTurn: false` and reports `session.steer` as unsupported, but it does
support `session.send_message`. Treating "running and cannot steer" as "cannot
send" is what previously left the composer disabled for the whole run.

So the client asks for a queued send when the runtime can send but not steer,
and steers when it can.

## HTTP API

All paths are under `/api/v2` and require the session's user.

### Send with queueing

```
POST /sessions/{sessionId}/runtime/messages
{
  "content": "…",
  "clientMessageId": "opt_…",
  "attachments": [{"fileId": "…"}],
  "queueWhenBusy": true
}
```

- With `queueWhenBusy` absent or false, a busy runtime answers `409` with
  `{"detail": "session is <status>"}` — unchanged behaviour.
- With `queueWhenBusy: true` and a busy runtime, the message is accepted and the
  response carries `result.queued = true` plus `result.item`, the queued row.
- Capabilities are still checked on the queueing path: a runtime that cannot
  send at all will not be able to send the queued message either.
- Attachments are persisted when the message is queued, not when it is
  dispatched, so a queued item survives a restart.

### List the queue

```
GET /sessions/{sessionId}/runtime/queue
→ { "sessionId": "…", "items": [QueuedMessageView…], "serverTime": "…" }
```

Items are returned in dispatch order (oldest first). `items` includes rows that
already left the queue, so a client can show what became of a message.

### Edit a queued message

```
PATCH /sessions/{sessionId}/runtime/queue/{itemId}
{ "content": "…" }
→ { "item": QueuedMessageView, "serverTime": "…" }
```

### Remove a queued message

```
DELETE /sessions/{sessionId}/runtime/queue/{itemId}
→ { "item": QueuedMessageView, "serverTime": "…" }
```

## Item states

| `status` | Meaning | Editable / removable |
| --- | --- | --- |
| `queued` | Waiting for the current turn to finish. | Yes |
| `sending` | Claimed for dispatch. | No |
| `sent` | Handed to the runtime. | No |
| `failed` | Dispatch failed; `errorCode`/`errorMessage` explain why. | No |

Only `queued` items can be changed. Once an item is claimed the runtime owns it,
and rewriting it would misreport what actually ran. Editing or removing a
non-pending item answers `409` with
`{"code": "session/queue-item-not-found"}`; the client should refresh the queue
rather than treat the id as invalid, because the item may have been sent.

An empty message (no text and no attachment) is rejected with
`{"code": "session/queue-empty-message"}`.

## Dispatch

Dispatch is triggered by the observation that a turn ended: the connector's
runtime-status notification puts the session into `idle` or `error`, and the
server then hands the queue head to the runtime through the ordinary send path.

Properties the implementation guarantees:

- **One message per finished turn.** Sending starts a new turn, so the next item
  must wait for that turn to finish. This is what produces "the queue advances
  as the current task completes".
- **FIFO.** Items are dispatched by `position`, which is assigned on enqueue.
- **Exactly once.** Claiming is a single guarded `UPDATE … WHERE status =
  'queued'`, so two concurrent dispatchers cannot both take the same row.
- **No silent loss.** An item is marked `sent` only after the runtime accepted
  it. A dispatch failure marks it `failed` with the reason; a runtime that
  started working again in the meantime puts it back at `queued`.
- **Isolated per session.** One session's queue never affects another's.

Dispatch runs as a background task so a slow connector cannot stall the ingest
pipeline that observed the status change.

## Storage

`session_message_queue` (migration `v2_36`):

| Column | Notes |
| --- | --- |
| `id` | Queue item id (`queue_…`). |
| `session_id` | Owning session; `ON DELETE CASCADE`. |
| `user_id` | Who queued it; dispatch sends as this user. |
| `position` | Submission order. |
| `status` | `queued` / `sending` / `sent` / `failed`. |
| `content` | Message text. |
| `attachments_json` | Persisted attachment refs (JSON array). |
| `selections_json` | Reserved for per-message selections. |
| `client_message_id` | Client idempotency key. |
| `error_code`, `error_message` | Failure detail for `failed`. |
| `created_at`, `updated_at`, `updated_seq` | Bookkeeping. |

Index `idx_session_message_queue_position (session_id, position)` supports taking
the queue head.

## Client guidance

- Show pending items above the composer so a reader can see the message was
  accepted rather than lost.
- Offer edit and remove only for `queued` items.
- Refresh the queue after a queued send; the row appears immediately rather than
  looking dropped.
- Show a queue-aware composer placeholder while running instead of the
  "send an interrupt or wait" text, which describes a composer that would not
  accept typing at all.
