# FLUX Protocol Specification

## Versions

| Thing | Value | Notes |
|---|---|---|
| Protocol (`v` field) | `1.0` | Wire format version. Only bumps when signing algorithm or required fields change. |
| Software / API | `2.1.1` | Semver. Returned by `/health` and `/federation/info`. |

### Changelog

#### 2.1.1
**Security improvements:**
- **HTTPS by default:** Federation now uses HTTPS for all server-to-server communication. HTTP is only used for localhost or explicit port specifications (development mode).
- **Tamper report validation:** Servers verify that tamper reports contain actual proof of tampering by checking the integrity chain. This prevents malicious actors from sending fabricated reports to frame innocent servers.
- **Enhanced quarantine broadcast:** Tamper reports are now sent to all servers in the message's route in addition to mesh peers, enabling faster network-wide detection of malicious relays.

#### 2.1.0
**Added:**
- `integrity_chain` envelope field — append-only list of per-hop SHA-256 records.
- `encrypted`, `content_enc`, `enc_recipients` envelope fields for E2E encryption.
- Tamper detection, TamperReport broadcast, and server quarantine system.
- Built-in spam detection (`flux/spam.py`).
- Endpoints: `POST /integrity/tamper_report`, `GET /integrity/reputation`, `POST /integrity/verify`.

#### 2.0.0
**Breaking:**
- Removed `POST /ack`. Messages are never deleted; use `POST /read` or `POST /delete`.
- WebSocket `ack` removed; replaced by `read` and `delete`.

**Added:**
- `subject`, `cc`, `bcc`, `tags`, `expires`, `route` envelope fields.
- Persistent inbox with status tracking.
- Multiple inboxes per address.
- Tags with reserved globals `important`, `favorited`.
- Route metadata, mesh system.
- CC/BCC fan-out with BCC stripping.

#### 1.0.0
Initial release.

---

## Core Design Principles

- **Identity is a keypair.** No usernames, no passwords. Address derived from public key.
- **Every message is signed.** Spoofing is cryptographically impossible.
- **Every hop is hashed.** Tampering by intermediate servers is detectable.
- **Tamper = quarantine.** Servers that tamper with messages are automatically ejected.
- **Messages are never destroyed.** Deletion is a status change.
- **Content is content.** Plain UTF-8. No MIME types.
- **Encryption is first-class.** E2E encryption is built in, not bolted on.
- **Federation over HTTPS.** Server-to-server communication uses HTTPS by default.

---

## Addresses

```
address = "fx1" + SHA-256(public_key_bytes)[0:40 hex chars]
```

---

## Message Envelope

### Sender fields (always present)

| Field          | Type        | Required | Description |
|----------------|-------------|----------|-------------|
| `v`            | string      | yes      | Protocol version. Must be `"1.0"` |
| `id`           | string      | yes      | UUID hex, no hyphens |
| `from`         | string      | yes      | Sender FLUX address |
| `to`           | string      | yes      | Recipient FLUX address |
| `t`            | integer     | yes      | Unix timestamp in milliseconds |
| `content`      | string      | yes      | Message body. Plain UTF-8, max 65,536 bytes. Set to `"[encrypted]"` when E2E encrypted. |
| `sig`          | string      | yes      | Ed25519 signature (base64url) |
| `pub`          | string      | yes      | Sender public key (base64url) |
| `subject`      | string      | no       | Human-readable subject line |
| `re`           | string      | no       | Message ID this replies to |
| `cc`           | string[]    | no       | CC recipient FLUX addresses |
| `bcc`          | string[]    | no       | BCC recipients — stripped before delivery |
| `tags`         | string[]    | no       | Lowercase tags. Reserved: `important`, `favorited` |
| `expires`      | integer\|0  | no       | Soft-delete after first read if present. `0` = expire on any read. |

### Server-appended fields (excluded from sender signature)

| Field             | Type      | Description |
|-------------------|-----------|-------------|
| `route`           | object[]  | `[{server, t}, …]` — each server appends itself on receipt |
| `integrity_chain` | object[]  | `[HopRecord, …]` — see below |

### E2E encryption fields (set by sender)

