#!/usr/bin/env bash
# Rebuild and redeploy the Monero Wallet MCP server.
#
# Run from anywhere. This repo IS the podman build context, so the script
# builds the image from the checked-out tree, restarts the rootless quadlet
# service, and waits for the container healthcheck to pass.
#
# Rollback:  git checkout <older-sha> && ./deploy.sh
set -euo pipefail

IMAGE="localhost/monero-wallet-mcp:latest"
CONTAINER="monero-wallet-mcp"
SERVICE="${MCP_SERVICE:-monero-wallet-mcp.service}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

cd "$(dirname "$0")"

./deploy/install.sh

python3 -m py_compile server.py rpc.py healthcheck.py

if git rev-parse --git-dir >/dev/null 2>&1; then
    echo "==> deploying $(git rev-parse --short HEAD 2>/dev/null || echo 'no commits') -- $(git log -1 --format='%s' 2>/dev/null || echo 'uncommitted')"
    [ -z "$(git status --porcelain)" ] || \
        echo "WARNING: uncommitted or staged changes present" >&2
fi

echo "==> building $IMAGE"
podman build -t "$IMAGE" .

echo "==> restarting $SERVICE"
systemctl --user restart "$SERVICE"

echo "==> waiting up to ${HEALTH_TIMEOUT}s for healthy status"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
status=""
while :; do
    status=$(podman ps --filter "name=$CONTAINER" --format '{{.Status}}')
    case "$status" in
        *"(healthy)"*)
            echo "==> live: $status"
            exit 0
            ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "ERROR: not healthy after ${HEALTH_TIMEOUT}s (status: ${status:-container missing})" >&2
        echo "check with: podman logs $CONTAINER" >&2
        exit 1
    fi
    sleep 5
done
