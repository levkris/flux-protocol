# FLUX Protocol Specification — v1.0

FLUX (Fast Lightweight Unified eXchange) is a minimal, cryptographically authenticated messaging protocol. It is transport-agnostic, JSON-native, and designed to be simple enough that anyone can implement a client in an afternoon.

---

## Core Design Principles

- **Identity is a keypair, not a credential.** No usernames, no passwords, no registration. Your address is derived from your public key.
- **Every message is signed.** Spoofing an address is cryptographically impossible.
- **Content is content.** The body of a message is a plain UTF-8 string. No MIME types, no HTML, no embedded metadata.
- **Transport is pluggable.** HTTP and WebSocket are first-class. Both use the same message envelope.
- **Minimal envelope.** Only the fields that are strictly necessary exist.

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

### Signing

The signature is computed over the **core fields only** (`v`, `id`, `from`, `to`, `t`, `content`, and `re` if present). The `sig` and `pub` fields are excluded from the signed payload.

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

Retrieve and drain all pending messages for an address.

**Headers:** `X-Flux-Token: <token>`

**Response:**
```json
{ "ok": true, "messages": [ ... ], "count": 2 }
```

Draining is destructive — messages are marked delivered and will not appear again.

---

### GET /peek/{address}

Check how many messages are pending without consuming them.

**Headers:** `X-Flux-Token: <token>`

**Response:**
```json
{ "ok": true, "count": 5 }
```

---

### POST /ack

Acknowledge a delivered message. Removes it from the server permanently.

**Request body:**
```json
{ "id": "<message_id>" }
```

**Response:**
```json
{ "ok": true, "acked": true }
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
  "queued": 14,
  "addresses": 3,
  "delivered_unacked": 2,
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

On success, the server responds with:

```json
{
  "ok": true,
  "action": "authed",
  "address": "fx1...",
  "queued": [ ... ]
}
```

The `queued` array contains any messages that arrived while the client was offline, flushed immediately on connect.

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

### Acknowledging via WebSocket

```json
{ "action": "ack", "id": "<msg_id>" }
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

Fetch and peek operations require an `X-Flux-Token` header. The token is derived as:

```
token = SHA-256("flux:" + address + ":" + FLUX_SECRET)
```

`FLUX_SECRET` is a shared secret set on the server via environment variable. Clients who know their own private key and the server secret can compute their token locally — no login round-trip needed.

> **Production note:** Replace this with a challenge-response scheme (sign a server-issued nonce with your private key) for deployments where the server secret cannot be shared with clients.

---

## Message Lifecycle

```
Client A                   FLUX Node                  Client B
   │                           │                           │
   │── POST /send ────────────▶│                           │
   │                           │── WebSocket push ────────▶│  (if online)
   │◀── { delivery: realtime } │                           │
   │                           │                           │
   │                           │  (B offline: enqueued)    │
   │                           │                           │
   │                           │◀── WS connect + auth ─────│
   │                           │─── queued flush ─────────▶│
   │                           │◀── ack ────────────────────│
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
