# FLUX Federation

Federation lets users on different FLUX servers send messages to each other using human-readable addresses like `alice@server-a.com` → `bob@server-b.com`.

## How It Works

FLUX servers never relay message content between each other. Instead:

1. **Sender's server resolves the recipient's FLUX address** by asking the remote server.
2. **Message is sent directly** to the recipient's home server using the raw `fx1...` address.

```
alice@server-a.com  sends to  bob@server-b.com

Server A:
  GET https://server-b.com/federation/resolve/bob
  ← { "ok": true, "flux_address": "fx1bob...", "flux_pub_b64": "..." }

  POST https://server-b.com/send
  { signed message envelope with to: "fx1bob..." }
```

The federation resolve endpoint only returns the public key and address — never any credentials or private information.

## Endpoints

### `GET /federation/resolve/{username}`

Called by remote servers. Returns the public profile of a local user.

```json
{
  "ok": true,
  "username": "bob",
  "display_name": "Bob Smith",
  "flux_address": "fx1b3c9...",
  "flux_pub_b64": "base64url..."
}
```

Returns `404` if the user does not exist.

### `GET /federation/info`

Discovery endpoint. Returns metadata about this node.

```json
{
  "ok": true,
  "domain": "server-b.com",
  "version": "1.0",
  "federation": true
}
```

## Address Format

Federated addresses follow `username@domain` format. The domain must be resolvable and running a FLUX node.

- `alice@mail.mycompany.com`
- `bob@localhost:8765` (for local development)

Raw `fx1...` addresses always work too — they bypass the federation lookup entirely.

## Running Two Servers Locally

```bash
# Terminal 1 — Server A on port 8765
python main.py server --port 8765 --backend sqlite --domain localhost:8765

# Terminal 2 — Server B on port 8766
python main.py server --port 8766 --backend sqlite --domain localhost:8766

# Terminal 3 — Client for Server A
python test_client.py --server http://localhost:8765
# register → alice

# Terminal 4 — Client for Server B
python test_client.py --server http://localhost:8766
# register → bob

# Now alice can send to bob@localhost:8766
```

## Security

- The resolve endpoint is public and unauthenticated — it only exposes what a public directory would.
- Message signatures are always verified end-to-end. A malicious server cannot forge messages from its users to users on other servers.
- Address resolution results are cached for 5 minutes (`federation.CACHE_TTL`).

## OAuth Integration

FLUX does not implement OAuth itself. To add OAuth login to your node:

1. Handle the OAuth redirect and token exchange in your own code (any library/framework).
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
