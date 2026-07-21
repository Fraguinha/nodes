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
render_network_config "$NETWORK_CONFIG" "$WIFI_SSID" "$WIFI_PASSWORD" "$NODE_HOSTNAME"
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

sync_health_only() {
  local common_root="$SCRIPT_DIR/rootfs/common"
  local asset node remote_script remote_command
  local -a assets=(
    usr/local/bin/node-health-check.sh
    usr/local/bin/node-wifi-recovery.sh
    etc/systemd/system/node-health.service
    etc/systemd/system/node-health.timer
  )

  for asset in "${assets[@]}"; do
    [[ -f "$common_root/$asset" && ! -L "$common_root/$asset" ]] \
      || die "health asset must be a regular file: $asset"
  done
  for asset in "${assets[0]}" "${assets[1]}"; do
    bash -n "$common_root/$asset" || die "invalid health script syntax: $asset"
  done

  IFS= read -r -d '' remote_script <<'HEALTH_REMOTE' || [[ -n "$remote_script" ]]
umask 077
cd /
for command_name in timeout tar sudo mktemp rm; do
  command -v "$command_name" >/dev/null
done
stage=""
cleanup_stage() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "$stage" ]] && ! timeout --kill-after=2s 10s rm -rf -- "$stage"; then
    printf 'Failed cleaning health upload staging: %s\n' "$stage" >&2
    status=1
  fi
  exit "$status"
}
trap cleanup_stage EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
stage=$(timeout --kill-after=2s 10s mktemp -d tmp/nodes-health.XXXXXXXXXX)
timeout --kill-after=2s 30s tar --extract --file=- --directory="$stage" \
  --no-same-owner --no-same-permissions -- \
  usr/local/bin/node-health-check.sh \
  usr/local/bin/node-wifi-recovery.sh \
  etc/systemd/system/node-health.service \
  etc/systemd/system/node-health.timer
sudo -n bash --noprofile --norc -euo pipefail -s -- "$stage" <<'HEALTH_INSTALL'
umask 077
cd /
stage="$1"
assets=(
  usr/local/bin/node-health-check.sh
  usr/local/bin/node-wifi-recovery.sh
  etc/systemd/system/node-health.service
  etc/systemd/system/node-health.timer
)
modes=(0755 0755 0644 0644)
work=""
temps=()
present=()
changed=0
timer_paused=0
timer_restore_attempted=0
bounded() {
  timeout --kill-after=2s 10s "$@"
}
wait_for_health() {
  local deadline=$((SECONDS + 65)) attempt=0 remaining service_state
  while :; do
    remaining=$((deadline - SECONDS))
    if [[ "$remaining" -le 0 || "$attempt" -ge 65 ]]; then
      printf 'Timed out waiting for node-health.service to finish naturally\n' >&2
      return 1
    fi
    [[ "$remaining" -le 5 ]] || remaining=5
    service_state=$(timeout --kill-after=2s "${remaining}s" \
      systemctl show --property=ActiveState --value node-health.service) || return
    case "$service_state" in
      inactive|failed) return 0 ;;
      active|activating|reloading|deactivating|refreshing) ;;
      *)
        printf 'Unexpected node-health.service state: %s\n' "$service_state" >&2
        return 1
        ;;
    esac
    sleep 1 || return
    attempt=$((attempt + 1))
  done
}
finish_health_sync() {
  local status=$? index asset target replacement
  local rollback_allowed=1
  trap - EXIT HUP INT TERM
  set +e
  if [[ "$status" -ne 0 && "$changed" -eq 1 && "$timer_restore_attempted" -eq 1 ]]; then
    timer_paused=1
    if ! bounded systemctl stop node-health.timer || ! wait_for_health; then
      printf 'Cannot safely roll back health files while health activation is uncertain; installed files retained\n' >&2
      rollback_allowed=0
    fi
  fi
  if [[ "$status" -ne 0 && "$changed" -eq 1 && "$rollback_allowed" -eq 1 ]]; then
    for index in "${!assets[@]}"; do
      asset="${assets[$index]}"
      target="$asset"
      if [[ "${present[$index]}" == 1 ]]; then
        replacement=$(bounded mktemp "$target.nodes-health.XXXXXXXXXX")
        if [[ $? -ne 0 || -z "$replacement" ]]; then
          printf 'Rollback failed preparing %s\n' "$target" >&2
          continue
        fi
        temps+=("$replacement")
        if ! bounded cp -a -- "$work/backup/${asset##*/}" "$replacement" \
          || ! bounded mv -fT -- "$replacement" "$target"; then
          printf 'Rollback failed restoring %s\n' "$target" >&2
        fi
      elif ! bounded rm -f -- "$target"; then
        printf 'Rollback failed removing new health asset %s\n' "$target" >&2
      fi
    done
    if ! bounded systemctl daemon-reload; then
      printf 'Rollback daemon-reload failed\n' >&2
    fi
  fi
  if [[ "$timer_paused" -eq 1 ]]; then
    if ! bounded systemctl start node-health.timer; then
      printf 'Failed restoring node-health.timer activity\n' >&2
      status=1
    fi
  fi
  for replacement in "${temps[@]}"; do
    if ! bounded rm -f -- "$replacement"; then
      printf 'Failed cleaning health candidate %s\n' "$replacement" >&2
      status=1
    fi
  done
  if [[ -n "$work" ]] && ! bounded rm -rf -- "$work"; then
    printf 'Failed cleaning health install staging: %s\n' "$work" >&2
    status=1
  fi
  exit "$status"
}
trap finish_health_sync EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
for command_name in timeout systemctl systemd-analyze install cp mv rm mktemp bash sleep; do
  command -v "$command_name" >/dev/null
