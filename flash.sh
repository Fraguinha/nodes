#!/usr/bin/env bash
set -euo pipefail

RPI_IMAGER="/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager"
UBUNTU_BASE_URL="https://cdimage.ubuntu.com/releases"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/rpi-flash"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "Error: $1" >&2; exit 1; }

[[ -f "$RPI_IMAGER" ]] || die "Raspberry Pi Imager not found. Install from https://www.raspberrypi.com/software/"
command -v pass &>/dev/null || die "pass not found"
pass ls &>/dev/null || die "pass store not accessible (check GPG key)"

DISKS=()
while IFS= read -r line; do
  disk=$(echo "$line" | awk '{print $1}')
  [[ "$disk" == "/dev/disk0" || "$disk" == "/dev/disk1" ]] && continue
  size=$(diskutil info "$disk" 2>/dev/null | grep "Disk Size" | awk -F'(' '{print $2}' | awk -F')' '{print $1}')
  name=$(diskutil info "$disk" 2>/dev/null | grep "Device / Media Name" | awk -F: '{print $2}' | xargs)
  DISKS+=("$disk|$name|$size")
done < <(diskutil list | grep "^/dev/disk" | grep -v "synthesized")

[[ ${#DISKS[@]} -eq 0 ]] && die "no external disks found. Connect the NVMe via USB and try again."

if [[ ${#DISKS[@]} -eq 1 ]]; then
  DISK=$(echo "${DISKS[0]}" | cut -d'|' -f1)
  echo "Detected: $DISK - $(echo "${DISKS[0]}" | cut -d'|' -f2) ($(echo "${DISKS[0]}" | cut -d'|' -f3))"
else
  echo "Multiple external disks found:"
  for i in "${!DISKS[@]}"; do
    echo "  $((i+1))) $(echo "${DISKS[$i]}" | tr '|' ' ')"
  done
  read -rp "Select disk [1-${#DISKS[@]}]: " choice
  DISK=$(echo "${DISKS[$((choice-1))]}" | cut -d'|' -f1)
fi

read -rp "Hostname: " HOSTNAME
[[ -n "$HOSTNAME" ]] || die "hostname is required"
read -rp "Role (init/join): " ROLE
[[ "$ROLE" == "init" || "$ROLE" == "join" ]] || die "role must be 'init' or 'join'"

if [[ "$ROLE" == "join" ]]; then
  read -rp "Server Tailscale hostname: " SERVER_HOSTNAME
  [[ -n "$SERVER_HOSTNAME" ]] || die "server hostname is required"
fi

echo "Reading secrets..."
TAILSCALE_AUTHKEY=$(pass tailscale/authkey)
[[ -n "$TAILSCALE_AUTHKEY" ]] || die "pass tailscale/authkey is empty"
[[ "$TAILSCALE_AUTHKEY" =~ ^tskey- ]] || die "tailscale authkey invalid (should start with tskey-)"

if [[ "$ROLE" == "init" ]]; then
  GITHUB_TOKEN=$(pass github/token)
  [[ -n "$GITHUB_TOKEN" ]] || die "pass github/token is empty"
  SOPS_AGE_KEY_B64=$(base64 -b 0 < "${HOME}/.config/sops/age/keys.txt")
  [[ -n "$SOPS_AGE_KEY_B64" ]] || die "SOPS age key not found"
fi

echo ""
echo "  Disk:     $DISK"
echo "  Hostname: $HOSTNAME"
echo "  Role:     $ROLE"
[[ "$ROLE" == "join" ]] && echo "  Server:   $SERVER_HOSTNAME"
echo ""
read -rp "Proceed? [y/N]: " confirm
[[ "$confirm" == [yY] ]] || { echo "Aborted."; exit 1; }

echo "Finding latest Ubuntu Server LTS..."
UBUNTU_VERSION=$(curl -s "$UBUNTU_BASE_URL/" \
  | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' \
  | sort -V \
  | awk -F. '$1 % 2 == 0 && $2 == "04"' \
  | tail -1)
UBUNTU_IMAGE=$(curl -s "$UBUNTU_BASE_URL/$UBUNTU_VERSION/release/" \
  | grep -oE "ubuntu-[0-9.]+-preinstalled-server-arm64\+raspi\.img\.xz" \
  | head -1)
[[ -n "$UBUNTU_IMAGE" ]] || die "could not find Ubuntu Server image for version $UBUNTU_VERSION"
UBUNTU_URL="$UBUNTU_BASE_URL/$UBUNTU_VERSION/release/$UBUNTU_IMAGE"
echo "Found: $UBUNTU_IMAGE"

mkdir -p "$CACHE_DIR"
TEMPLATE="$SCRIPT_DIR/cloud-init/${ROLE}.yaml"
[[ -f "$TEMPLATE" ]] || die "template not found at $TEMPLATE"

export HOSTNAME TAILSCALE_AUTHKEY SERVER_HOSTNAME GITHUB_TOKEN SOPS_AGE_KEY_B64
envsubst '${HOSTNAME} ${TAILSCALE_AUTHKEY} ${SERVER_HOSTNAME} ${GITHUB_TOKEN} ${SOPS_AGE_KEY_B64}' \
  < "$TEMPLATE" > "$CACHE_DIR/user-data"

echo "Flashing image..."
"$RPI_IMAGER" --cli \
  --cloudinit-userdata "$CACHE_DIR/user-data" \
  --cloudinit-networkconfig "$SCRIPT_DIR/cloud-init/network.yaml" \
  --enable-writing-system-drives \
  "$UBUNTU_URL" "$DISK"

rm -f "$CACHE_DIR/user-data"
echo "Done! Insert the NVMe into the Pi and power on."
