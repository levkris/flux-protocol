# FLUX Protocol Specification - v1.0.0

FLUX (Fast Lightweight Unified eXchange) is a minimal, cryptographically authenticated messaging protocol. It is transport-agnostic, JSON-native, and designed to be simple enough that anyone can implement a client in an afternoon.

---

## Core Design Principles

- **Identity is a keypair, not a credential.** No usernames, no passwords, no registration. Your address is derived from your public key.
- **Every message is signed.** Spoofing an address is cryptographically impossible.
- **Content is content.** The body of a message is a plain UTF-8 string. No MIME types, no HTML, no embedded metadata.
- **Transport is pluggable.** HTTP and WebSocket are first-class. Both use the same message envelope.
- **Minimal envelope.** Only the fields that are strictly necessary exist.
- **Messages are never destroyed.** Deletion is a status change, not a physical removal. The server retains all messages permanently.

---

## Addresses

A FLUX address is derived from an Ed25519 public key:

```
address = "fx1" + SHA-256(public_key_bytes)[0:40 hex chars]
```

Example: `fx1a84eb946ce36ddf459fcd550663cb7e2bb38f`

Addresses are self-certifying. Possession of the private key proves ownership. No central authority is involved.

---

## Message Envelope

Every FLUX message is a JSON object with the following fields:

| Field     | Type    | Required | Description                                      |
|-----------|---------|----------|--------------------------------------------------|
| `v`       | string  | yes      | Protocol version. Must be `"1.0"`               |
| `id`      | string  | yes      | Unique message ID (UUID hex, no hyphens)        |
| `from`    | string  | yes      | Sender FLUX address                              |
| `to`      | string  | yes      | Recipient FLUX address                           |
| `t`       | integer | yes      | Unix timestamp in milliseconds                   |
| `content` | string  | yes      | Message body. Plain UTF-8, max 65,536 bytes     |
| `sig`     | string  | yes      | Ed25519 signature (base64url) over core fields  |
| `pub`     | string  | yes      | Sender public key bytes (base64url)             |
| `re`      | string  | no       | Message ID this is a reply to                   |

When messages are returned from the server (inbox, fetch), an additional field is present:

| Field     | Type   | Description                                      |
|-----------|--------|--------------------------------------------------|
| `_status` | string | Server-assigned status: `pending`, `delivered`, `read`, or `deleted` |

### Signing

The signature is computed over the **core fields only** (`v`, `id`, `from`, `to`, `t`, `content`, and `re` if present). The `sig`, `pub`, and `_status` fields are excluded from the signed payload.

```
core = {v, id, from, to, t, content [, re]}
payload = JSON.stringify(core, keys_sorted, no_spaces)
sig = Ed25519Sign(private_key, payload.encode("utf-8"))
```

### Verification

A receiving node must:

1. Decode `pub` from base64url to get the raw public key bytes.
2. Recompute `"fx1" + SHA-256(pub_bytes)[0:40]` and confirm it matches `from`.
3. Reconstruct the canonical payload and verify `sig` against it.
4. Reject messages where `abs(now_ms - t) > 300_000` (5 minute clock skew tolerance).

If any check fails, the message is rejected with a `403`.

---

## Message Lifecycle

Messages move through the following statuses. **They are never physically deleted.**

```
  [sent by sender]
        │
        ▼
    pending        — stored, not yet fetched by recipient
        │
        ▼ (fetch or WebSocket drain)
    delivered      — fetched/pushed, not yet read
        │
        ▼ (POST /read or WS read action)
      read         — recipient has marked as read
        │
        ▼ (POST /delete or WS delete action)
    deleted        — soft-deleted; hidden from default inbox view
                     but permanently retained on the server
```

Only the recipient can transition a message to `read` or `deleted`. These operations require the recipient's authentication token.

---

## HTTP Transport

All responses are JSON. Successful responses include `"ok": true`. Errors include `"ok": false` and an `"error"` string.

### POST /send

Submit a message to the node.

**Request body:** A complete FLUX message envelope (see above).

**Response:**
```json
{ "ok": true, "delivery": "realtime" }
{ "ok": true, "delivery": "queued" }
{ "ok": false, "error": "invalid signature" }
```

`delivery` is `"realtime"` if the recipient was connected via WebSocket and received the message immediately, or `"queued"` if it was stored for later retrieval.

---

### GET /fetch/{address}

Drain all **pending** messages for an address, marking them `delivered`.
Messages are not deleted — they remain on the server with status `delivered`.

**Headers:** `X-Flux-Token: <token>`

**Response:**
```json
{ "ok": true, "messages": [ ... ], "count": 2 }
```

---

### GET /inbox/{address}

Return all messages for an address without consuming them.
By default, excludes `deleted` messages.

**Headers:** `X-Flux-Token: <token>`

**Query params:**
- `?status=pending` — only pending messages
- `?status=delivered` — only delivered messages
- `?status=read` — only read messages
- `?status=deleted` — only deleted messages
- (no param) — all non-deleted messages