done
work=$(bounded mktemp -d run/nodes-health.XXXXXXXXXX)
bounded install -d -m 0700 -- "$work/incoming" "$work/backup"
for index in "${!assets[@]}"; do
  asset="${assets[$index]}"
  [[ -f "$stage/$asset" && ! -L "$stage/$asset" ]] \
    || { printf 'Invalid staged health asset: %s\n' "$asset" >&2; exit 1; }
  bounded install -o root -g root -m "${modes[$index]}" -- \
    "$stage/$asset" "$work/incoming/${asset##*/}"
done
bounded bash -n "$work/incoming/node-health-check.sh"
bounded bash -n "$work/incoming/node-wifi-recovery.sh"
for index in "${!assets[@]}"; do
  asset="${assets[$index]}"
  target="$asset"
  [[ -d "${target%/*}" ]] \
    || { printf 'Missing health asset directory: %s\n' "${target%/*}" >&2; exit 1; }
  present[$index]=0
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" ]] \
      || { printf 'Health destination is not a regular file: %s\n' "$target" >&2; exit 1; }
    bounded cp -a -- "$target" "$work/backup/${asset##*/}"
    present[$index]=1
  fi
  replacement=$(bounded mktemp "$target.nodes-health.XXXXXXXXXX")
  temps+=("$replacement")
  bounded install -o root -g root -m "${modes[$index]}" -- \
    "$work/incoming/${asset##*/}" "$replacement"
done
timer_state=$(bounded systemctl show --property=ActiveState --value node-health.timer)
case "$timer_state" in
  active)
    timer_paused=1
    bounded systemctl stop node-health.timer
    ;;
  inactive|failed) ;;
  *)
    printf 'Cannot safely pause node-health.timer in state %s\n' "$timer_state" >&2
    exit 1
    ;;
esac
wait_for_health
changed=1
for index in 0 1; do
  bounded mv -fT -- "${temps[$index]}" "${assets[$index]}"
done
bounded systemd-analyze verify --man=no \
  "$work/incoming/node-health.service" "$work/incoming/node-health.timer"
for index in 2 3; do
  bounded mv -fT -- "${temps[$index]}" "${assets[$index]}"
done
bounded systemctl daemon-reload
if [[ "$timer_paused" -eq 1 ]]; then
  timer_restore_attempted=1
  bounded systemctl start node-health.timer
  timer_paused=0
fi
HEALTH_INSTALL
HEALTH_REMOTE
  printf -v remote_command '%q ' bash --noprofile --norc -euo pipefail -c "$remote_script"

  for node in "$@"; do
    echo "==> $node (health only)"
    COPYFILE_DISABLE=1 tar --format=ustar --no-xattrs --no-acls \
      -C "$common_root" -cf - -- "${assets[@]}" \
      | ssh_node "pi@$node" "$remote_command" \
      || die "health-only sync failed on $node"
  done
  echo "Done."
}

