#!/usr/bin/env bash
# Install the quadlet unit for the Monero Wallet MCP server.
#
# Symlinks this repo's deploy/ unit into the user quadlet directory and
# reloads systemd, so the checkout is the single source of truth. Also seeds
# the env files under ~/.config/monero-wallet-mcp/ (mode 600):
#   token.env    -- a read token and a separate spend token
#   upstream.env -- the wallet's RPC login, read from the running container
#   spend.env    -- ENABLE_SPENDING=0 plus cap placeholders, left for you
#
# Idempotent: re-running refreshes the symlink and leaves existing env files
# untouched. No secret is committed or printed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_DIR/deploy"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
QUADLET_DIR="$CONFIG_HOME/containers/systemd"
ENV_DIR="$CONFIG_HOME/monero-wallet-mcp"
TOKEN_FILE="$ENV_DIR/token.env"
UPSTREAM_FILE="$ENV_DIR/upstream.env"
SPEND_FILE="$ENV_DIR/spend.env"

UNITS=(monero-wallet-mcp.container)

log() { printf '==> %s\n' "$*"; }

rand_hex() {
    openssl rand -hex 32 2>/dev/null && return 0
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

write_secret() {
    local path="$1"
    local old_umask
    old_umask=$(umask)
    umask 077
    cat > "$path"
    umask "$old_umask"
    chmod 600 "$path"
}

mkdir -p "$ENV_DIR"

# 1. tokens ---------------------------------------------------------------
if [ -s "$TOKEN_FILE" ]; then
    log "token env present: $TOKEN_FILE"
else
    read_token=""
    if command -v podman >/dev/null 2>&1; then
        read_token=$(podman exec monero-wallet-mcp printenv MONERO_MCP_TOKEN 2>/dev/null || true)
    fi
    if [ -n "$read_token" ]; then
        log "reused the existing read token"
    else
        read_token=$(rand_hex)
        log "generated a new read token -- update clients to match"
    fi
    spend_token=$(rand_hex)
    write_secret "$TOKEN_FILE" <<EOF
# Read token: admits the endpoint, cannot call spend tools.
MONERO_MCP_TOKEN=$read_token
# Spend token: additionally required for transfer / sweep_all / create_address.
MONERO_MCP_SPEND_TOKEN=$spend_token
EOF
    log "wrote $TOKEN_FILE (mode 600) with a read and a spend token"
fi

# 2. upstream RPC login ---------------------------------------------------
if [ -s "$UPSTREAM_FILE" ]; then
    log "upstream env present: $UPSTREAM_FILE"
else
    user=""
    password=""
    if command -v podman >/dev/null 2>&1; then
        cmd=$(podman inspect monero-wallet-rpc --format '{{json .Config.Cmd}}' 2>/dev/null || true)
        login=$(printf '%s' "$cmd" | grep -o -- '--rpc-login=[^"]*' | head -n1 | sed 's/^--rpc-login=//' || true)
        user=${login%%:*}
        password=${login#*:}
        [ "$login" = "$user" ] && password=""
    fi
    if [ -n "$user" ]; then
        log "read the wallet RPC login from the running container"
    else
        log "could not read the wallet RPC login; fill in $UPSTREAM_FILE by hand"
    fi
    write_secret "$UPSTREAM_FILE" <<EOF
# Upstream credentials for monero-wallet-rpc (--rpc-login).
MONERO_WALLET_RPC_USER=$user
MONERO_WALLET_RPC_PASSWORD=$password
EOF
    log "wrote $UPSTREAM_FILE (mode 600)"
fi

# 3. spending policy ------------------------------------------------------
if [ -s "$SPEND_FILE" ]; then
    log "spending policy present: $SPEND_FILE"
else
    write_secret "$SPEND_FILE" <<EOF
# Spending is OFF until you set ENABLE_SPENDING=1. With it on, both caps
# below must be positive (in XMR) or the server refuses to start.
ENABLE_SPENDING=0
MAX_SPEND_PER_CALL=1
MAX_SPEND_PER_DAY=5
# Optional comma-separated destination allowlist; empty allows any address.
ALLOWED_DESTINATIONS=
EOF
    log "wrote $SPEND_FILE (mode 600) with ENABLE_SPENDING=0"
fi

# 4. symlink units into the quadlet search path ---------------------------
mkdir -p "$QUADLET_DIR"

NETWORK_TARGET="$QUADLET_DIR/npm-network.network"
if [ ! -e "$NETWORK_TARGET" ]; then
    ln -sfn "$DEPLOY_DIR/npm-network.network" "$NETWORK_TARGET"
    log "linked npm-network.network"
else
    log "npm-network.network already present"
fi

for unit in "${UNITS[@]}"; do
    target="$QUADLET_DIR/$unit"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv -f "$target" "$target.bak"
        log "backed up existing $unit -> $unit.bak"
    fi
    ln -sfn "$DEPLOY_DIR/$unit" "$target"
    log "linked $unit"
done

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    log "systemd --user reloaded"
fi

cat <<EOF

Done. Spending is DISABLED until you edit $SPEND_FILE.

Next:
  ./deploy.sh                                     # install, build, restart, wait healthy
  # or, units only:
  systemctl --user start monero-wallet-mcp.service

Open the published port on the LAN:
  sudo firewall-cmd --permanent --add-port=8103/tcp && sudo firewall-cmd --reload

Clients send a bearer token from
  $TOKEN_FILE
as \`Authorization: Bearer <token>\`, pointed at http://<host>:8103/mcp.
The read token can call read tools; only the spend token can move funds.
EOF
