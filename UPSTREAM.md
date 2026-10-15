# What this repo owns, and what it depends on

This repo owns only the **application** — its Python code, tests, CI and
`Containerfile`. It ships no unit and no env file. The runtime is owned by
[`stack`](https://github.com/regularnmae/stack), which holds every quadlet unit
and the `npm-network.network` the MCP servers share, writes this server's env
file(s), and publishes the host port. See that repo for the unit, the env-file
locations and `stack/deploy/examples/monero-wallet-mcp/`.

It does **not** own the wallet RPC. The `monero-wallet-rpc` unit is owned by
`stack`, together with the rest of the crypto node stack — one file, one owner.

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
cd ~/build/stack
./deploy.sh --only monero-wallet-mcp   # build the image, restart, wait for "(healthy)"
```

`stack` reads `--rpc-login` from the running `monero-wallet-rpc` container.
Spending stays **off** until you flip `ENABLE_SPENDING` in the spend env `stack`
seeds; see that repo for the env-file location and the host port.