sync_full() {
  local platform="$1" check="$2" activate="$3"
  shift 3
  local common_root="$SCRIPT_DIR/rootfs/common"
  local physical_root="$SCRIPT_DIR/rootfs/physical"
  local asset source parent node index matched remote_script remote_command host_status result=0
  local -a common_assets=(
    usr/local/bin/node-health-check.sh
    usr/local/bin/node-wifi-recovery.sh
    usr/local/bin/watchdog-load.sh
    usr/local/bin/setup-node.sh
    usr/local/bin/node-configure.py
    usr/local/share/nodes/k3s-server-ha.yaml
    usr/local/share/nodes/node-policy.json
    etc/k3s-resolv.conf
    etc/chrony/conf.d/ip-fallback.conf
    etc/systemd/system/node-health.service
    etc/systemd/system/node-health.timer
    etc/systemd/system/watchdog-load.service
    etc/systemd/system/tailscaled.service.d/wait-for-time-sync.conf
    etc/systemd/system/tailscaled.service.d/10-oom.conf
    etc/systemd/system/k3s.service.d/restart-after-tailscale.conf
    etc/systemd/system/k3s-agent.service.d/restart-after-tailscale.conf
    etc/systemd/system/ssh.service.d/10-oom.conf
    etc/systemd/journald.conf.d/10-limits.conf
    etc/systemd/system.conf.d/10-watchdog.conf
    etc/rsyslog.d/10-k3s.conf
    etc/sysctl.d/99-tailscale.conf
  )
  local -a physical_assets=(
    etc/systemd/network/10-wlan0.link
    etc/udev/rules.d/70-wifi-powersave-off.rules
    etc/chrony/conf.d/ip-fallback.conf
  )
  local -a assets=("${common_assets[@]}")
  local -a asset_roots=()
  local -a tar_args=(--format=ustar --no-xattrs --no-acls -cf -)
  for asset in "${assets[@]}"; do
    asset_roots+=("$common_root")
  done
  if [[ "$platform" == physical ]]; then
    for asset in "${physical_assets[@]}"; do
      matched=0
      for index in "${!assets[@]}"; do
        if [[ "${assets[$index]}" == "$asset" ]]; then
          asset_roots[$index]="$physical_root"
          matched=1
          break
        fi
      done
      if [[ "$matched" -eq 0 ]]; then
        assets+=("$asset")
        asset_roots+=("$physical_root")
      fi
    done
  fi
  assets+=(authorized_keys)
  asset_roots+=("$SCRIPT_DIR/config")
  for index in "${!assets[@]}"; do
    asset="${assets[$index]}"
    source="${asset_roots[$index]}/$asset"
    [[ -f "$source" && ! -L "$source" ]] \
      || die "sync asset must be a regular file: $asset"
    parent="${source%/*}"
    while [[ "$parent" != "$SCRIPT_DIR" ]]; do
      [[ -d "$parent" && ! -L "$parent" ]] \
        || die "sync asset directory must not be a symlink: $asset"
      parent="${parent%/*}"
    done
    case "$asset" in
      usr/local/bin/*.sh) bash -n "$source" || die "invalid script syntax: $asset" ;;
      authorized_keys) [[ -s "$source" ]] || die "$source is empty" ;;
    esac
    tar_args+=(-C "${asset_roots[$index]}" "$asset")
  done
  IFS= read -r -d '' remote_script <<'CONFIG_REMOTE' || [[ -n "$remote_script" ]]
umask 077
cd /
platform="$1"
check="$2"
activate="$3"
expected_host="$4"
shift 4
assets=("$@")
for command_name in timeout tar sudo mktemp rm; do
  command -v "$command_name" >/dev/null
done
stage=""
cleanup_upload() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "$stage" ]] && ! timeout --kill-after=2s 10s rm -rf -- "$stage"; then
    printf 'Failed cleaning configuration upload staging: %s\n' "$stage" >&2
    status=1
  fi
  exit "$status"
}
trap cleanup_upload EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
stage=$(timeout --kill-after=2s 10s mktemp -d tmp/nodes-config.XXXXXXXXXX)
timeout --kill-after=2s 30s tar --extract --file=- --directory="$stage" \
  --no-same-owner --no-same-permissions -- "${assets[@]}"
sudo -n bash --noprofile --norc -euo pipefail -s -- "$stage" "$platform" "$check" "$activate" "$expected_host" \
  "${assets[@]}" <<'CONFIG_INSTALL'
umask 077
cd /
stage="$1"
platform="$2"
check="$3"
activate="$4"
expected_host="$5"
shift 5
assets=("$@")
export PYTHONDONTWRITEBYTECODE=1
work=""
temps=()
chrony_context_temps=()
present=()
changed_indices=()
applied=()
created_dirs=()
committed=0
files_touched=0
timer_restore_attempted=0
timer_paused=0
bounded() {
  timeout --kill-after=2s 10s "$@"
}
authorized_keys_match() {
  bounded python3 - "$@" <<'AUTHORIZED_KEYS'
import base64
import binascii
from pathlib import Path
import re
import sys

SPACE = " \t\r\v\f"

def field(line, offset):
    while offset < len(line) and line[offset] in SPACE:
        offset += 1
    start = offset
    quoted = False
    while offset < len(line):
        char = line[offset]
        if quoted and char == "\\" and offset + 1 < len(line) and line[offset + 1] == '"':
            offset += 2
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted and char in SPACE:
            break
        offset += 1
    if quoted or offset == start:
        raise ValueError
    return line[start:offset], offset

def options(text):
    parts = []
    start = offset = 0
    quoted = False
    while offset < len(text):
        char = text[offset]
        if quoted and char == "\\" and offset + 1 < len(text) and text[offset + 1] == '"':
            offset += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            parts.append(text[start:offset])
            start = offset + 1
        offset += 1
    if quoted:
        raise ValueError
    parts.append(text[start:])
    result = []
    for part in parts:
        name, equals, value = part.partition("=")
        if not re.fullmatch(r"[a-zA-Z0-9-]+", name):
            raise ValueError
        quoted_value = value.startswith('"')
        if quoted_value:
            if len(value) < 2 or not value.endswith('"'):
                raise ValueError
            decoded = []
            offset = 1
            while offset < len(value) - 1:
                char = value[offset]
                if char == "\\" and offset + 1 < len(value) - 1 and value[offset + 1] == '"':
                    decoded.append('"')
                    offset += 2
                    continue
                if char == '"':
                    raise ValueError
                decoded.append(char)
                offset += 1
            value = "".join(decoded)
        elif '"' in value:
            raise ValueError
        result.append((name.lower(), bool(equals), quoted_value, value))
    return tuple(result)

def key_blob(kind, encoded):
    try:
        blob = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        length = int.from_bytes(blob[:4], "big")
        if length <= 0 or length > len(blob) - 4:
            return None
        if blob[4:4 + length].decode("ascii") != kind:
            return None
        return blob
    except (binascii.Error, ValueError, UnicodeError):
        return None

def keys(path):
    result = {}
    for line in Path(path).read_bytes().decode("utf-8", "surrogateescape").split("\n"):
        if not line.strip(SPACE) or line.lstrip(SPACE).startswith("#"):
            continue
        first, offset = field(line, 0)
        second, offset = field(line, offset)
        blob = key_blob(first, second)
        restrictions = ()
        kind = first
        if blob is None:
            restrictions = options(first)
            third, offset = field(line, offset)
            kind = second
            blob = key_blob(kind, third)
            if blob is None:
                raise ValueError
        result.setdefault((kind, blob), []).append(restrictions)
    if not result:
        raise ValueError
    return result

try:
    desired = keys(sys.argv[1])
except (OSError, ValueError):
    print("Invalid desired authorized_keys", file=sys.stderr)
    raise SystemExit(2)
if len(sys.argv) == 2:
    raise SystemExit(0)
try:
    actual = keys(sys.argv[2])
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if desired == actual else 1)
AUTHORIZED_KEYS
}
chrony_context_io() {
  bounded python3 - "$@" <<'CHRONY_CONTEXT'
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

pending = Path("var/lib/nodes/chrony-pending")
snapshot_name = re.compile(r"[0-9a-f]{64}\.conf")
temporary_name = re.compile(r"\.snapshot-[A-Za-z0-9_]+\.tmp")

def regular(path, writable=False):
    descriptor = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2 * 1024 * 1024:
        os.close(descriptor)
        raise ValueError
    return os.fdopen(descriptor, "rb")

def fingerprint(path):
    digest = hashlib.sha256()
    with regular(path) as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()

def same_bytes(first, second):
    with regular(first) as left, regular(second) as right:
        while True:
            chunk = left.read(65536)
            if chunk != right.read(65536):
                return False
            if not chunk:
                return True

def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

try:
    operation = sys.argv[1]
    if operation == "--chrony-temp" and len(sys.argv) == 2:
        descriptor, filename = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=pending)
        os.close(descriptor)
        print(os.path.relpath(filename))
    elif operation == "--chrony-validate" and len(sys.argv) == 2:
        for path in pending.iterdir():
            if snapshot_name.fullmatch(path.name):
                if fingerprint(path) + ".conf" != path.name:
                    raise ValueError
            elif temporary_name.fullmatch(path.name):
                with regular(path):
                    pass
            else:
                raise ValueError
    elif operation == "--chrony-publish" and len(sys.argv) == 3:
        candidate = Path(sys.argv[2])
        if candidate.parent != pending or not temporary_name.fullmatch(candidate.name):
            raise ValueError
        digest = fingerprint(candidate)
        with regular(candidate, writable=True) as source:
            os.fsync(source.fileno())
        destination = pending / (digest + ".conf")
        try:
            os.link(candidate, destination, follow_symlinks=False)
        except FileExistsError:
            if not same_bytes(candidate, destination):
                raise ValueError
        for path in (pending, pending.parent, pending.parent.parent):
            sync_directory(path)
        candidate.unlink()
        sync_directory(pending)
    else:
        raise ValueError
except (OSError, ValueError):
    print("Durable chrony ownership context is unsafe or could not be saved", file=sys.stderr)
    raise SystemExit(1)
CHRONY_CONTEXT
}
chrony_path_safe() {
  local path="$1" kind="$2" exact_mode="$3" metadata permissions
  [[ ! -L "$path" ]] || return 1
  case "$kind" in
    directory) [[ -d "$path" ]] || return 1 ;;
    file) [[ -f "$path" ]] || return 1 ;;
  esac
  metadata=$(bounded stat -c '%a:%u:%g' -- "$path" 2>"$work/chrony-context-error") || return 1
  [[ "${metadata#*:}" == 0:0 ]] || return 1
  permissions="${metadata%%:*}"
  [[ "$permissions" =~ ^[0-7]{1,4}$ ]] || return 1
  (( (8#$permissions & 0022) == 0 )) || return 1
  [[ -z "$exact_mode" || "$permissions" == "$exact_mode" ]]
}
validate_chrony_context() {
  local path entry name
  for path in var var/lib; do
    chrony_path_safe "$path" directory "" || return 1
  done
  if [[ -e var/lib/nodes || -L var/lib/nodes ]]; then
    chrony_path_safe var/lib/nodes directory "" || return 1
  fi
  if [[ -e var/lib/nodes/chrony-pending || -L var/lib/nodes/chrony-pending ]]; then
    chrony_path_safe var/lib/nodes/chrony-pending directory 700 || return 1
    for entry in var/lib/nodes/chrony-pending/* \
      var/lib/nodes/chrony-pending/.[!.]* var/lib/nodes/chrony-pending/..?*; do
      [[ -e "$entry" || -L "$entry" ]] || continue
      name="${entry##*/}"
      [[ "$name" =~ ^[0-9a-f]{64}[.]conf$ || "$name" =~ ^[.]snapshot-[A-Za-z0-9_]+[.]tmp$ ]] \
        || return 1
      chrony_path_safe "$entry" file 600 || return 1
    done
    chrony_context_io --chrony-validate || return 1
  fi
  return 0
}
persist_chrony_context() {
  local capture="$1" path candidate
  if ! validate_chrony_context; then
    printf 'Unsafe durable chrony ownership context\n' >&2
    return 1
  fi
  [[ "$capture" -eq 1 ]] || return 0
  for path in etc etc/chrony etc/chrony/conf.d; do
    if ! chrony_path_safe "$path" directory ""; then
      printf 'Unsafe managed chrony policy path\n' >&2
      return 1
    fi
  done
  if ! chrony_path_safe etc/chrony/conf.d/ip-fallback.conf file ""; then
    printf 'Unsafe managed chrony policy file\n' >&2
    return 1
  fi
  for path in var/lib/nodes var/lib/nodes/chrony-pending; do
    if [[ ! -d "$path" ]]; then
      bounded install -d -o root -g root -m 0700 -- "$path"
    fi
  done
  candidate=$(chrony_context_io --chrony-temp)
  [[ "$candidate" == var/lib/nodes/chrony-pending/.snapshot-*.tmp ]] \
    || { printf 'Invalid durable chrony candidate\n' >&2; return 1; }
  chrony_context_temps+=("$candidate")
  if ! bounded install -o root -g root -m 0600 -- \
    etc/chrony/conf.d/ip-fallback.conf "$candidate" 2>"$work/chrony-context-error" \
    || ! chrony_path_safe "$candidate" file 600; then
    printf 'Failed preparing durable chrony ownership context\n' >&2
    return 1
  fi
  chrony_context_io --chrony-publish "$candidate" || return 1
  if ! validate_chrony_context; then
    printf 'Unsafe durable chrony ownership context after publication\n' >&2
    return 1
  fi
}
destination() {
  target="$1"
  mode=0644
  owner=root
  group=root
  expected_uid=0
  expected_gid=0
  case "$1" in
    usr/local/bin/*) mode=0755 ;;
    authorized_keys)
      target=home/pi/.ssh/authorized_keys
      mode=0600
      owner=pi
      group="$pi_gid"
      expected_uid="$pi_uid"
      expected_gid="$pi_gid"
      ;;
  esac
}
protected_ssh_asset() {
  case "$1" in
    authorized_keys|etc/systemd/system/ssh.service.d/10-oom.conf) return 0 ;;
    *) return 1 ;;
  esac
}
validate_parents() {
  local rest="${1%/*}" part current=""
  while [[ -n "$rest" ]]; do
    part="${rest%%/*}"
    current="${current:+$current/}$part"
    [[ ! -L "$current" ]] \
      || { printf 'Unsafe destination directory: /%s\n' "$current" >&2; return 1; }
    if [[ -e "$current" && ! -d "$current" ]]; then
      printf 'Destination parent is not a directory: /%s\n' "$current" >&2
      return 1
    fi
    [[ "$rest" == */* ]] || break
    rest="${rest#*/}"
  done
}
prepare_parents() {
  local rest="${1%/*}" part current=""
  while [[ -n "$rest" ]]; do
    part="${rest%%/*}"
    current="${current:+$current/}$part"
    if [[ ! -d "$current" ]]; then
      created_dirs+=("$current")
      bounded install -d -o root -g root -m 0755 -- "$current"
    fi
    [[ "$rest" == */* ]] || break
    rest="${rest#*/}"
  done
}
wait_for_health() {
  local deadline=$((SECONDS + 65)) attempt=0 remaining service_state
  while :; do
    remaining=$((deadline - SECONDS))
    if [[ "$remaining" -le 0 || "$attempt" -ge 65 ]]; then
      printf 'Timed out waiting for node-health.service to finish naturally\n' >&2
      return 1
    fi
    [[ "$remaining" -le 5 ]] || remaining=5
    service_state=$(timeout --kill-after=2s "${remaining}s" \
      systemctl show --property=ActiveState --value node-health.service) || return
    case "$service_state" in
      inactive|failed) return 0 ;;
      active|activating|reloading|deactivating|refreshing) ;;
      *) printf 'Unexpected node-health.service state\n' >&2; return 1 ;;
    esac
    sleep 1 || return
    attempt=$((attempt + 1))
  done
}
finish_config_sync() {
  local status=$? position index asset target replacement mode owner group expected_uid expected_gid
  local rollback_allowed=1
  trap - EXIT HUP INT TERM
  set +e
  if [[ "$status" -ne 0 && "$committed" -eq 0 && "$files_touched" -eq 1 \
    && "$timer_restore_attempted" -eq 1 ]]; then
    timer_paused=1
    if ! bounded systemctl stop node-health.timer || ! wait_for_health; then
      printf 'Cannot safely roll back files while health activation is uncertain\n' >&2
      rollback_allowed=0
    fi
  fi
  if [[ "$status" -ne 0 && "$committed" -eq 0 && "$rollback_allowed" -eq 1 ]]; then
    for ((position=${#applied[@]} - 1; position >= 0; position--)); do
      index="${applied[$position]}"
      asset="${assets[$index]}"
      destination "$asset"
      if [[ "${present[$index]}" == 1 ]]; then
        replacement=$(bounded mktemp "$target.nodes-config.XXXXXXXXXX")
        if [[ $? -ne 0 || -z "$replacement" ]]; then
          printf 'Rollback failed preparing /%s\n' "$target" >&2
          continue
        fi
        temps+=("$replacement")
        if ! bounded cp -a -- "$work/backup/$index" "$replacement" \
          || ! bounded mv -fT -- "$replacement" "$target"; then
          printf 'Rollback failed restoring /%s\n' "$target" >&2
        fi
      elif ! bounded rm -f -- "$target"; then
        printf 'Rollback failed removing new asset /%s\n' "$target" >&2
      fi
    done
    if [[ "$files_touched" -eq 1 ]] && ! bounded systemctl daemon-reload; then
      printf 'Rollback daemon-reload failed\n' >&2
    fi
  fi
  if [[ "$timer_paused" -eq 1 ]] && ! bounded systemctl start node-health.timer; then
    printf 'Failed restoring node-health.timer activity\n' >&2
    status=1
  fi
  for replacement in "${temps[@]}"; do
    if ! bounded rm -f -- "$replacement"; then
      printf 'Failed cleaning configuration candidate %s\n' "$replacement" >&2
      status=1
    fi
  done
  for replacement in "${chrony_context_temps[@]}"; do
    if ! bounded rm -f -- "$replacement" 2>"$work/chrony-context-error"; then
      printf 'Failed cleaning temporary chrony ownership candidate\n' >&2
      status=1
    fi
  done
  if [[ "$status" -ne 0 && "$committed" -eq 0 && "$rollback_allowed" -eq 1 ]]; then
    for ((position=${#created_dirs[@]} - 1; position >= 0; position--)); do
      if ! bounded rmdir -- "${created_dirs[$position]}"; then
        printf 'Rollback left nonempty directory /%s\n' "${created_dirs[$position]}" >&2
      fi
    done
  fi
  if [[ -n "$work" ]] && ! bounded rm -rf -- "$work"; then
    printf 'Failed cleaning configuration install staging: %s\n' "$work" >&2
    status=1
  fi
  exit "$status"
}
trap finish_config_sync EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
for command_name in timeout systemctl systemd-analyze install cp mv rm rmdir \
  mktemp bash python3 sleep stat cmp hostname id getent; do
  command -v "$command_name" >/dev/null
done
os_id=""
while IFS='=' read -r key value; do
  [[ "$key" != ID ]] || os_id="$value"
done < etc/os-release
[[ "$os_id" == ubuntu || "$os_id" == '"ubuntu"' ]] \
  || { printf 'Unsupported remote OS: Ubuntu required\n' >&2; exit 1; }
[[ "$(bounded hostname -s)" == "$expected_host" ]] \
  || { printf 'Remote hostname does not match requested host\n' >&2; exit 1; }
role=""
for unit in k3s.service k3s-agent.service; do
  load_state=$(bounded systemctl show --property=LoadState --value "$unit") \
    || { [[ "$load_state" == not-found ]] \
      || { printf 'Cannot safely determine installed k3s role\n' >&2; exit 1; }; }
  case "$load_state" in
    loaded)
      [[ -z "$role" ]] \
        || { printf 'Ambiguous installed k3s role\n' >&2; exit 1; }
      role=server
      [[ "$unit" != k3s-agent.service ]] || role=agent
      ;;
    not-found) ;;
    *) printf 'Cannot safely determine installed k3s role\n' >&2; exit 1 ;;
  esac
done
[[ -n "$role" ]] || { printf 'No installed k3s role found\n' >&2; exit 1; }
pi_uid=$(bounded id -u pi)
pi_gid=$(bounded id -g pi)
[[ "$pi_uid" =~ ^[0-9]+$ && "$pi_gid" =~ ^[0-9]+$ && "$pi_uid" != 0 ]] \
  || { printf 'Invalid pi account identity\n' >&2; exit 1; }
IFS=: read -r account password account_uid account_gid gecos account_home account_shell \
  <<< "$(bounded getent passwd pi)"
[[ "$account" == pi && "$account_uid" == "$pi_uid" && "$account_gid" == "$pi_gid" \
  && "$account_home" == /home/pi && -d home/pi && ! -L home/pi ]] \
  || { printf 'Unexpected pi home or account identity\n' >&2; exit 1; }
for asset in "${assets[@]}"; do
  destination "$asset"
  if protected_ssh_asset "$asset"; then
    continue
  fi
  validate_parents "$target"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" ]] \
      || { printf 'Destination is not a regular file: /%s\n' "$target" >&2; exit 1; }
  fi
done
work=$(bounded mktemp -d run/nodes-config.XXXXXXXXXX)
bounded install -d -m 0700 -- "$work/desired" "$work/backup"
for asset in "${assets[@]}"; do
  [[ -f "$stage/$asset" && ! -L "$stage/$asset" ]] \
    || { printf 'Invalid staged configuration asset: %s\n' "$asset" >&2; exit 1; }
  destination "$asset"
  if [[ "$asset" == */* ]]; then
    bounded install -d -m 0700 -- "$work/desired/${asset%/*}"
  fi
  bounded install -o "$owner" -g "$group" -m "$mode" -- \
    "$stage/$asset" "$work/desired/$asset"
  case "$asset" in
    usr/local/bin/*.sh) bounded bash -n "$work/desired/$asset" ;;
    etc/systemd/*)
      bounded python3 -c \
        'import configparser,sys; p=configparser.ConfigParser(strict=False,interpolation=None); p.read_file(open(sys.argv[1]))' \
        "$work/desired/$asset"
      ;;
  esac
done
bounded python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' \
  "$work/desired/usr/local/bin/node-configure.py"
bounded python3 -c 'import json,sys; json.load(open(sys.argv[1]))' \
  "$work/desired/usr/local/share/nodes/node-policy.json"
authorized_keys_match "$work/desired/authorized_keys" || exit 1
file_drift=0
ssh_pending=0
for index in "${!assets[@]}"; do
  asset="${assets[$index]}"
  destination "$asset"
  present[$index]=0
  reason=missing
  is_protected=0
  if protected_ssh_asset "$asset"; then
    is_protected=1
  fi
  if [[ "$is_protected" -eq 1 ]] && ! validate_parents "$target" >/dev/null 2>&1; then
    reason=unsafe-parent
  elif [[ -L "$target" || ( -e "$target" && ! -f "$target" ) ]]; then
    reason=not-regular
    if [[ "$is_protected" -ne 1 ]]; then
      printf 'Destination is not a regular file: /%s\n' "$target" >&2
      exit 1
    fi
  elif [[ -f "$target" ]]; then
    present[$index]=1
    reason=content
    content_matches=0
    if [[ "$asset" == authorized_keys ]]; then
      key_status=0
      authorized_keys_match "$work/desired/$asset" "$target" || key_status=$?
      [[ "$key_status" -le 1 ]] || exit 1
      if [[ "$key_status" -eq 0 ]]; then
        content_matches=1
      fi
    elif bounded cmp -s -- "$work/desired/$asset" "$target"; then
      content_matches=1
    fi
    if [[ "$content_matches" -eq 1 ]]; then
      reason=mode/owner
      if [[ "$(bounded stat -c '%a:%u:%g' -- "$target")" == "${mode#0}:$expected_uid:$expected_gid" ]]; then
        printf 'match /%s\n' "$target"
        continue
      fi
    fi
  fi
  if [[ "$is_protected" -eq 1 ]]; then
    printf 'pending /%s (%s; protected SSH configuration)\n' "$target" "$reason"
    ssh_pending=1
  else
    printf 'drift /%s (%s)\n' "$target" "$reason"
    file_drift=1
    changed_indices+=("$index")
  fi
done
if validate_parents home/pi/.ssh/authorized_keys >/dev/null 2>&1 \
  && [[ -d home/pi/.ssh && ! -L home/pi/.ssh ]] \
  && [[ "$(bounded stat -c '%a:%u:%g' -- home/pi/.ssh)" == "700:$pi_uid:$pi_gid" ]]; then
  printf 'match /home/pi/.ssh\n'
else
  printf 'pending /home/pi/.ssh (metadata/type or missing; protected SSH configuration)\n'
  ssh_pending=1
fi
verify_units() {
  if ! bounded systemd-analyze verify --man=no \
    "$work/desired/etc/systemd/system/node-health.service" \
    "$work/desired/etc/systemd/system/node-health.timer" \
    "$work/desired/etc/systemd/system/watchdog-load.service" > "$work/unit-validation" 2>&1; then
    printf 'Invalid staged node units\n' >&2
    return 1
  fi
}
if [[ "$check" == 1 ]]; then
  validation_status=0
  verify_units || validation_status=1
  runtime_status=0
  python3 "$work/desired/usr/local/bin/node-configure.py" \
    --check --platform "$platform" --role "$role" --source-root "/$work/desired" \
    || runtime_status=$?
  if [[ "$file_drift" -ne 0 || "$validation_status" -ne 0 ]]; then
    exit 1
  fi
  if [[ "$runtime_status" -eq 0 && "$ssh_pending" -eq 1 ]]; then
    exit 2
  fi
  exit "$runtime_status"
fi
chrony_capture=0
for index in "${changed_indices[@]}"; do
  if [[ "${assets[$index]}" == etc/chrony/conf.d/ip-fallback.conf \
    && "${present[$index]}" == 1 ]]; then
    chrony_capture=1
  fi
done
for index in "${changed_indices[@]}"; do
  asset="${assets[$index]}"
  destination "$asset"
  if [[ "${present[$index]}" == 1 ]]; then
    bounded cp -a -- "$target" "$work/backup/$index"
  fi
  prepare_parents "$target"
  replacement=$(bounded mktemp "$target.nodes-config.XXXXXXXXXX")
  temps[$index]="$replacement"
  bounded install -o "$owner" -g "$group" -m "$mode" -- \
    "$work/desired/$asset" "$replacement"
done
if [[ "$file_drift" -ne 0 ]]; then
  timer_state=$(bounded systemctl show --property=ActiveState --value node-health.timer)
  case "$timer_state" in
    active)
      timer_paused=1
      bounded systemctl stop node-health.timer
      ;;
    inactive|failed) ;;
    *) printf 'Cannot safely pause node-health.timer\n' >&2; exit 1 ;;
  esac
  wait_for_health
fi
persist_chrony_context "$chrony_capture"
for index in "${changed_indices[@]}"; do
  asset="${assets[$index]}"
  if [[ "$asset" == usr/local/bin/* ]]; then
    destination "$asset"
    applied+=("$index")
    files_touched=1
    bounded mv -fT -- "${temps[$index]}" "$target"
  fi
done
verify_units
for index in "${changed_indices[@]}"; do
  asset="${assets[$index]}"
  if [[ "$asset" != usr/local/bin/* ]]; then
    destination "$asset"
    applied+=("$index")
    files_touched=1
    bounded mv -fT -- "${temps[$index]}" "$target"
  fi
done
if [[ "$files_touched" -eq 1 ]]; then
  bounded systemctl daemon-reload
fi
if [[ "$timer_paused" -eq 1 ]]; then
  timer_restore_attempted=1
  bounded systemctl start node-health.timer
  timer_paused=0
fi
committed=1
printf 'Non-SSH files committed; reconciling runtime policy\n'
activation_args=()
if [[ "$activate" == 1 ]]; then
  activation_args=(--activate)
fi
runtime_status=0
python3 usr/local/bin/node-configure.py --platform "$platform" --role "$role" \
  "${activation_args[@]}" || runtime_status=$?
if [[ "$ssh_pending" -eq 1 ]]; then
  printf 'SSH configuration pending; protected targets unchanged\n' >&2
  if [[ "$runtime_status" -eq 0 ]]; then
    runtime_status=2
  fi
fi
case "$runtime_status" in
  0) printf 'Runtime policy matches\n' ;;
  2) printf 'Runtime policy pending; committed files retained\n' >&2 ;;
  *) printf 'Runtime policy failed; committed files retained\n' >&2; runtime_status=1 ;;
esac
exit "$runtime_status"
CONFIG_INSTALL
CONFIG_REMOTE
  for node in "$@"; do
    printf -v remote_command '%q ' bash --noprofile --norc -euo pipefail -c "$remote_script" -- \
      "$platform" "$check" "$activate" "$node" "${assets[@]}"
    echo "==> $node ($platform)"
    if COPYFILE_DISABLE=1 tar "${tar_args[@]}" | ssh_node "pi@$node" "$remote_command"; then
      :
    else
      host_status=$?
      printf 'Configuration %s on %s (status %s)\n' \
        "$([[ "$check" == 1 ]] && printf check-incomplete || printf sync-incomplete)" \
        "$node" "$host_status" >&2
      if [[ "$host_status" == 2 && "$result" != 1 ]]; then
        result=2
      else
        result=1
      fi
    fi
  done
  if [[ "$result" -eq 0 ]]; then
    echo "Done."
  fi
  return "$result"
}

sync_command() {
  local health_only=0 check=0 activate=0 platform="" hostname_count=0 node
  local -a hostnames=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --health-only)
        health_only=1
        shift
        ;;
      --platform)
        [[ $# -ge 2 ]] || die "--platform requires physical or cloud"
        platform="$2"
        [[ "$platform" == physical || "$platform" == cloud ]] \
          || die "platform must be 'physical' or 'cloud'"
        shift 2
        ;;
      --check)
        check=1
        shift
        ;;
      --activate)
        activate=1
        shift
        ;;
      --help|-h)
        cat <<'EOF'
Usage: nodes sync --platform physical|cloud [--check|--activate] HOSTNAME...
       nodes sync --health-only HOSTNAME...

  --platform     Select the authoritative physical or cloud configuration
  --check        Report file and effective runtime drift without applying changes
  --activate     Explicitly allow disruptive runtime activation
  --health-only  Install only health scripts and units; preserve timer enablement
  --help         Show this help

SSH configuration and authorized keys are verify-only, including with --activate.

NODES_SSH_CONFIG optionally selects an SSH config with strict known-host checking.
EOF
        return
        ;;
      --*)
        die "unknown option '$1'"
        ;;
      *)
        hostnames+=("$1")
        hostname_count=$((hostname_count + 1))
        shift
        ;;
    esac
  done
  [[ "$hostname_count" -gt 0 ]] \
    || die "usage: nodes sync --platform physical|cloud [--check|--activate] HOSTNAME..."
  [[ "$check" -eq 0 || "$activate" -eq 0 ]] \
    || die "--check and --activate cannot be combined"
  if [[ "$health_only" -eq 1 ]]; then
    [[ -z "$platform" && "$check" -eq 0 && "$activate" -eq 0 ]] \
      || die "--health-only cannot be combined with --platform, --check or --activate"
  else
    [[ -n "$platform" ]] || die "full sync and check require --platform physical|cloud"
  fi
  for node in "${hostnames[@]}"; do
    validate_hostname "$node"
  done
  require_command ssh
  require_command tar
  if [[ "$health_only" -eq 1 ]]; then
    sync_health_only "${hostnames[@]}"
  else
    sync_full "$platform" "$check" "$activate" "${hostnames[@]}"
  fi
}