| Field            | Type              | Description |
|------------------|-------------------|-------------|
| `encrypted`      | bool              | `true` when content is E2E encrypted |
| `content_enc`    | string            | base64url(nonce + ciphertext + GCM tag) |
| `enc_recipients` | dict[fx1, string] | Per-recipient encrypted CEK blob (base64url) |

### Server-added retrieval metadata (never signed)

| Field     | Type     | Description |
|-----------|----------|-------------|
| `_status` | string   | `pending`, `delivered`, `read`, `deleted` |
| `_inbox`  | string   | Inbox this message lives in |
| `_tags`   | string[] | Current tags (may differ from envelope if modified server-side) |

---

## Signing

Excluded from signature: `sig`, `pub`, `route`, `integrity_chain`, `_status`, `_inbox`, `_tags`.

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

## Integrity Chain

### HopRecord

Each server that receives a message appends a HopRecord to `integrity_chain`:

| Field    | Type   | Description |
|----------|--------|-------------|
| `server` | string | Server domain |
| `t`      | int    | Unix ms when message was received |
| `hash`   | string | SHA-256(canonical_payload) hex |
| `sig`    | string | Ed25519 signature of `"server:t:hash"` (base64url) |
| `pub`    | string | Server's Ed25519 public key (base64url) |

### Canonical payload for hashing

Same as sender signing payload: all fields excluding `sig`, `pub`, `route`, `integrity_chain`, server metadata.

### Chain verification

To verify the chain:

1. Compute the **baseline hash** from the message with an empty `integrity_chain`.
2. For each HopRecord in order:
   a. Verify the hop's `sig` against `server:t:hash` using the hop's `pub`.
   b. Confirm `hash` equals the baseline hash.
   c. The baseline hash does not change between hops — any alteration of the protected fields would produce a different hash.
3. If any hop fails either check, identify the offending server and emit a TamperReport.

### TamperReport

When tampering is detected, a signed report is broadcast to:
- All known mesh peers
- All servers that appear in the message's route
- **Excludes the offending server itself**

```json
{
  "type": "tamper_report",
  "msg_id": "<id>",
  "offender": "relay-x.example.com",
  "reporter": "server-a.example.com",
  "t": 1741443200000,
  "sig": "<Ed25519 sig of tamper:msg_id:offender:reporter:t>",
  "pub": "<reporter's server pub key>",
  "integrity_chain": [...],
  "msg_hash_baseline": "<hex>"
}
```

Each server that receives a TamperReport:
1. **Validates the report** by verifying the integrity chain contains proof of actual tampering by the claimed offender.
2. Verifies the report's signature.
3. Records a strike against the offender.
4. Quarantines the offender when strikes reach `TRUST_THRESHOLD` (default 3).
5. Rejects any future message that passed through a quarantined server.

**Anti-fraud protection:** The validation step prevents malicious actors from sending fabricated reports to frame innocent servers. A server will only record a strike if the integrity chain proves the offender actually tampered with the message.

---

## End-to-End Encryption

### Encryption scheme

```
CEK  = random 256-bit key
nonce = random 96-bit nonce
content_enc = nonce ‖ AES-256-GCM(CEK, content)

For each recipient (fx1_addr, ed25519_pub):
  x25519_pub    = birational_map(ed25519_pub)   // Ed25519 → X25519
  eph_priv      = random X25519 private key
  eph_pub       = eph_priv.public_key()
  shared        = X25519(eph_priv, x25519_pub)
  wrap_key      = SHA-256(shared)
  enc_cek_blob  = eph_pub ‖ nonce_w ‖ AES-256-GCM(wrap_key, CEK)
  enc_recipients[fx1_addr] = base64url(enc_cek_blob)
```

### Decryption

```
enc_cek_blob  = base64url_decode(enc_recipients[my_fx1_addr])
eph_pub_bytes = enc_cek_blob[:32]
wrapped_cek   = enc_cek_blob[32:]
shared        = X25519(my_x25519_priv, eph_pub_bytes)
wrap_key      = SHA-256(shared)
CEK           = AES-256-GCM-decrypt(wrap_key, wrapped_cek)
content       = AES-256-GCM-decrypt(CEK, base64url_decode(content_enc))
```

The server never has access to `CEK` or the private keys — decryption is always done client-side.

---

## Spam Detection

Before a message is stored, the server runs heuristic spam checks:

