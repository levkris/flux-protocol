# FLUX Federation

**Requires:** FLUX 2.0.0+ / API version 2.0.0

Federation lets users on different FLUX servers send messages to each other using human-readable addresses like `alice@server-a.com` → `bob@server-b.com`.

---

## How It Works

FLUX servers never relay message content between each other directly. Instead:

1. **Sender's server resolves the recipient's FLUX address** by querying the remote server's federation endpoint.
2. **Message is signed by the sender's identity** and delivered directly to the recipient's home server.

```
alice@server-a.com  sends to  bob@server-b.com

Server A:
  GET https://server-b.com/federation/resolve/bob
  ← { "ok": true, "flux_address": "fx1bob…", "flux_pub_b64": "…" }

  POST https://server-b.com/send
  { signed message envelope, to: "fx1bob…" }
```

The federation resolve endpoint only returns the public key and address — never credentials or private information.

---

## Endpoints

### `GET /federation/resolve/{username}`

Called by remote servers. Returns the public profile of a local user.

```json
{
  "ok": true,
  "username": "bob",
  "display_name": "Bob Smith",
  "flux_address": "fx1b3c9…",
  "flux_pub_b64": "base64url…"
}
```

Returns `404` if the user does not exist.

---

### `GET /federation/info`

Discovery endpoint. Returns metadata about this node.

```json
{
  "ok": true,
  "domain": "server-b.com",
  "version": "2.0.0",
  "protocol": "1.0",
  "federation": true
}
```

`version` is the software/API version (semver). `protocol` is the wire protocol version.

---

## Address Format

Federated addresses follow `username@domain` format:

- `alice@mail.mycompany.com`
- `bob@localhost:8765` (local development)

Raw `fx1…` addresses always work too — they bypass federation lookup entirely.

---

## Running Two Servers Locally

```bash
# Terminal 1 — Server A on port 8765
python main.py server --port 8765 --backend sqlite --domain localhost:8765

# Terminal 2 — Server B on port 8766
python main.py server --port 8766 --backend sqlite --domain localhost:8766

# Terminal 3 — register alice on Server A
python test_client.py --server http://localhost:8765
# > register

# Terminal 4 — register bob on Server B
python test_client.py --server http://localhost:8766
# > register

# Alice can now send to bob@localhost:8766
# Bob can send to alice@localhost:8765
```

---

## Security

- The resolve endpoint is public and unauthenticated — it only exposes what a public directory would.
- Message signatures are always verified end-to-end. A malicious server cannot forge messages from its users.
- Address resolution results are cached for 5 minutes (`federation.CACHE_TTL`).

---

## Relationship to Mesh

Federation and mesh solve different problems:

| | Federation | Mesh |
|---|---|---|
| Purpose | Route messages between independent servers | Replicate/relay messages across trusted servers |
| Trust model | Public (anyone can resolve) | Shared token (trusted peers only) |
| Message ownership | Sender signs, recipient is on remote server | Same message relayed to multiple stores |
| Config | Automatic via DNS/domain | Explicit `mesh.config.json` |

A server can use both simultaneously.

---

## OAuth Integration

FLUX does not implement OAuth itself. To add OAuth login to your node:

1. Handle the OAuth redirect and token exchange in your own code.
2. Verify the user with the provider and extract their stable user ID.
3. Call `flux.oauth.complete_oauth_login()`:

```python
from flux.oauth import complete_oauth_login

token, username = await complete_oauth_login(
    accounts=app["accounts"],
    provider="google",
    provider_uid="1234567890",
    suggested_username="alice",
    display_name="Alice Smith",
)
# Return `token` to the client as their X-Flux-Session header value
```

This creates the account on first login, or logs into the existing one on subsequent logins. Username collisions are resolved automatically.