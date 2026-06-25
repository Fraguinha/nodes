#!/usr/bin/env bash
set -euo pipefail

die() { echo "Error: $1" >&2; exit 1; }

command -v hcloud &>/dev/null || die "hcloud not found (brew install hcloud)"

read -rp "Hostname: " HOSTNAME
[[ -n "$HOSTNAME" ]] || die "hostname is required"

hcloud server list -o columns=name | grep -q "^$HOSTNAME$" || die "server '$HOSTNAME' not found"

echo ""
echo "  Delete: $HOSTNAME"
echo ""
read -rp "Proceed? [y/N]: " confirm
[[ "$confirm" == [yY] ]] || { echo "Aborted."; exit 1; }

PEER=$(hcloud server list -o columns=name | awk -v h="$HOSTNAME" 'NR>1 && $1 != h {print $1; exit}')
[[ -n "$PEER" ]] || PEER="$HOSTNAME"

echo "Draining node..."
timeout 30 tailscale ssh pi@"$PEER" \
  "sudo k3s kubectl drain $HOSTNAME --ignore-daemonsets --delete-emptydir-data --force --disable-eviction" 2>/dev/null || true

echo "Removing node from cluster..."
timeout 30 tailscale ssh pi@"$PEER" \
  "sudo k3s kubectl delete node $HOSTNAME" 2>/dev/null || true

echo "Deleting server..."
hcloud server delete "$HOSTNAME"

echo "Done!"