**Response:**
```json
{ "ok": true, "messages": [ ... ], "count": 5 }
```

Each message includes a `_status` field.

---

### GET /peek/{address}

Check how many **pending** messages are waiting without consuming them.

**Headers:** `X-Flux-Token: <token>`

**Response:**
```json
{ "ok": true, "count": 5 }
```

---

### POST /read

Mark a message as read. Only the recipient may call this.

**Headers:** `X-Flux-Token: <token>`

**Request body:**
```json
{ "id": "<message_id>", "address": "<recipient_flux_address>" }
```

**Response:**
```json
{ "ok": true, "read": true }
```

---

### POST /delete

Soft-delete a message. Only the recipient may call this.
The message is retained on the server permanently with status `deleted`.
It will no longer appear in the default inbox view.

**Headers:** `X-Flux-Token: <token>`

**Request body:**
```json
{ "id": "<message_id>", "address": "<recipient_flux_address>" }
```

**Response:**
```json
{ "ok": true, "deleted": true }
```

---

### GET /status/{address}

Check whether an address currently has an active WebSocket connection.

**Response:**
```json
{ "ok": true, "address": "fx1...", "online": true }
```

---

### GET /stats

Server-wide statistics.

**Response:**
```json
{
  "ok": true,
  "backend": "sqlite",
  "total": 42,
  "pending": 3,
  "delivered": 5,
  "read": 31,
  "deleted": 3,
  "addresses": 4,
  "online_addresses": 1,
  "ws_connections": 2
}
```

---

### GET /health

Liveness check. Returns `200` if the server is running.

---

## WebSocket Transport

Connect to `ws://<host>:<port>/ws`. All frames are JSON text.

### Authentication

The first frame sent by the client must be an auth frame:

```json
{ "action": "auth", "address": "fx1...", "token": "<token>" }
```

On success, the server drains any pending messages (marking them `delivered`) and responds with the full inbox:

```json
{
  "ok": true,
  "action": "authed",
  "address": "fx1...",
  "messages": [ ... ]
}
```

Each message in `messages` includes a `_status` field.

---

### Sending via WebSocket

```json
{ "action": "send", "msg": { <full message envelope> } }
```

Response:
```json
{ "ok": true, "id": "<msg_id>", "delivery": "realtime" }
```

---

### Receiving messages

Inbound messages are pushed as:

```json
{ "type": "msg", "msg": { <full message envelope> } }
```

---

### Marking a message as read via WebSocket

```json
{ "action": "read", "id": "<msg_id>" }
```

Response:
```json
{ "ok": true, "read": true }
```

The address is taken from the authenticated session — only the connected user's messages can be marked.

---

### Soft-deleting a message via WebSocket

```json
{ "action": "delete", "id": "<msg_id>" }
```

Response:
```json
{ "ok": true, "deleted": true }
```

---

### Ping / Pong

```json
{ "action": "ping" }
```
```json
{ "ok": true, "action": "pong", "t": 1741443200000 }
```

---

## Authentication Token

Fetch, peek, inbox, read, and delete operations require an `X-Flux-Token` header. The token is derived as:

```
token = SHA-256("flux:" + address + ":" + FLUX_SECRET)
```

`FLUX_SECRET` is a shared secret set on the server via environment variable. Clients who know their own private key and the server secret can compute their token locally — no login round-trip needed.

> **Production note:** Replace this with a challenge-response scheme (sign a server-issued nonce with your private key) for deployments where the server secret cannot be shared with clients.

---

## Full Message Lifecycle Diagram

```
Client A                   FLUX Node                  Client B
   │                           │                           │
   │── POST /send ────────────▶│                           │
   │                           │── WebSocket push ────────▶│  (if online)
   │◀── { delivery: realtime } │                           │
   │                           │                           │
   │                           │  (B offline: pending)     │
   │                           │                           │
   │                           │◀── WS connect + auth ─────│
   │                           │─── inbox flush ──────────▶│  (pending→delivered)
   │                           │                           │
   │                           │◀── { action: read } ──────│  (delivered→read)
   │                           │◀── { action: delete } ────│  (read→deleted, retained)
```

---

## Limits

| Parameter              | Default    |
|------------------------|------------|
| Max content size       | 65,536 B   |
| Max queued per address | 500        |
| Clock skew tolerance   | 5 minutes  |
| WS heartbeat interval  | 20 seconds |

All limits are configurable in `flux/constants.py`.

---

## Extending FLUX

The protocol is intentionally minimal. Common extensions:

- **Encryption:** Encrypt `content` with the recipient's public key before sending. The node never sees plaintext.
- **Attachments:** Store blobs out-of-band, put the URL or hash in `content`.
- **Threading:** Use the `re` field to chain messages into threads.
- **Federation:** Nodes can forward messages to other nodes based on address prefix routing.
- **Groups:** A group is just an address. The holder of the private key distributes to members.
- **Archiving:** Query `?status=deleted` to implement a trash/archive view.