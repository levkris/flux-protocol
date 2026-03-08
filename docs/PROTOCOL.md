# FLUX Protocol Specification

## Versions

| Thing | Value | Notes |
|---|---|---|
| Protocol (`v` field) | `1.0` | Wire format version. In every message envelope and signature. Only bumps when signing algorithm or required fields change. |
| Software / API | `2.0.0` | Semver. Returned by `/health` and `/federation/info`. |

### Changelog

#### 2.0.0
**Breaking:**
- Removed `POST /ack`. Messages are never deleted; use `POST /read` or `POST /delete` instead.
- WebSocket `ack` action removed; replaced by `read` and `delete`.

**Added:**
- `subject`, `cc`, `bcc`, `tags`, `expires`, `route` envelope fields.
- Persistent inbox — messages survive across fetches.
- Multiple inboxes per address.
- Tags with two reserved globals: `important`, `favorited`.
- `expires` — message soft-deletes itself after first read.
- `route` — append-only hop metadata added by each server.
- Endpoints: `/inbox`, `/inboxes`, `/read`, `/delete`, `/tag`, `/move`.
- Account routes: `/mail/read`, `/mail/delete`, `/mail/tag`, `/mail/move`, `/mail/inboxes`.
- Mesh system: `/mesh/relay`, `/mesh/info`, `mesh.config.json`.
- CC/BCC fan-out with BCC stripping.

#### 1.0.0
Initial release.

---

## Core Design Principles

- **Identity is a keypair.** No usernames, no passwords. Address derived from public key.
- **Every message is signed.** Spoofing is cryptographically impossible.
- **Messages are never destroyed.** Deletion is a status change. Servers retain all messages permanently.
- **Content is content.** Plain UTF-8. No MIME types.
- **Transport is pluggable.** HTTP and WebSocket are first-class.
- **Routing is transparent.** Every server a message passes through appends itself to `route`.

---

## Addresses

```
address = "fx1" + SHA-256(public_key_bytes)[0:40 hex chars]
```

---

## Message Envelope

| Field     | Type       | Required | Description |
|-----------|------------|----------|-------------|
| `v`       | string     | yes      | Protocol version. Must be `"1.0"` |
| `id`      | string     | yes      | UUID hex, no hyphens |
| `from`    | string     | yes      | Sender FLUX address |
| `to`      | string     | yes      | Recipient FLUX address |
| `t`       | integer    | yes      | Unix timestamp in milliseconds |
| `content` | string     | yes      | Message body. Plain UTF-8, max 65,536 bytes |
| `sig`     | string     | yes      | Ed25519 signature (base64url) |
| `pub`     | string     | yes      | Sender public key (base64url) |
| `subject` | string     | no       | Human-readable subject line |
| `re`      | string     | no       | Message ID this replies to |
| `cc`      | string[]   | no       | CC recipient FLUX addresses |
| `bcc`     | string[]   | no       | BCC recipients — stripped before delivery to others |
| `tags`    | string[]   | no       | Lowercase tags. Reserved: `important`, `favorited` |
| `expires` | integer\|0 | no       | If present, message soft-deletes after first read. `0` = expire on any read. |
| `route`   | object[]   | no       | Server hop metadata `[{server, t}, …]`. Excluded from signature. Append-only. |

### Server-added fields (retrieval only, never signed)

| Field    | Type     | Description |
|----------|----------|-------------|
| `_status`| string   | `pending`, `delivered`, `read`, `deleted` |
| `_inbox` | string   | Inbox this message lives in |
| `_tags`  | string[] | Current tags (may differ from envelope if modified server-side) |

### Signing

Excluded from signature: `sig`, `pub`, `route`, `_status`, `_inbox`, `_tags`.

```
core    = all fields except excluded set above
payload = JSON.stringify(core, keys_sorted, no_spaces)
sig     = Ed25519Sign(private_key, payload.encode("utf-8"))
```

### Verification

1. Decode `pub` from base64url.
2. Confirm `"fx1" + SHA-256(pub_bytes)[0:40]` matches `from`.
3. Reconstruct canonical payload and verify `sig`.
4. Reject if `abs(now_ms - t) > 300_000` (5 min clock skew).

---

## Message Lifecycle

Messages are **never physically deleted.**

```
  enqueued
      │
      ▼
  pending       — stored, not yet fetched
      │
      ▼  fetch / WS drain
  delivered     — fetched or pushed, not yet read
      │
      ▼  POST /read  or  WS { action: "read" }
    read         — explicitly marked read
      │             if `expires` set → goes to deleted instead of read
      ▼  POST /delete  or  WS { action: "delete" }
  deleted        — soft-deleted; hidden from default inbox; permanently retained
```

Only the recipient can move messages to `read` or `deleted`. All require auth.

---

## CC / BCC

- **CC**: recipient gets a copy with the full `cc` list intact.
- **BCC**: each BCC recipient gets a separate individually addressed copy. The `bcc` field is stripped entirely before delivery. No recipient ever sees who was BCC'd.

---

## Multiple Inboxes

Every message belongs to an inbox (default: `"inbox"`). Inboxes are created implicitly on first use. Useful patterns: `inbox`, `archive`, `work`, `spam`, `sent`.

