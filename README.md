# FLUX

**Fast Lightweight Unified eXchange** — a minimal, cryptographically authenticated messaging protocol.

No usernames. No passwords. No MIME types. Your identity is a keypair. Your address is derived from your public key. Every message is signed. Content is just content.

**Current version: 2.0.0** — see [CHANGELOG](#changelog) below.

---

## Why FLUX?

SMTP was designed in 1982. It carries decades of legacy: plaintext auth, header bloat, MIME complexity, no built-in signature verification, and a trust model that relies on DNS and reputation rather than cryptography.

FLUX is different:

| | SMTP | FLUX |
|---|---|---|
| Identity | username@domain | cryptographic keypair |
| Auth | password | Ed25519 signature on every message |
| Spoofing | trivially possible | cryptographically impossible |
| Message format | RFC 5322 headers + MIME | clean JSON, content is a string |
| Transport | TCP text protocol | HTTP + WebSocket |
| Real-time delivery | no | yes, via WebSocket push |
| Storage | server-dependent | persistent, never deleted |
| Dependencies | complex MTA stack | ~300 lines of Python |

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
# Copy and edit config
cp .env.example .env

# Optional: set up mesh networking
cp mesh.config.example.json mesh.config.json

# Build and run
docker compose up -d
docker compose logs flux
```

---

## Configuration

All server settings via environment variables:

| Variable | Default | Description |
|---|---|---|
| `FLUX_SECRET` | `insecure-default-change-me` | Token derivation secret — **change this** |
| `FLUX_BACKEND` | `memory` | `memory` or `sqlite` |
| `FLUX_DB_PATH` | `flux.db` | SQLite message store path |
| `FLUX_ACCOUNTS_DB` | `flux_accounts.db` | SQLite accounts store path |
| `FLUX_PORT` | `8765` | Listen port |
| `FLUX_HOST` | `0.0.0.0` | Bind address |
| `FLUX_DOMAIN` | — | Domain for federated addressing. **Required** for account system. |

Copy `.env.example` to `.env` and edit before deploying.

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

See `mesh.config.example.json` and [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for full details.

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
2. Implement `enqueue`, `drain`, `peek_count`, `list_messages`, `mark_read`, `delete_message`, `add_tag`, `remove_tag`, `move_inbox`, `list_inboxes`, `stats`
3. Register it in the `create_store` factory

---

## Protocol

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the full specification.

---

## Versioning

FLUX uses [Semantic Versioning 2.0.0](https://semver.org/).

- **MAJOR** — breaking changes to the API or wire protocol
- **MINOR** — backwards-compatible new features
- **PATCH** — backwards-compatible bug fixes

The `v` field in message envelopes (`"1.0"`) is the **wire protocol version** and is versioned independently from the software. It only changes when the signing algorithm or the set of required envelope fields changes.

---

## Changelog

### 2.0.0
**Breaking changes:**
- Removed `POST /ack`. Messages are never deleted server-side. Use `POST /read` or `POST /delete`.
- WebSocket `ack` action removed. Use `read` or `delete` actions instead.

**New features:**
- Persistent inbox — messages survive across fetches with status tracking (`pending` → `delivered` → `read` → `deleted`).
- `subject`, `cc`, `bcc`, `tags`, `expires`, `route` envelope fields.
- Multiple inboxes per address.
- Tags system with reserved globals `important` and `favorited`.
- `expires` property — message soft-deletes after first read.
- Route metadata — transparent hop tracking through servers.
- New endpoints: `/inbox`, `/inboxes`, `/read`, `/delete`, `/tag`, `/move`.
- Account routes: `/mail/read`, `/mail/delete`, `/mail/tag`, `/mail/move`, `/mail/inboxes`.
- Mesh networking system with `broadcast`, `chain`, and `hybrid` delivery modes.
- CC/BCC fan-out with BCC stripping.

### 1.0.0
Initial release.

---

## License

MIT — see [LICENSE](LICENSE).