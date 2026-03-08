# FLUX

**Fast Lightweight Unified eXchange** — a minimal, cryptographically authenticated messaging protocol.

No usernames. No passwords. No MIME types. No tags. Your identity is a keypair. Your address is derived from your public key. Every message is signed. Content is just content.

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
| Dependencies | complex MTA stack | ~200 lines of Python |

---

## Quick Start

```bash
pip install -r requirements.txt

# Generate an identity
python main.py keygen --save alice.json

# Start a server (SQLite, persistent)
python main.py server --backend sqlite --db flux.db

# Send a message
python main.py send --identity alice.json --to fx1<bob_address> --message "Hello"

# Fetch messages
python main.py fetch --identity alice.json

# Listen in real time
python main.py listen --identity alice.json

# Run the built-in demo
python main.py demo
```

---

## Docker

```bash
# Build and run
docker compose up -d

# Or manually
docker build -t flux .
docker run -p 8765:8765 -v flux_data:/data -e FLUX_SECRET=your-secret flux
```

---

## Configuration

All server settings are via environment variables:

| Variable | Default | Description |
|---|---|---|
| `FLUX_SECRET` | `insecure-default-change-me` | Token derivation secret — **change this** |
| `FLUX_BACKEND` | `memory` | `memory` or `sqlite` |
| `FLUX_DB_PATH` | `flux.db` | SQLite file path |
| `FLUX_PORT` | `8765` | Listen port |
| `FLUX_HOST` | `0.0.0.0` | Bind address |

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

## Project Structure

```
flux-protocol/
├── flux/
│   ├── __init__.py      # public API exports
│   ├── constants.py     # all tuneable values
│   ├── crypto.py        # Ed25519 primitives
│   ├── identity.py      # keypair management
│   ├── message.py       # envelope build + verify
│   ├── store.py         # MemoryStore + SQLiteStore
│   ├── presence.py      # WebSocket connection registry
│   ├── auth.py          # token derivation
│   ├── routes.py        # HTTP handlers
│   ├── ws.py            # WebSocket handler
│   └── server.py        # app factory + run
├── docs/
│   └── PROTOCOL.md      # full protocol specification
├── main.py              # CLI entrypoint
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── LICENSE
```

---

## Swapping the Storage Backend

The store is fully pluggable via `flux/store.py`. To add a new backend (Redis, Postgres, etc.):

1. Subclass `BaseStore`
2. Implement `enqueue`, `drain`, `peek_count`, `ack`, `stats`
3. Register it in the `create_store` factory

---

## Protocol

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the full specification, including message format, signing rules, WebSocket frame types, and extension points.

---

## License

MIT — see [LICENSE](LICENSE).