---

## Tags

Tags are lowercase strings, stored server-side per message and queryable. Two reserved global tags:

- `important` — high priority
- `favorited` — saved / starred

Custom tags are unrestricted.

---

## HTTP Endpoints

All responses: `{"ok": true, …}` on success, `{"ok": false, "error": "…"}` on failure.

### POST /send
Submit a signed message. Appends server to `route`. Strips `bcc` before storing.

Response: `{ "ok": true, "delivery": "realtime"|"queued", "mesh": {…} }`

---

### GET /fetch/{address}
Drain pending → `delivered`. Does **not** delete. Supports `?inbox=`.

Headers: `X-Flux-Token`

---

### GET /inbox/{address}
Return all non-deleted messages. Supports `?inbox=`, `?status=`, `?tag=`.

Headers: `X-Flux-Token`

---

### GET /inboxes/{address}
List all inbox names for an address.

Headers: `X-Flux-Token`

---

### GET /peek/{address}
Count pending messages. Supports `?inbox=`.

Headers: `X-Flux-Token`

---

### POST /read
Mark a message as read. If `expires` is set, soft-deletes instead. Only recipient.

```json
{ "id": "<msg_id>", "address": "<fx1_address>" }
```
Headers: `X-Flux-Token`

---

### POST /delete
Soft-delete. Message retained permanently with status `deleted`. Only recipient.

```json
{ "id": "<msg_id>", "address": "<fx1_address>" }
```
Headers: `X-Flux-Token`

---

### POST /tag
Add or remove a tag. Only recipient.

```json
{ "id": "<msg_id>", "address": "<fx1_address>", "tag": "important", "action": "add"|"remove" }
```
Headers: `X-Flux-Token`

---

### POST /move
Move a message to a different inbox. Only recipient.

```json
{ "id": "<msg_id>", "address": "<fx1_address>", "inbox": "archive" }
```
Headers: `X-Flux-Token`

---

### GET /status/{address}
Check if address has an active WebSocket connection.

---

### GET /stats
Server-wide statistics including counts per status.

---

### GET /health
```json
{ "ok": true, "protocol": "1.0", "version": "2.0.0" }
```

---

## WebSocket Transport

Connect to `ws://<host>:<port>/ws`. All frames are JSON text.

### Auth (first frame)
```json
{ "action": "auth", "address": "fx1…", "token": "<token>", "inbox": "inbox" }
```
Response:
```json
{ "ok": true, "action": "authed", "address": "…", "inbox": "inbox", "messages": […] }
```
Pending messages are drained to `delivered` and returned in `messages`.

### Actions

| Action   | Extra fields | Description |
|----------|-------------|-------------|
| `send`   | `msg: {…}`  | Send a message |
| `read`   | `id`        | Mark as read (or soft-delete if expires set) |
| `delete` | `id`        | Soft-delete |
| `tag`    | `id`, `tag`, `tag_action: "add"\|"remove"` | Tag a message |
| `move`   | `id`, `inbox` | Move to inbox |
| `ping`   | —           | Heartbeat |

Inbound messages pushed as: `{ "type": "msg", "msg": {…} }`

---

## Authentication Token

```
token = SHA-256("flux:" + address + ":" + FLUX_SECRET)
```

---

## Mesh System

A mesh connects multiple FLUX servers into a unified delivery network. Servers in a mesh share a `token` (never transmitted raw — always SHA-256 hashed as `flux-mesh:<token>`).

### Delivery Modes

| Mode        | Behaviour |
|-------------|-----------|
| `broadcast` | Message sent to **all** peers simultaneously. Best for redundancy and archiving. |
| `chain`     | Sent to peers **in order**. Stops on first success. Best for geographic routing. |
| `hybrid`    | Checks which peer has the recipient online via `/status`. Falls back to broadcast. |

A server can be in **multiple meshes** simultaneously.

### mesh.config.json

```json
{
  "meshes": {
    "main": {
      "token": "shared-secret-all-peers-must-know",
      "mode": "broadcast",
      "peers": [
        "http://server-a.example.com:8765",
        "http://server-b.example.com:8765"
      ]
    }
  }
}
```

Copy `mesh.config.example.json` to `mesh.config.json` to get started. Add to `.gitignore`.

### Mesh Endpoints

| Method | Route       | Auth header         | Description |
|--------|-------------|---------------------|-------------|
| POST   | /mesh/relay | `X-Flux-Mesh-Token` | Receive a relayed message from a peer |
| GET    | /mesh/info  | —                   | List mesh names (no tokens exposed) |

### Route metadata

Each server appends itself on receipt:
```json
"route": [
  { "server": "server-a.example.com:8765", "t": 1741443200000 },
  { "server": "server-b.example.com:8765", "t": 1741443200500 }
]
```
`route` is excluded from the message signature.

---

## Limits

| Parameter              | Default    |
|------------------------|------------|
| Max content size       | 65,536 B   |
| Max queued per address | 500        |
| Clock skew tolerance   | 5 minutes  |
| WS heartbeat interval  | 20 seconds |

All configurable in `flux/constants.py`.