#!/usr/bin/env bash

flash_command() {
RASPI_BASE_URL="https://cdimage.ubuntu.com/releases"
CLOUD_BASE_URL="https://cloud-images.ubuntu.com/releases"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/nodes/images"
DISK=""
ARCH=""
NODE_HOSTNAME=""
ROLE=""
SERVER_HOSTNAME=""
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: nodes provision physical [options]

  --disk PATH
  --arch arm64|amd64
  --hostname NAME
  --role init|join|agent
  --server NAME
  --yes
  --help
EOF
}

latest_lts_version() {
  curl -fsSL --retry 3 "$1/" \
    | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' \
    | awk -F. '$1 % 2 == 0 && $2 == "04"' \
    | sort -t. -k1,1n -k2,2n -k3,3n \
    | tail -n 1
}

sha256_file() {
  shasum -a 256 "$1" | awk '{print $1}'
}

download_verified_image() {
  local base_url="$1"
  local image="$2"
  local destination="$3"
  local checksums=""
  local expected=""
  local actual=""

  checksums=$(curl -fsSL --retry 3 "$base_url/SHA256SUMS")
  expected=$(printf '%s\n' "$checksums" | awk -v image="$image" '
    {
      name = $2
      sub(/^\*/, "", name)
      if (name == image) {
        print $1
        exit
      }
    }
  ')
  [[ -n "$expected" ]] || die "checksum for $image not found"

  if [[ -f "$destination" ]]; then
    actual=$(sha256_file "$destination")
    if [[ "$actual" == "$expected" ]]; then
      echo "Using cached image: $image"
      return
    fi
    rm -f "$destination"
  fi

  echo "Downloading $image..."
  curl -fL --retry 3 --continue-at - --output "$destination" "$base_url/$image"
  actual=$(sha256_file "$destination")
  if [[ "$actual" != "$expected" ]]; then
    rm -f "$destination"
    die "checksum verification failed for $image"
  fi
}

raw_disk_target() {
  local raw_disk="$1"
  local size="$2"

  printf \
    'json:{"driver":"raw","size":%s,"file":{"driver":"host_device","filename":"%s"}}' \
    "$size" "$raw_disk"
}

verify_written_image() {
  local source_path="$1"
  local source_format="$2"
  local raw_disk="$3"
  local virtual_size=""
  local target=""

  virtual_size=$("$QEMU_IMG" info --output=json -f "$source_format" "$source_path" \
    | plutil -extract virtual-size raw -o - -)
  [[ "$virtual_size" =~ ^[1-9][0-9]*$ ]] || die "could not determine the image size"

  target=$(raw_disk_target "$raw_disk" "$virtual_size")
  echo "Verifying the written image..."
  sudo "$QEMU_IMG" compare -p -f "$source_format" "$source_path" "$target" \
    || die "written image does not match the source"
}

candidate_disks() {
  local disk=""
  local info=""
  local internal=""
  local device_type=""
  local protocol=""

  while IFS= read -r disk; do
    info=$(diskutil info -plist "$disk" 2>/dev/null) || continue
    internal=$(plutil -extract Internal raw -o - - <<< "$info" 2>/dev/null) || continue
    device_type=$(plutil -extract VirtualOrPhysical raw -o - - <<< "$info" 2>/dev/null || true)
    protocol=$(plutil -extract BusProtocol raw -o - - <<< "$info" 2>/dev/null || true)
    if [[ "$internal" == "false" && "$device_type" != "Virtual" && "$protocol" != "Disk Image" ]]; then
      printf '%s\n' "$disk"
    fi
  done < <(diskutil list physical | awk '/^\/dev\/disk[0-9]+/ {print $1}')
}

disk_description() {
  local disk="$1"
  local info=""
  local name=""
  local size=""

  info=$(diskutil info -plist "$disk" 2>/dev/null) || die "disk '$disk' not found"
  name=$(plutil -extract MediaName raw -o - - <<< "$info" 2>/dev/null || true)
  size=$(plutil -extract TotalSize raw -o - - <<< "$info" 2>/dev/null || true)
  if [[ "$size" =~ ^[0-9]+$ ]]; then
    size=$(awk -v bytes="$size" 'BEGIN {printf "%.1f GB", bytes / 1e9}')
  fi
  printf '%s - %s (%s)' "$disk" "${name:-unknown}" "${size:-unknown size}"
}

validate_target_disk() {
  local disk="$1"
  local info=""
  local internal=""

  [[ "$disk" =~ ^/dev/disk[0-9]+$ ]] || die "invalid disk '$disk'"
  info=$(diskutil info -plist "$disk" 2>/dev/null) || die "disk '$disk' not found"
  internal=$(plutil -extract Internal raw -o - - <<< "$info" 2>/dev/null) \
    || die "could not determine whether '$disk' is internal"
  [[ "$internal" == "false" ]] || die "refusing to write internal disk '$disk'"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --disk)
      [[ $# -ge 2 ]] || die "--disk requires a value"
      DISK="$2"
      shift 2
      ;;
    --arch)
      [[ $# -ge 2 ]] || die "--arch requires a value"
      ARCH="$2"
      shift 2
      ;;
    --hostname)
      [[ $# -ge 2 ]] || die "--hostname requires a value"
      NODE_HOSTNAME="$2"
      shift 2
      ;;
    --role)
      [[ $# -ge 2 ]] || die "--role requires a value"
      ROLE="$2"
      shift 2
      ;;
    --server)
      [[ $# -ge 2 ]] || die "--server requires a value"
      SERVER_HOSTNAME="$2"
      shift 2
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option '$1'"
      ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || die "physical flashing is supported on macOS"
require_command curl
require_command diskutil
require_command plutil
require_command pass
require_command shasum

if [[ -z "$DISK" ]]; then
  DISKS=()
  while IFS= read -r disk; do
    [[ -n "$disk" ]] && DISKS+=("$disk")
  done < <(candidate_disks)

  [[ ${#DISKS[@]} -gt 0 ]] || die "no external disks found"
  if [[ ${#DISKS[@]} -eq 1 ]]; then
    DISK="${DISKS[0]}"
    echo "Detected: $(disk_description "$DISK")"
  else
    echo "External disks:"
    for index in "${!DISKS[@]}"; do
      printf '  %d) %s\n' "$((index + 1))" "$(disk_description "${DISKS[$index]}")"
    done
    choice=""
    read -r -p "Select disk [1-${#DISKS[@]}]: " choice
    [[ "$choice" =~ ^[0-9]+$ && "$choice" -ge 1 && "$choice" -le ${#DISKS[@]} ]] \
      || die "invalid disk selection"
    DISK="${DISKS[$((choice - 1))]}"
  fi
fi
validate_target_disk "$DISK"

if [[ -z "$ARCH" ]]; then
  read -r -p "Architecture (arm64/amd64): " ARCH
fi
[[ "$ARCH" == "arm64" || "$ARCH" == "amd64" ]] || die "architecture must be 'arm64' or 'amd64'"

require_command qemu-img "brew install qemu"
QEMU_IMG=$(command -v qemu-img)
if [[ "$ARCH" == "arm64" ]]; then
  require_command xz "brew install xz"
else
  require_command sgdisk "brew install gptfdisk"
  SGDISK=$(command -v sgdisk)
fi

if [[ -z "$NODE_HOSTNAME" ]]; then
  read -r -p "Hostname: " NODE_HOSTNAME
fi
validate_hostname "$NODE_HOSTNAME"

if [[ -z "$ROLE" ]]; then
  read -r -p "Role (init/join/agent): " ROLE
fi
[[ "$ROLE" == "init" || "$ROLE" == "join" || "$ROLE" == "agent" ]] \
  || die "role must be 'init', 'join' or 'agent'"

if [[ "$ROLE" != "init" ]]; then
  if [[ -z "$SERVER_HOSTNAME" ]]; then
    read -r -p "Server Tailscale hostname: " SERVER_HOSTNAME
  fi
  validate_hostname "$SERVER_HOSTNAME"
fi

TAILSCALE_AUTHKEY="${NODES_TAILSCALE_AUTHKEY:-}"
if [[ -z "$TAILSCALE_AUTHKEY" ]]; then
  TAILSCALE_AUTHKEY=$(pass_value "${NODES_TAILSCALE_PASS_PATH:-tailscale/authkey}") \
    || die "Tailscale auth key not found in pass"
fi
[[ "$TAILSCALE_AUTHKEY" == tskey-* ]] || die "Tailscale auth key must start with tskey-"

GITHUB_TOKEN="${NODES_GITHUB_TOKEN:-}"
if [[ "$ROLE" == "init" && -z "$GITHUB_TOKEN" ]]; then
  GITHUB_TOKEN=$(pass_value "${NODES_GITHUB_PASS_PATH:-github/token}") \
    || die "GitHub token not found in pass"
fi

WIFI_SSID="${NODES_WIFI_SSID:-}"
if [[ -z "$WIFI_SSID" ]]; then
  WIFI_SSID=$(pass_value "${NODES_WIFI_SSID_PASS_PATH:-wifi/ssid}" || true)
fi
if [[ -z "$WIFI_SSID" ]]; then
  read -r -p "Wi-Fi SSID: " WIFI_SSID
fi

WIFI_PASSWORD="${NODES_WIFI_PASSWORD:-}"
if [[ -z "$WIFI_PASSWORD" ]]; then
  WIFI_PASSWORD=$(pass_value "${NODES_WIFI_PASSWORD_PASS_PATH:-wifi/password}" || true)
fi
if [[ -z "$WIFI_PASSWORD" ]]; then
  read -r -s -p "Wi-Fi password: " WIFI_PASSWORD
  echo
fi
unset NODES_TAILSCALE_AUTHKEY NODES_GITHUB_TOKEN NODES_WIFI_PASSWORD

echo
echo "  Disk:     $(disk_description "$DISK")"
echo "  Arch:     $ARCH"
echo "  Hostname: $NODE_HOSTNAME"
echo "  Role:     $ROLE"
[[ "$ROLE" != "init" ]] && echo "  Server:   $SERVER_HOSTNAME"
echo "  Wi-Fi:    $WIFI_SSID"
echo
confirm "Erase $DISK and continue?" "$ASSUME_YES"

umask 077
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/nodes-flash.XXXXXX")
trap 'rm -rf "$WORK_DIR"' EXIT
mkdir -p "$CACHE_DIR"
USER_DATA="$WORK_DIR/user-data"
NETWORK_CONFIG="$WORK_DIR/network-config"
META_DATA="$WORK_DIR/meta-data"
render_user_data "$USER_DATA" "$NODE_HOSTNAME" "$ROLE" "physical" "$SERVER_HOSTNAME" "$TAILSCALE_AUTHKEY" "$GITHUB_TOKEN"
render_network_config "$NETWORK_CONFIG" "$WIFI_SSID" "$WIFI_PASSWORD"
printf 'instance-id: %s\nlocal-hostname: %s\n' "$NODE_HOSTNAME" "$NODE_HOSTNAME" > "$META_DATA"

echo "Finding the latest Ubuntu Server LTS..."
if [[ "$ARCH" == "arm64" ]]; then
  UBUNTU_VERSION=$(latest_lts_version "$RASPI_BASE_URL")
  [[ -n "$UBUNTU_VERSION" ]] || die "could not find an Ubuntu LTS release"
  RELEASE_URL="$RASPI_BASE_URL/$UBUNTU_VERSION/release"
  UBUNTU_IMAGE=$(curl -fsSL --retry 3 "$RELEASE_URL/" \
    | grep -oE "ubuntu-[0-9.]+-preinstalled-server-arm64\+raspi\.img\.xz" \
    | head -n 1)
  [[ -n "$UBUNTU_IMAGE" ]] || die "could not find an arm64 Raspberry Pi image"
else
  UBUNTU_VERSION=$(latest_lts_version "$CLOUD_BASE_URL")
  [[ -n "$UBUNTU_VERSION" ]] || die "could not find an Ubuntu LTS release"
  RELEASE_URL="$CLOUD_BASE_URL/$UBUNTU_VERSION/release"
  UBUNTU_IMAGE=$(curl -fsSL --retry 3 "$RELEASE_URL/" \
    | grep -oE "ubuntu-[0-9.]+-server-cloudimg-amd64\.img" \
    | head -n 1)
  [[ -n "$UBUNTU_IMAGE" ]] || die "could not find an amd64 cloud image"
fi
echo "Selected image: $UBUNTU_IMAGE"
if [[ "$ARCH" == "arm64" && "$UBUNTU_VERSION" == 26.04* ]]; then
  echo "Raspberry Pi 5 requires boot EEPROM 2025-02-11 or newer for Ubuntu $UBUNTU_VERSION."
  echo "Check it on the Pi with: rpi-eeprom-update"
  confirm "EEPROM requirement verified?" "$ASSUME_YES"
fi

IMAGE_PATH="$CACHE_DIR/$UBUNTU_IMAGE"
download_verified_image "$RELEASE_URL" "$UBUNTU_IMAGE" "$IMAGE_PATH"

SOURCE_PATH="$IMAGE_PATH"
SOURCE_FORMAT="qcow2"
ARM_BOOT_SECTOR=""
if [[ "$ARCH" == "arm64" ]]; then
  SOURCE_PATH="$WORK_DIR/${UBUNTU_IMAGE%.xz}"
  SOURCE_FORMAT="raw"
  ARM_BOOT_SECTOR="$WORK_DIR/arm-boot-sector.bin"
  echo "Decompressing $UBUNTU_IMAGE..."
  xz -dc "$IMAGE_PATH" > "$SOURCE_PATH"
  dd if="$SOURCE_PATH" of="$ARM_BOOT_SECTOR" bs=512 count=1 2>/dev/null
  dd if=/dev/zero of="$SOURCE_PATH" bs=512 count=1 conv=notrunc 2>/dev/null
fi

sudo -v
echo "Writing $UBUNTU_IMAGE to $DISK with QEMU..."
diskutil unmountDisk "$DISK"
RAW_DISK="/dev/r${DISK#/dev/}"
sudo "$QEMU_IMG" convert -p -n -S 0 -f "$SOURCE_FORMAT" -O raw "$SOURCE_PATH" "$RAW_DISK"
sync
verify_written_image "$SOURCE_PATH" "$SOURCE_FORMAT" "$RAW_DISK"
if [[ -n "$ARM_BOOT_SECTOR" ]]; then
  echo "Publishing the Raspberry Pi partition table..."
  BOOT_TARGET=$(raw_disk_target "$RAW_DISK" 512)
  sudo "$QEMU_IMG" convert -n -S 0 -f raw -O raw "$ARM_BOOT_SECTOR" "$BOOT_TARGET"
  sync
  verify_written_image "$ARM_BOOT_SECTOR" raw "$RAW_DISK"
fi

diskutil unmountDisk "$DISK" >/dev/null 2>&1 || true
if [[ "$ARCH" == "arm64" ]]; then
  echo "Installing the cloud-init seed..."
  SEED_PART="${DISK}s1"
else
  echo "Adding the cloud-init seed partition..."
  sudo "$SGDISK" -e "$DISK"
  sudo "$SGDISK" -n 0:-64M:0 -t 0:0700 -c 0:CIDATA "$DISK"
  SEED_NUM=$(sudo "$SGDISK" -p "$DISK" | awk '/CIDATA/ {print $1; exit}')
  [[ -n "$SEED_NUM" ]] || die "could not create the CIDATA partition"
  SEED_PART="${DISK}s${SEED_NUM}"
fi

attempt=0
while [[ ! -e "$SEED_PART" && $attempt -lt 10 ]]; do
  sleep 1
  attempt=$((attempt + 1))
done
if [[ ! -e "$SEED_PART" ]]; then
  echo
  read -r -p "Reconnect the drive, then press enter: "
  [[ -e "$SEED_PART" ]] || die "could not find $SEED_PART"
fi

if [[ "$ARCH" == "amd64" ]]; then
  sudo newfs_msdos -v CIDATA "$SEED_PART"
fi
diskutil mount "$SEED_PART" >/dev/null
MOUNT_POINT=$(diskutil info -plist "$SEED_PART" | plutil -extract MountPoint raw -o - -) \
  || die "could not determine the mount point for $SEED_PART"
[[ -d "$MOUNT_POINT" ]] || die "could not mount $SEED_PART"
if [[ "$ARCH" == "arm64" && ! -f "$MOUNT_POINT/config.txt" ]]; then
  diskutil unmount "$SEED_PART" >/dev/null 2>&1 || true
  die "$SEED_PART is not the Raspberry Pi system-boot partition"
fi
cp "$USER_DATA" "$MOUNT_POINT/user-data"
cp "$META_DATA" "$MOUNT_POINT/meta-data"
cp "$NETWORK_CONFIG" "$MOUNT_POINT/network-config"
sync
diskutil eject "$DISK"

if [[ "$ARCH" == "arm64" ]]; then
  echo "Done. Insert the drive into the Raspberry Pi and power it on."
else
  echo "Done. Insert the drive into the PC and power it on."
fi
}

up_command() {
NODE_HOSTNAME=""
ROLE=""
SERVER_HOSTNAME=""
SERVER_TYPE="${NODES_HCLOUD_TYPE:-cx23}"
LOCATION="${NODES_HCLOUD_LOCATION:-nbg1}"
IMAGE="${NODES_HCLOUD_IMAGE:-ubuntu-24.04}"
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: nodes provision cloud [options]

  --hostname NAME
  --role init|join|agent
  --server NAME
  --type TYPE
  --location LOCATION
  --image IMAGE
  --yes
  --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname)
      [[ $# -ge 2 ]] || die "--hostname requires a value"
      NODE_HOSTNAME="$2"
      shift 2
      ;;
    --role)
      [[ $# -ge 2 ]] || die "--role requires a value"
      ROLE="$2"
      shift 2
      ;;
    --server)
      [[ $# -ge 2 ]] || die "--server requires a value"
      SERVER_HOSTNAME="$2"
      shift 2
      ;;
    --type)
      [[ $# -ge 2 ]] || die "--type requires a value"
      SERVER_TYPE="$2"
      shift 2
      ;;
    --location)
      [[ $# -ge 2 ]] || die "--location requires a value"
      LOCATION="$2"
      shift 2
      ;;
    --image)
      [[ $# -ge 2 ]] || die "--image requires a value"
      IMAGE="$2"
      shift 2
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option '$1'"
      ;;
  esac
done

require_command pass
require_command hcloud "brew install hcloud"

if [[ -z "$NODE_HOSTNAME" ]]; then
  read -r -p "Hostname: " NODE_HOSTNAME
fi
validate_hostname "$NODE_HOSTNAME"

if hcloud server describe "$NODE_HOSTNAME" >/dev/null 2>&1; then
  die "server '$NODE_HOSTNAME' already exists"
fi

if [[ -z "$ROLE" ]]; then
  read -r -p "Role (init/join/agent) [join]: " ROLE
  ROLE="${ROLE:-join}"
fi
[[ "$ROLE" == "init" || "$ROLE" == "join" || "$ROLE" == "agent" ]] \
  || die "role must be 'init', 'join' or 'agent'"

if [[ "$ROLE" != "init" ]]; then
  if [[ -z "$SERVER_HOSTNAME" ]]; then
    read -r -p "Server Tailscale hostname: " SERVER_HOSTNAME
  fi
  validate_hostname "$SERVER_HOSTNAME"
fi

TAILSCALE_AUTHKEY="${NODES_TAILSCALE_AUTHKEY:-}"
if [[ -z "$TAILSCALE_AUTHKEY" ]]; then
  TAILSCALE_AUTHKEY=$(pass_value "${NODES_TAILSCALE_PASS_PATH:-tailscale/authkey}") \
    || die "Tailscale auth key not found in pass"
fi
[[ "$TAILSCALE_AUTHKEY" == tskey-* ]] || die "Tailscale auth key must start with tskey-"

GITHUB_TOKEN="${NODES_GITHUB_TOKEN:-}"
if [[ "$ROLE" == "init" && -z "$GITHUB_TOKEN" ]]; then
  GITHUB_TOKEN=$(pass_value "${NODES_GITHUB_PASS_PATH:-github/token}") \
    || die "GitHub token not found in pass"
fi
unset NODES_TAILSCALE_AUTHKEY NODES_GITHUB_TOKEN

echo
echo "  Hostname: $NODE_HOSTNAME"
echo "  Role:     $ROLE"
[[ "$ROLE" != "init" ]] && echo "  Server:   $SERVER_HOSTNAME"
echo "  Type:     $SERVER_TYPE"
echo "  Location: $LOCATION"
echo "  Image:    $IMAGE"
echo
confirm "Create the server?" "$ASSUME_YES"

umask 077
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/nodes-cloud.XXXXXX")
trap 'rm -rf "$WORK_DIR"' EXIT
USER_DATA="$WORK_DIR/user-data"
render_user_data "$USER_DATA" "$NODE_HOSTNAME" "$ROLE" "cloud" "$SERVER_HOSTNAME" "$TAILSCALE_AUTHKEY" "$GITHUB_TOKEN"

hcloud server create \
  --name "$NODE_HOSTNAME" \
  --type "$SERVER_TYPE" \
  --image "$IMAGE" \
  --location "$LOCATION" \
  --user-data-from-file "$USER_DATA"

echo "Done. Cloud-init will provision the node in approximately five minutes."
}

down_command() {
NODE_HOSTNAME=""
PEER="${NODES_CLUSTER_PEER:-}"
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: nodes remove [options]

  --hostname NAME
  --peer NAME
  --yes
  --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname)
      [[ $# -ge 2 ]] || die "--hostname requires a value"
      NODE_HOSTNAME="$2"
      shift 2
      ;;
    --peer)
      [[ $# -ge 2 ]] || die "--peer requires a value"
      PEER="$2"
      shift 2
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option '$1'"
      ;;
  esac
done

require_command hcloud "brew install hcloud"
require_command ssh

if [[ -z "$NODE_HOSTNAME" ]]; then
  read -r -p "Hostname: " NODE_HOSTNAME
fi
validate_hostname "$NODE_HOSTNAME"
hcloud server describe "$NODE_HOSTNAME" >/dev/null 2>&1 || die "server '$NODE_HOSTNAME' not found"

if [[ -z "$PEER" ]]; then
  PEER=$(hcloud server list -o columns=name | awk -v hostname="$NODE_HOSTNAME" 'NR > 1 && $1 != hostname {print $1; exit}')
  [[ -n "$PEER" ]] || PEER="$NODE_HOSTNAME"
fi
validate_hostname "$PEER"

echo
echo "  Delete: $NODE_HOSTNAME"
echo "  Via:    $PEER"
echo
confirm "Drain and delete the server?" "$ASSUME_YES"

echo "Draining $NODE_HOSTNAME..."
if ! ssh_node "pi@$PEER" \
  sudo k3s kubectl drain "$NODE_HOSTNAME" \
  --ignore-daemonsets --delete-emptydir-data --force --disable-eviction --timeout=30s; then
  echo "Warning: could not drain $NODE_HOSTNAME" >&2
fi

echo "Removing $NODE_HOSTNAME from Kubernetes..."
if ! ssh_node "pi@$PEER" \
  sudo k3s kubectl delete node "$NODE_HOSTNAME" --wait=false; then
  echo "Warning: could not remove $NODE_HOSTNAME from Kubernetes" >&2
fi

echo "Deleting $NODE_HOSTNAME..."
hcloud server delete "$NODE_HOSTNAME"
echo "Done."
}

sync_command() {
[[ $# -gt 0 ]] || die "usage: nodes sync <hostname>..."
require_command ssh
require_command tar

KEYS_FILE="$SCRIPT_DIR/config/authorized_keys"
COMMON_ROOT="$SCRIPT_DIR/rootfs/common"
PHYSICAL_ROOT="$SCRIPT_DIR/rootfs/physical"
[[ -s "$KEYS_FILE" ]] || die "$KEYS_FILE is empty or missing"

for node in "$@"; do
  validate_hostname "$node"
  echo "==> $node"

  ssh_node "pi@$node" \
    'umask 077; mkdir -p ~/.ssh && cat > ~/.ssh/authorized_keys' < "$KEYS_FILE" \
    || die "failed writing authorized_keys on $node"

  COPYFILE_DISABLE=1 tar -C "$COMMON_ROOT" -cf - . \
    | ssh_node "pi@$node" 'sudo tar --extract --file=- --directory=/ --no-same-owner' \
    || die "failed syncing common files to $node"
  COPYFILE_DISABLE=1 tar -C "$PHYSICAL_ROOT" -cf - . \
    | ssh_node "pi@$node" 'sudo tar --extract --file=- --directory=/ --no-same-owner' \
    || die "failed syncing physical files to $node"

  ssh_node "pi@$node" 'sudo bash -euo pipefail -s' <<'REMOTE'
systemctl mask --now multipathd.service multipathd.socket >/dev/null 2>&1 || true
systemctl stop node-health.service >/dev/null 2>&1 || true
systemctl disable node-health.service >/dev/null 2>&1 || true
systemctl daemon-reload
sysctl --system >/dev/null
systemctl enable --now watchdog-load.service >/dev/null
systemctl daemon-reexec
systemctl enable --now node-health.timer >/dev/null
systemctl restart systemd-journald
systemctl restart rsyslog
journalctl --vacuum-size=256M >/dev/null
truncate -s 0 /var/log/syslog
find /var/log -maxdepth 1 -regextype posix-extended -regex '.*/(syslog|kern\.log)\.[0-9].*' -delete

[[ -e /dev/watchdog0 ]] || { echo "    no watchdog device"; exit 1; }

held=""
attempt=0
while [[ -z "$held" && $attempt -lt 10 ]]; do
  fds=$(ls -l /proc/1/fd 2>/dev/null || true)
  case "$fds" in
    *watchdog*) held=1 ;;
  esac
  [[ -n "$held" ]] || sleep 2
  attempt=$((attempt + 1))
done
[[ -n "$held" ]] || { echo "    watchdog not held by pid 1"; exit 1; }

multipath_service=$(systemctl is-enabled multipathd.service 2>/dev/null || true)
multipath_socket=$(systemctl is-enabled multipathd.socket 2>/dev/null || true)
echo "    multipathd: service=${multipath_service:-not-found}, socket=${multipath_socket:-not-found}"
echo "    watchdog: $(systemctl show -p RuntimeWatchdogUSec --value) via $(wdctl 2>/dev/null | awk '/Identity/ {print $2}')"
echo "    health: $(systemctl is-active node-health.timer)"
echo "    log usage: $(du -sh /var/log | cut -f1)"
REMOTE
done

echo "Done."
}
