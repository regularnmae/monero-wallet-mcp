# monero-wallet-mcp — agent notes

Flat three-module Python MCP server (not a package). `server.py` is the app —
config, read tools, the gated spend tools, the spend budget and the
two-token middleware; `rpc.py` is its **stdlib-only** monero-wallet-rpc client;
`healthcheck.py` is the container healthcheck probe. Rootless podman/quadlet;
the runtime — its quadlet unit, the `npm-network` it shares and the env wiring —
is owned by the `stack` repo (https://github.com/regularnmae/stack), not here.
uv only. `mcp` is 2.x (`MCPServer`).

One of four sibling crypto MCP servers. This is a **money server** — the
safety posture matters more than the features. Read `README.md` before
changing anything about how spending works.

## Commands

```sh
uv run ruff check .            # lint (CI gate)
uv run pyright                 # typecheck (CI gate)
uv run pytest -q               # tests, no network
uv run python scripts/smoke.py # boots the real app on a loopback port
```

Deployment is owned by the `stack` repo: `cd ~/build/stack && ./deploy.sh --only monero-wallet-mcp`
rebuilds the image, restarts `monero-wallet-mcp.service` and waits for
"(healthy)"; `./deploy.sh` deploys the whole stack. The unit and `npm-network`
live there, not here.

## Safety invariants — do not weaken these

- **Spending is off by default.** The spend tools are defined inside
  `if ENABLE_SPENDING:` so that when it is off they are not registered at all.
  Keep that structure; do not register them unconditionally.
- **Two tokens.** The middleware admits the read token for reads and requires
  the spend token for any `tools/call` naming a tool in `SPEND_TOOLS`. Enabling
  spending with no spend token is a startup error. If you add a spend tool, add
  its name to `SPEND_TOOLS` (and to `tests/test_schemas.py`).
- **Caps are mandatory when spending is on.** `MAX_SPEND_PER_CALL` and
  `MAX_SPEND_PER_DAY` must both be > 0 or import raises. There is no
  "unlimited" default.
- **Every spend funnels through `_run_spend`** — precheck (caps + allowlist),
  reserve the daily budget for non-dry-runs, call, audit, release on failure.
  Do not call `rpc.call("transfer", …)` from a tool directly.
- **Dry-run is the default argument** on every spend tool (`do_not_relay`).
  A dry run must not consume the budget.
- **Amounts are XMR in the tool signature, atomic units on the wire**,
  converted once via `ATOMIC_PER_XMR`. Never accept atomic units from the model.

## Facts that are easy to get wrong

- The middleware **reads the POST body** to learn the tool name, then replays
  it. `_replay(body, original)` delegates to the original `receive` after the
  replayed message — if it returns `http.disconnect` instead, the SSE response
  is truncated and the client sees `IncompleteRead`. Do not "simplify" it back.
- The read token and the spend token are both accepted for read; the spend
  token is a superset.
- `DailyBudget` is process-global and per-UTC-day; only committed (non-dry-run)
  spends reserve it, and a failed call releases.
- `rpc.py` raises one exception type, `RPCError`, and is stdlib-only.
- **Authentication is HTTP digest, on one connection per call.**
  monero-wallet-rpc rejects Basic with 401; its digest nonce is bound to the
  TCP connection (a fresh connection gives `stale=true`); and Python's
  `urllib` digest handler does not interoperate with monero's server. Hence
  `rpc.py` computes the digest itself and does challenge + retry on a single
  `http.client.HTTPConnection` created in `call()` and always closed. Do not
  "simplify" this to `urllib` + a Basic header. `tests/test_rpc.py` pins the
  RFC 2617 vector and the retry-on-one-connection flow via the `_connect` /
  `_post` seams.
- Tool bodies are synchronous (`def`); anyio runs them on worker threads.
- `tests/test_auth.py` drives the middleware as a **pure ASGI app** (no
  socket); a fake `Inner` app records whether it was reached and what body it
  saw. Use that pattern for middleware changes.
- `pytest` uses `pythonpath = ["."]`; do **not** `from conftest import ...` in a
  test (tests is a package) — use the fixtures.
- A new top-level module must go in the `Containerfile`'s `COPY` lines **and**
  pyright's `include`.
- Publishing: `PublishPort=8103:8000`. Open `8103/tcp` in firewalld.
- `stack/deploy/install.sh` writes `$DATA_DIR/config/monero-wallet-mcp/{token,upstream,spend}.env`
  (mode 600) and renders the unit. The env-file shapes are documented in
  `stack/deploy/examples/monero-wallet-mcp/`.