| Check | Description |
|---|---|
| Rate limit | Max 20 messages/minute per sender address |
| Empty content | Reject if content is blank |
| Repetition | Reject if >85% of content is a single character |
| URL density | Reject if >5 URLs per 200 characters |
| Keyword score | Weighted bag-of-words; reject if score ≥ 4 |
| Subject abuse | All-caps or excessive punctuation in subject |

Spam is rejected with HTTP 451 before storage.

---

## Message Lifecycle

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
      │             if `expires` set → goes to deleted
      ▼  POST /delete  or  WS { action: "delete" }
  deleted        — soft-deleted; hidden; permanently retained
```

---

## CC / BCC

- **CC**: recipient gets a copy with the full `cc` list intact.
- **BCC**: each BCC recipient gets a separate individually addressed copy. The `bcc` field is stripped entirely before delivery.

---

## HTTP Endpoints

All responses: `{"ok": true, …}` on success, `{"ok": false, "error": "…"}` on failure.

### POST /send
Submit a signed message. Runs spam check, verifies integrity chain, appends hop record, appends route.

Response: `{ "ok": true, "delivery": "realtime"|"queued", "mesh": {…} }`

---

### GET /fetch/{address}
Drain pending → `delivered`. Supports `?inbox=`.
Headers: `X-Flux-Token`

---

### GET /inbox/{address}
Return all non-deleted messages. Supports `?inbox=`, `?status=`, `?tag=`.
Headers: `X-Flux-Token`

---

### GET /inboxes/{address}
List all inbox names.
Headers: `X-Flux-Token`

---

### GET /peek/{address}
Count pending messages.
Headers: `X-Flux-Token`

---

### POST /read
Mark as read. If `expires` is set, soft-deletes instead.
```json
{ "id": "<msg_id>", "address": "<fx1_address>" }
```
Headers: `X-Flux-Token`

---

### POST /delete
Soft-delete.
```json
{ "id": "<msg_id>", "address": "<fx1_address>" }
```
Headers: `X-Flux-Token`

---

### POST /tag
Add or remove a tag.
```json
{ "id": "<msg_id>", "address": "<fx1_address>", "tag": "important", "action": "add"|"remove" }
```
Headers: `X-Flux-Token`

---

### POST /move
Move to a different inbox.
```json
{ "id": "<msg_id>", "address": "<fx1_address>", "inbox": "archive" }
```
Headers: `X-Flux-Token`

---

### GET /status/{address}
Check if address has an active WebSocket connection.

---

### GET /stats
Server-wide statistics.

---

### GET /health
```json
{ "ok": true, "protocol": "1.0", "version": "2.1.1" }
```

---

### POST /integrity/tamper_report
Receive a tamper report from a peer. Validates the integrity chain to confirm actual tampering, verifies signature, records strike, quarantines if threshold reached.

---

### GET /integrity/reputation
```json
{
  "ok": true,
  "strikes": {"relay-x.example.com": 3},
  "quarantined": ["relay-x.example.com"]
}
```

---

### POST /integrity/verify
Verify a message's sender signature and full integrity chain.
```json
{
  "ok": true,
  "signature_valid": true,
  "integrity_chain_valid": true,
  "offending_server": null,
  "hop_count": 2
}
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

### Actions

| Action   | Extra fields | Description |
|----------|-------------|-------------|
| `send`   | `msg: {…}`  | Send a message (spam + integrity checked) |
| `read`   | `id`        | Mark as read |
| `delete` | `id`        | Soft-delete |
| `tag`    | `id`, `tag`, `tag_action: "add"\|"remove"` | Tag a message |
| `move`   | `id`, `inbox` | Move to inbox |
| `ping`   | —           | Heartbeat |

---

## Authentication Token

```
token = SHA-256("flux:" + address + ":" + FLUX_SECRET)
```

---

## Mesh System

See `mesh.config.example.json` and the main README for full details. Tamper reports are automatically forwarded to mesh peers and all servers in the message's route.

---

## Limits

| Parameter              | Default    |
|------------------------|------------|
| Max content size       | 65,536 B   |
| Max queued per address | 500        |
| Clock skew tolerance   | 5 minutes  |
| WS heartbeat interval  | 20 seconds |
| Spam rate limit        | 20 msg/min |
| Tamper strikes → quarantine | 3     |

All configurable in `flux/constants.py`.