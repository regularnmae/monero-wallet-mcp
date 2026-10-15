# deploy/ — what this repo owns, and what it depends on

This repo owns exactly **one** unit — `monero-wallet-mcp.container` — plus a
shared network file:

- `monero-wallet-mcp.container` — the MCP server, `PublishPort=8103:8000`.
- `npm-network.network` — the podman network it shares with the daemons.
  `install.sh` links it **only if no other repo has already provided it**.

It does **not** own the wallet RPC. The `monero-wallet-rpc` unit is owned by
[`crypto-nodes`](https://github.com/regularnmae/crypto-nodes), together with the
rest of the crypto node stack — one file, one owner.

## Upstream contract

The server assumes a monero-wallet-rpc reachable on the podman network:

| | |
| --- | --- |
| Container / address | `monero-wallet-rpc:18083` (JSON-RPC) |
| Network | `npm-network` (also published on the host's `127.0.0.1:18083`) |
| Auth | `--rpc-login`, **HTTP digest** |

**Authentication is the thing to get right.** monero-wallet-rpc rejects a plain
Basic header with 401, and its digest nonce is bound to the **TCP connection**,
so the challenge and the authenticated retry must share one socket (a fresh
connection answers `stale=true`). Python's `urllib` digest handler does not
interoperate with monero's server. `rpc.py` therefore computes the digest
itself and does challenge + retry on a single `http.client` connection per
call — see the module docstring and `tests/test_rpc.py`.

The wallet can move funds, so this repo also carries the spend guard rails
(`ENABLE_SPENDING`, caps, a second token); see the repo `README.md`.

## Deploy

```sh
./deploy/install.sh     # token.env + upstream.env + spend.env (ENABLE_SPENDING=0)
./deploy.sh
sudo firewall-cmd --permanent --add-port=8103/tcp && sudo firewall-cmd --reload
```

`install.sh` reads `--rpc-login` from the running `monero-wallet-rpc`
container. Spending stays **off** until you edit `~/.config/monero-wallet-mcp/spend.env`.
