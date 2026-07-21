#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/rpi-flash"

die() { echo "Error: $1" >&2; exit 1; }

command -v pass &>/dev/null || die "pass not found"
pass ls &>/dev/null || die "pass store not accessible (check GPG key)"
command -v hcloud &>/dev/null || die "hcloud not found (brew install hcloud)"

read -rp "Hostname: " HOSTNAME
[[ -n "$HOSTNAME" ]] || die "hostname is required"
read -rp "Server Tailscale hostname: " SERVER_HOSTNAME
[[ -n "$SERVER_HOSTNAME" ]] || die "server hostname is required"

echo "Reading secrets..."
TAILSCALE_AUTHKEY=$(pass tailscale/authkey)
[[ -n "$TAILSCALE_AUTHKEY" ]] || die "pass tailscale/authkey is empty"
[[ "$TAILSCALE_AUTHKEY" =~ ^tskey- ]] || die "tailscale authkey invalid (should start with tskey-)"

echo ""
echo "  Hostname: $HOSTNAME"
echo "  Server:   $SERVER_HOSTNAME"
echo "  Type:     cx23 (2 vCPU / 4 GB)"
echo "  Location: nbg1 (Nuremberg)"
echo ""
read -rp "Proceed? [y/N]: " confirm
[[ "$confirm" == [yY] ]] || { echo "Aborted."; exit 1; }

mkdir -p "$CACHE_DIR"
export HOSTNAME TAILSCALE_AUTHKEY SERVER_HOSTNAME
envsubst '${HOSTNAME} ${TAILSCALE_AUTHKEY} ${SERVER_HOSTNAME}' \
  < "$SCRIPT_DIR/cloud-init/cloud.yaml" > "$CACHE_DIR/cloud-user-data"

hcloud server create \
  --name "$HOSTNAME" \
  --type cx23 \
  --image ubuntu-24.04 \
  --location nbg1 \
  --user-data-from-file "$CACHE_DIR/cloud-user-data"

rm -f "$CACHE_DIR/cloud-user-data"
echo "Done! cloud-init will run on first boot (~3-5 minutes)."
