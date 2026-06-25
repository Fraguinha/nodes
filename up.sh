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

echo "Fetching cluster info from $SERVER_HOSTNAME..."
SERVER_IP=$(tailscale ip -4 "$SERVER_HOSTNAME" 2>/dev/null)
[[ -n "$SERVER_IP" ]] || die "could not resolve $SERVER_HOSTNAME Tailscale IP"

K3S_TOKEN=$(tailscale ssh pi@"$SERVER_HOSTNAME" "sudo cat /var/lib/rancher/k3s/server/node-token" 2>/dev/null)
[[ -n "$K3S_TOKEN" ]] || die "could not fetch k3s token from $SERVER_HOSTNAME"

echo ""
echo "  Hostname: $HOSTNAME"
echo "  Server:   $SERVER_HOSTNAME ($SERVER_IP)"
echo "  Type:     cx23 (2 vCPU / 4 GB)"
echo "  Location: nbg1 (Nuremberg)"
echo ""
read -rp "Proceed? [y/N]: " confirm
[[ "$confirm" == [yY] ]] || { echo "Aborted."; exit 1; }

mkdir -p "$CACHE_DIR"
export HOSTNAME TAILSCALE_AUTHKEY SERVER_IP K3S_TOKEN
envsubst '${HOSTNAME} ${TAILSCALE_AUTHKEY} ${SERVER_IP} ${K3S_TOKEN}' \
  < "$SCRIPT_DIR/cloud-init/cloud.yaml" > "$CACHE_DIR/cloud-user-data"

hcloud server create \
  --name "$HOSTNAME" \
  --type cx23 \
  --image ubuntu-24.04 \
  --location nbg1 \
  --user-data-from-file "$CACHE_DIR/cloud-user-data"

rm -f "$CACHE_DIR/cloud-user-data"
echo "Done! cloud-init will run on first boot (~3-5 minutes)."
