#!/usr/bin/env bash
set -euo pipefail

RPI_IMAGER="/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager"
RASPI_BASE_URL="https://cdimage.ubuntu.com/releases"
CLOUD_BASE_URL="https://cloud-images.ubuntu.com/releases"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/rpi-flash"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "Error: $1" >&2; exit 1; }

latest_lts_version() {
  curl -sL "$1/" \
    | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' \
    | sort -V \
    | awk -F. '$1 % 2 == 0 && $2 == "04"' \
    | tail -1
}

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

[[ ${#DISKS[@]} -eq 0 ]] && die "no external disks found. Connect the disk via USB and try again."

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

read -rp "Architecture (arm64/amd64): " ARCH
[[ "$ARCH" == "arm64" || "$ARCH" == "amd64" ]] || die "architecture must be 'arm64' or 'amd64'"

if [[ "$ARCH" == "arm64" ]]; then
  [[ -f "$RPI_IMAGER" ]] || die "Raspberry Pi Imager not found. Install from https://www.raspberrypi.com/software/"
else
  command -v qemu-img &>/dev/null || die "qemu-img not found (brew install qemu)"
  command -v sgdisk &>/dev/null || die "sgdisk not found (brew install gptfdisk)"
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
fi

echo ""
echo "  Disk:     $DISK"
echo "  Arch:     $ARCH"
echo "  Hostname: $HOSTNAME"
echo "  Role:     $ROLE"
[[ "$ROLE" == "join" ]] && echo "  Server:   $SERVER_HOSTNAME"
echo ""
read -rp "Proceed? [y/N]: " confirm
[[ "$confirm" == [yY] ]] || { echo "Aborted."; exit 1; }

mkdir -p "$CACHE_DIR"
TEMPLATE="$SCRIPT_DIR/cloud-init/${ROLE}.yaml"
[[ -f "$TEMPLATE" ]] || die "template not found at $TEMPLATE"

export HOSTNAME TAILSCALE_AUTHKEY SERVER_HOSTNAME GITHUB_TOKEN
envsubst '${HOSTNAME} ${TAILSCALE_AUTHKEY} ${SERVER_HOSTNAME} ${GITHUB_TOKEN}' \
  < "$TEMPLATE" > "$CACHE_DIR/user-data"

if [[ "$ARCH" == "arm64" ]]; then
  echo "Finding latest Ubuntu Server LTS..."
  UBUNTU_VERSION=$(latest_lts_version "$RASPI_BASE_URL")
  UBUNTU_IMAGE=$(curl -sL "$RASPI_BASE_URL/$UBUNTU_VERSION/release/" \
    | grep -oE "ubuntu-[0-9.]+-preinstalled-server-arm64\+raspi\.img\.xz" \
    | head -1)
  [[ -n "$UBUNTU_IMAGE" ]] || die "could not find Ubuntu Server image for version $UBUNTU_VERSION"
  UBUNTU_URL="$RASPI_BASE_URL/$UBUNTU_VERSION/release/$UBUNTU_IMAGE"
  echo "Found: $UBUNTU_IMAGE"

  echo "Flashing image..."
  "$RPI_IMAGER" --cli \
    --cloudinit-userdata "$CACHE_DIR/user-data" \
    --cloudinit-networkconfig "$SCRIPT_DIR/cloud-init/network.yaml" \
    --enable-writing-system-drives \
    "$UBUNTU_URL" "$DISK"

  rm -f "$CACHE_DIR/user-data"
  echo "Done! Insert the NVMe into the Pi and power on."
else
  echo "Finding latest Ubuntu Server LTS..."
  UBUNTU_VERSION=$(latest_lts_version "$CLOUD_BASE_URL")
  UBUNTU_IMAGE=$(curl -sL "$CLOUD_BASE_URL/$UBUNTU_VERSION/release/" \
    | grep -oE "ubuntu-[0-9.]+-server-cloudimg-amd64\.img" \
    | head -1)
  [[ -n "$UBUNTU_IMAGE" ]] || die "could not find Ubuntu Server cloud image for version $UBUNTU_VERSION"
  UBUNTU_URL="$CLOUD_BASE_URL/$UBUNTU_VERSION/release/$UBUNTU_IMAGE"
  echo "Found: $UBUNTU_IMAGE"

  QCOW2_PATH="$CACHE_DIR/$UBUNTU_IMAGE"
  RAW_PATH="$CACHE_DIR/${UBUNTU_IMAGE%.img}.raw"
  if [[ ! -f "$QCOW2_PATH" ]]; then
    echo "Downloading image..."
    curl -sL "$UBUNTU_URL" -o "$QCOW2_PATH"
  fi
  echo "Converting image to raw..."
  qemu-img convert -O raw "$QCOW2_PATH" "$RAW_PATH"

  printf 'instance-id: %s\nlocal-hostname: %s\n' "$HOSTNAME" "$HOSTNAME" > "$CACHE_DIR/meta-data"

  echo "Writing image to disk..."
  diskutil unmountDisk "$DISK"
  dd if="$RAW_PATH" of="$DISK" bs=4m
  rm -f "$RAW_PATH"

  echo "Adding cloud-init seed partition..."
  sgdisk -e "$DISK"
  sgdisk -n 0:-64M:0 -t 0:0700 -c 0:CIDATA "$DISK"
  SEED_NUM=$(sgdisk -p "$DISK" | awk '/CIDATA/ {print $1}')
  SEED_PART="${DISK}s${SEED_NUM}"

  for _ in $(seq 1 10); do
    [[ -e "$SEED_PART" ]] && break
    sleep 1
  done
  if [[ ! -e "$SEED_PART" ]]; then
    echo ""
    echo "macOS hasn't picked up the new partition yet."
    read -rp "Unplug and reconnect the disk, then press enter: "
    [[ -e "$SEED_PART" ]] || die "still can't find $SEED_PART — check 'diskutil list $DISK' (the identifier may have changed)"
  fi

  newfs_msdos -v CIDATA "$SEED_PART"
  diskutil mount "$SEED_PART"
  cp "$CACHE_DIR/user-data" /Volumes/CIDATA/user-data
  cp "$CACHE_DIR/meta-data" /Volumes/CIDATA/meta-data
  cp "$SCRIPT_DIR/cloud-init/network.yaml" /Volumes/CIDATA/network-config
  diskutil eject "$DISK"

  rm -f "$CACHE_DIR/user-data" "$CACHE_DIR/meta-data"
  echo "Done! Insert the disk into the PC and power on."
fi
