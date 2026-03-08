# FLUX

**Fast Lightweight Unified eXchange** — a cryptographically secure, federated email protocol built for the modern era.

No usernames. No passwords. No MIME types. No trust-by-reputation DNS games. Your identity is a keypair. Your address is derived from your public key. Every message is signed. Every hop is hashed. Tampering is automatically detected, reported, and punished.

**Current version: 2.1.1** — see [CHANGELOG](#changelog) below.

---

## Why FLUX?

SMTP was designed in 1982. It carries forty years of legacy: plaintext auth, header bloat, MIME complexity, no built-in signature verification, and a trust model that relies on DNS reputation rather than cryptography. Anyone can forge a from-address. Any server along the route can silently alter your message. Spam filters are bolted on after the fact.

FLUX is designed from scratch as an email protocol with security as a first principle:

| | SMTP | FLUX |
|---|---|---|
| Identity | username@domain | Ed25519 cryptographic keypair |
| Auth | password | Signature on every message |
| Spoofing | trivially possible | cryptographically impossible |
| Message format | RFC 5322 headers + MIME | clean JSON, content is a string |
| Transport | TCP text protocol | HTTP + WebSocket |
| Real-time delivery | no | yes, via WebSocket push |
| Storage | server-dependent | persistent, never deleted |
| Tamper detection | none | per-hop SHA-256 integrity chain |
| Tamper response | none | automatic quarantine propagation |
| Encryption | optional (S/MIME, PGP) | built-in E2E (X25519 + AES-256-GCM) |
| Spam detection | external tools | built-in heuristic filter |
| Dependencies | complex MTA stack | ~500 lines of Python |

---

## Quick Start

```bash
pip install -r requirements.txt

# Generate an identity
python main.py keygen --save alice.json

# Start a server
python main.py server --backend sqlite --db flux.db --domain localhost:8765

# Run the built-in demo
python main.py demo
```

---

## Docker

```bash
cp .env.example .env
# edit .env

docker compose up -d
docker compose logs flux
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `FLUX_SECRET` | `insecure-default-change-me` | Token derivation secret — **change this** |
| `FLUX_BACKEND` | `memory` | `memory` or `sqlite` |
| `FLUX_DB_PATH` | `flux.db` | SQLite message store path |
| `FLUX_ACCOUNTS_DB` | `flux_accounts.db` | SQLite accounts store path |
| `FLUX_PORT` | `8765` | Listen port |
| `FLUX_HOST` | `0.0.0.0` | Bind address |
| `FLUX_DOMAIN` | — | Domain for federated addressing. **Required** for account system. |

### Security Best Practices

**FLUX_SECRET:**
- Generate a cryptographically random secret (64+ characters)
- Use `openssl rand -hex 32` or similar
- Never commit to version control
- Each server operator sets their own secret (federated systems)
- Rotate immediately if compromise suspected
- See [FLUX_SECRET Rotation Guide](#flux_secret-rotation) below

---

## Security Architecture

### 1 — Identity & Signatures

Every FLUX address is derived from an Ed25519 public key:

```
address = "fx1" + SHA-256(public_key_bytes)[0:40 hex chars]
```

Every message is signed by the sender's private key before transmission. Servers verify the signature on receipt. Spoofing is cryptographically impossible — you cannot send as an address you don't hold the private key for.

---

### 2 — Per-Hop Integrity Chain

Every server that handles a message appends a **HopRecord** to the message's `integrity_chain`:

```json
{
  "server": "relay-a.example.com",
  "t": 1741443200000,
  "hash": "e3b0c44298fc1c14...",
  "sig": "base64url...",
  "pub": "base64url..."
}
```

Each HopRecord contains:
- `hash` — SHA-256 of the message's canonical payload *as received* by that server
- `sig` — the server's Ed25519 signature over `server:t:hash`
- `pub` — the server's Ed25519 public key

**What this proves:** If any server in the chain alters the message content, subject, recipients, or any other protected field, the hash at that hop will not match the baseline hash computed from the sender's signed payload. The mismatch is detectable by any downstream server or the final recipient.

The integrity chain is **excluded from the sender's signature** (so servers can append to it without invalidating the original signature), but each hop's record is independently signed by that server's own key.

---

### 3 — Tamper Detection & Automatic Quarantine

When a server or recipient detects a hash mismatch in the integrity chain, it:

1. **Identifies the offending server** — the hop whose hash doesn't match.
2. **Records a strike** against that server locally.
3. **Broadcasts a signed TamperReport** to:
   - All known mesh peers
   - All servers that appear in the message's route
   - **Excludes the offending server itself**

```
Server A detects tampering by Relay-X
  → strikes Relay-X locally
  → broadcasts TamperReport to:
     • Mesh peers (Server B, Server C, Server D)
     • All servers in message route
     (NOT to Relay-X)
  → Recipients verify the report contains proof of tampering
  → Each server strikes Relay-X independently
```

When a server accumulates **3 strikes** (configurable via `TRUST_THRESHOLD`), it is **quarantined**: all future messages that passed through it are rejected. No administrator action required — the network heals itself.

**Anti-Fraud Protection:** Tamper reports are validated before acceptance. Each server independently verifies the integrity chain in the report to confirm actual tampering occurred. This prevents malicious actors from sending fabricated reports to frame innocent servers.

This creates a strong economic incentive: any server that tampers with messages will be ejected from the network automatically as reports propagate.

#### Integrity endpoints

| Method | Route | Description |
|---|---|---|
| POST | `/integrity/tamper_report` | Receive a tamper report from a peer (validated before acceptance) |
| GET | `/integrity/reputation` | View current strikes and quarantine list |
| POST | `/integrity/verify` | Verify a message's signature + integrity chain |

---

### 4 — End-to-End Encryption

Encryption is opt-in. When enabled, the server **never sees plaintext** — it only stores and routes ciphertext. This is true even for the sender's own server.

**Scheme:** X25519 ECDH key agreement + AES-256-GCM content encryption.

FLUX reuses the existing Ed25519 identity keypair for encryption via the standard birational map to X25519 — no separate encryption key needed.

```
Sender:
  1. Generate random 256-bit content encryption key (CEK)
  2. Encrypt content with AES-256-GCM(CEK) → ciphertext
  3. For each recipient:
       shared_secret = X25519(ephemeral_priv, recipient_x25519_pub)
       wrap_key = SHA-256(shared_secret)
       encrypted_CEK = AES-256-GCM(wrap_key, CEK)
  4. Wire: content="[encrypted]", content_enc=ciphertext,
           enc_recipients={fx1addr: encrypted_CEK_blob}

Recipient:
  1. shared_secret = X25519(my_x25519_priv, ephemeral_pub)
  2. wrap_key = SHA-256(shared_secret)
  3. CEK = AES-256-GCM-decrypt(wrap_key, encrypted_CEK)
  4. plaintext = AES-256-GCM-decrypt(CEK, ciphertext)
```

To encrypt a message in your code:

```python
from flux import FluxIdentity, build_message
from flux.encryption import encrypt_message, decrypt_message

alice = FluxIdentity.generate()
bob = FluxIdentity.generate()

msg = build_message(alice, bob.address, "Secret content")
encrypted_msg = encrypt_message(msg, {
    bob.address: bob.pub_b64(),
})
# encrypted_msg["content"] == "[encrypted]"
# encrypted_msg["content_enc"] == "<ciphertext>"

# Bob decrypts:
plaintext = decrypt_message(encrypted_msg, bob)
```

---

### 5 — Spam Detection

A built-in heuristic spam filter runs on every incoming message before it is stored or delivered. It checks:

- **Rate limiting** — max 20 messages/minute per sender address (configurable)
- **Content quality** — empty content, excessive character repetition
- **URL density** — unusually high link-to-text ratio
- **Keyword scoring** — weighted bag-of-words against common spam patterns
- **Subject line abuse** — all-caps, excessive punctuation

Spam is rejected with HTTP 451 before storage. No message is ever delivered to the recipient's inbox.

**Note:** Server operators can replace the built-in spam filter with custom implementations (Bayesian, ML-based, etc.) as needed.

---

### 6 — Federation Security (HTTPS by Default)

FLUX uses **HTTPS by default** for all federated server-to-server communication. HTTP is only used for localhost or when a port is explicitly specified (development mode).

```python
# Production: alice@server-a.com → bob@server-b.com
# Uses: https://server-b.com/federation/resolve/bob

# Development: alice@localhost:8765 → bob@localhost:8766
# Uses: http://localhost:8766/federation/resolve/bob
```

This protects against man-in-the-middle attacks during address resolution and message delivery between federated servers.

---

## Using as a Library

```python
import asyncio
from flux import FluxIdentity, FluxClient

async def main():
    alice = FluxIdentity.generate()
    client = FluxClient(alice, "http://localhost:8765")

    # HTTP send
    result = await client.send("fx1bob...", "Hello Bob")

    # WebSocket — real-time
    @client.on_message
    async def on_msg(msg):
        print(f"Got: {msg['content']}")

    await client.connect_ws()

asyncio.run(main())
```

---

## Message Features

- **Subject** — human-readable subject line
- **CC / BCC** — fan-out delivery; BCC stripped before delivery to other recipients
- **Tags** — custom tags per message; two global reserved tags: `important`, `favorited`
- **Multiple inboxes** — move messages between named inboxes (`inbox`, `archive`, `work`, etc.)
- **Persistent storage** — messages are never deleted; status tracks lifecycle (`pending` → `delivered` → `read` → `deleted`)
- **Expires** — set `expires: 0` on a message to have it soft-delete after first read
- **Reply threading** — use the `re` field to chain messages
- **Route metadata** — every server a message passes through appends itself to `route`
- **Integrity chain** — every server appends a signed SHA-256 hop record
- **End-to-end encryption** — optional, transparent to servers
- **Spam filtering** — built-in, runs before storage

---

## Mesh Networking

Connect multiple FLUX servers into a unified delivery network. Configure `mesh.config.json`:

```json
{
  "meshes": {
    "main": {
      "token": "shared-secret",
      "mode": "broadcast",
      "peers": ["http://server-a:8765", "http://server-b:8765"]
    }
  }
}
```

Three delivery modes: `broadcast` (all peers), `chain` (first success), `hybrid` (route to recipient's online server, fall back to broadcast).

Tamper reports are automatically broadcast through the mesh peer list AND to all servers that appear in the tampered message's route.

---

## Project Structure

```
flux-protocol/
├── flux/
│   ├── __init__.py          # public API exports
│   ├── constants.py         # tuneable values + version constants
│   ├── crypto.py            # Ed25519 primitives
│   ├── identity.py          # keypair management
│   ├── message.py           # envelope build + verify
│   ├── store.py             # MemoryStore + SQLiteStore (multi-inbox, tags)
│   ├── presence.py          # WebSocket connection registry
│   ├── auth.py              # token derivation
│   ├── routes.py            # raw FLUX HTTP handlers
│   ├── ws.py                # WebSocket handler
│   ├── server.py            # app factory + run
│   ├── account_routes.py    # federated account system routes
│   ├── accounts.py          # account + session store
│   ├── federation.py        # address resolution
│   ├── mesh.py              # mesh relay system
│   ├── integrity.py         # per-hop SHA-256 chain + tamper detection + reputation
│   ├── encryption.py        # E2E encryption (X25519 ECDH + AES-256-GCM)
│   ├── spam.py              # built-in spam detection
│   └── oauth.py             # OAuth hook interface
├── docs/
│   ├── PROTOCOL.md          # full protocol specification
│   └── FEDERATION.md        # federation guide
├── main.py                  # CLI entrypoint
├── test_client.py           # interactive test client
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── mesh.config.example.json
├── .env.example
└── LICENSE
```

---

## Swapping the Storage Backend

The store is fully pluggable via `flux/store.py`. To add a new backend (Redis, Postgres, etc.):

1. Subclass `BaseStore`
2. Implement all abstract methods
3. Register it in the `create_store` factory

---

## FLUX_SECRET Rotation

### When to Rotate

**Rotate immediately if:**
- You suspect the secret has been compromised
- The secret was accidentally committed to version control
- A developer/operator with access to the secret leaves your organization

**Optional periodic rotation:**
- Some security policies require credential rotation every 3-5 years
- This is a per-server operator decision in federated deployments

### How to Rotate

1. **Generate new secret:**
   ```bash
   openssl rand -hex 32
   ```

2. **Update server configuration:**
   ```bash
   # Update .env or environment variable
   FLUX_SECRET=<new-secret>
   ```

3. **Restart server:**
   ```bash
   docker compose restart flux
   # or
   systemctl restart flux
   ```

4. **For raw FLUX protocol clients (non-account-based):**
   - Clients must update their FLUX_SECRET environment variable
   - This causes temporary authentication downtime

5. **Account-based users are unaffected:**
   - Session tokens are stored in database, not derived from FLUX_SECRET
   - No action required from account-based users

### Impact Assessment

- **Account system users:** No impact (sessions stored in DB)
- **Raw FLUX protocol users:** Must update FLUX_SECRET on client side
- **Other servers in federation:** No impact (each server has independent secret)
- **Downtime:** Brief window during rotation for raw protocol clients

---

## Protocol

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the full specification.

---

## Versioning

FLUX uses [Semantic Versioning 2.0.0](https://semver.org/).

- **MAJOR** — breaking changes to the API or wire protocol
- **MINOR** — backwards-compatible new features
- **PATCH** — backwards-compatible bug fixes

The `v` field in message envelopes (`"1.0"`) is the **wire protocol version**, versioned independently from the software.

---

## Changelog

### 2.1.1
**Security improvements:**
- **HTTPS by default** — federation now uses HTTPS for all server-to-server communication. HTTP is only used for localhost or explicit port specifications (development mode).
- **Tamper report validation** — servers now verify that tamper reports contain actual proof of tampering by checking the integrity chain. This prevents malicious actors from sending fabricated reports to frame innocent servers.
- **Enhanced quarantine broadcast** — tamper reports are now sent to all servers in the message's route in addition to mesh peers, enabling faster network-wide detection of malicious relays.

### 2.1.0
**New features:**
- **Per-hop integrity chain** — every server appends a signed SHA-256 record to `integrity_chain`. Any downstream server or recipient can detect tampering by any intermediate server.
- **Automatic tamper quarantine** — when tampering is detected, a signed TamperReport is broadcast to all mesh peers (excluding the offender). Servers accumulating 3 reports are quarantined and their relayed messages rejected.
- **Built-in E2E encryption** — `flux.encryption` provides `encrypt_message` / `decrypt_message` using X25519 ECDH + AES-256-GCM. Servers never see plaintext when encryption is used.
- **Built-in spam detection** — `flux.spam` runs heuristic checks (rate limit, repetition, URL density, keyword scoring) before any message is stored.
- New endpoints: `POST /integrity/tamper_report`, `GET /integrity/reputation`, `POST /integrity/verify`.

### 2.0.0
**Breaking changes:**
- Removed `POST /ack`. Messages are never deleted server-side. Use `POST /read` or `POST /delete`.
- WebSocket `ack` action removed. Use `read` or `delete` actions instead.

**New features:**
- Persistent inbox with status tracking (`pending` → `delivered` → `read` → `deleted`).
- `subject`, `cc`, `bcc`, `tags`, `expires`, `route` envelope fields.
- Multiple inboxes per address.
- Tags system with reserved globals `important` and `favorited`.
- Route metadata — transparent hop tracking through servers.
- Mesh networking system with `broadcast`, `chain`, and `hybrid` delivery modes.
- CC/BCC fan-out with BCC stripping.

### 1.0.0
Initial release.

---

## Operating a FLUX Server

### Deployment Considerations

**Storage Management:**
- Messages are persistent (soft-delete only)
- Configure retention policies based on your storage capacity
- Monitor disk usage and implement archival strategies as needed

**Reputation System:**
- Reputation is local to each server (not shared across the network)
- Coordinate quarantine lists with trusted peers via out-of-band channels if desired
- Each server operator makes independent trust decisions

**Spam Protection:**
- Built-in heuristic filter is a baseline
- Consider implementing ML-based filtering for high-traffic deployments
- Server operators can replace the spam module with custom implementations

**Monitoring:**
- Check `/stats` endpoint for server health metrics
- Monitor `/integrity/reputation` for quarantined servers
- Track storage growth and plan capacity accordingly

---

## License

MIT — see [LICENSE](LICENSE).