# monero-wallet-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for a
Monero wallet (`monero-wallet-rpc`). It is **read-only by default**: spending
is off until an operator enables it, and even then it is capped, dry-run-first
and gated behind a second token.

## Read tools

| Tool | What it does |
| --- | --- |
| `get_balance` | Total, unlocked and locked balance |
| `get_address` | An account's primary address |
| `get_accounts` | Accounts (index, label, balance) |
| `get_height` | The wallet's synced height |
| `get_transfers` | Incoming / outgoing / pending / failed / pool transfers |
| `get_transfer_by_txid` | One transfer by txid |
| `get_payments` | Payments by payment id |
| `incoming_transfers` | Incoming transfers |

## Spend tools (only when `ENABLE_SPENDING=1`)

`transfer`, `sweep_all`, `create_address`. When spending is disabled these are
**not registered at all** — they do not appear in `tools/list` and cannot be
called.

## Safety model

This server can move real XMR on mainnet, so it is deliberately awkward to
weaponise:

- **Two tokens.** `MONERO_MCP_TOKEN` (read) admits the endpoint;
  `MONERO_MCP_SPEND_TOKEN` is *additionally* required for any spend tool. A
  client handed only the read token cannot move funds. With
  `ENABLE_SPENDING=1`, a spend token is mandatory or the server refuses to
  start.
- **Off by default.** `ENABLE_SPENDING` unset ⇒ spend tools absent.
- **Caps.** `MAX_SPEND_PER_CALL` and `MAX_SPEND_PER_DAY` (in XMR) are both
  required and positive when spending is on. The daily budget is tracked in
  process per UTC day and only committed spends count.
- **Dry-run first.** Every spend tool takes `dry_run: bool = True`. A dry run
  builds and signs the transaction (`do_not_relay`) but does not relay it, and
  does not consume the budget.
- **Optional allowlist.** `ALLOWED_DESTINATIONS` (comma-separated) restricts
  destinations; empty allows any.
- **Audit.** Every spend attempt logs one structured line (action, amount,
  destination, outcome).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MONERO_WALLET_RPC_URL` | `http://monero-wallet-rpc:18083/json_rpc` | The wallet's RPC endpoint |
| `MONERO_WALLET_RPC_USER` / `..._PASSWORD` | – | The wallet's `--rpc-login` |
| `ENABLE_SPENDING` | – | `1` registers the spend tools |
| `MAX_SPEND_PER_CALL` | `0` | Per-call cap in XMR (required > 0 if spending on) |
| `MAX_SPEND_PER_DAY` | `0` | Per-day cap in XMR (required > 0 if spending on) |
| `ALLOWED_DESTINATIONS` | – | Comma-separated destination allowlist |
| `MONERO_MCP_TOKEN` | – | Read token. **Required** unless `MCP_ALLOW_ANONYMOUS=1` |
| `MONERO_MCP_SPEND_TOKEN` | – | Spend token; mandatory when spending is on |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Listen address |
| `LOG_LEVEL` | `INFO` | Log level |

The upstream wallet should use `--rpc-login`; `install.sh` reads it from the
running container. Note that the regtest compose disables wallet-rpc login —
that must **not** carry over to a mainnet wallet.

## Running it

```sh
uv sync
MONERO_MCP_TOKEN=read-token uv run python server.py
```

Serve HTTP with two tokens and spending on:

```sh
MONERO_MCP_TOKEN=read \
MONERO_MCP_SPEND_TOKEN=spend \
ENABLE_SPENDING=1 MAX_SPEND_PER_CALL=0.5 MAX_SPEND_PER_DAY=2 \
uv run python server.py
```

## Development

```sh
uv run pytest -q                # tests, no network
uv run ruff check .             # lint
uv run pyright                  # typecheck
uv run python scripts/smoke.py  # boots the real app on loopback
```

`tests/test_auth.py` drives the token/spend gate as a pure ASGI app;
`tests/test_spend.py` covers the caps, allowlist, budget and dry-run path.

## Deploying

`deploy/` owns the quadlet unit and `install.sh`. On the target host:

```sh
cd ~/build && git clone <url> monero-wallet-mcp && cd monero-wallet-mcp
./deploy/install.sh
./deploy.sh
sudo firewall-cmd --permanent --add-port=8103/tcp && sudo firewall-cmd --reload
```

`install.sh` writes three env files under `~/.config/monero-wallet-mcp/`
(mode 600): `token.env` (both tokens), `upstream.env` (the RPC login, read
from the running container) and `spend.env` (seeded with `ENABLE_SPENDING=0`
and cap placeholders). Spending stays off until you edit `spend.env` and
restart. Roll back with `git checkout <older-sha> && ./deploy.sh`.
