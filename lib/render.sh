#!/usr/bin/env bash

NODES_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODES_PROJECT_DIR="$(cd "$NODES_LIB_DIR/.." && pwd)"
source "$NODES_LIB_DIR/common.sh"

append_file_entry() {
  local output="$1"
  local target="$2"
  local mode="$3"
  local source="$4"

  {
    printf '  - path: %s\n' "$target"
    printf '    permissions: "%s"\n' "$mode"
    printf '    owner: root:root\n'
    printf '    encoding: b64\n'
    printf '    content: '
    base64 < "$source" | tr -d '\n'
    printf '\n'
  } >> "$output"
}

append_text_entry() {
  local output="$1"
  local target="$2"
  local mode="$3"
  local content="$4"

  {
    printf '  - path: %s\n' "$target"
    printf '    permissions: "%s"\n' "$mode"
    printf '    owner: root:root\n'
    printf '    encoding: b64\n'
    printf '    content: '
    printf '%s' "$content" | base64 | tr -d '\n'
    printf '\n'
  } >> "$output"
}

render_authorized_keys() {
  local key=""
  local keys_file="$NODES_PROJECT_DIR/config/authorized_keys"

  [[ -s "$keys_file" ]] || die "$keys_file is empty or missing"
  while IFS= read -r key || [[ -n "$key" ]]; do
    [[ -n "$key" ]] || continue
    printf '      - %s\n' "$(yaml_quote "$key")"
  done < "$keys_file"
}

render_packages() {
  local platform="$1"
  local package=""
  local common_packages=(curl jq nfs-common nfs-kernel-server open-iscsi unattended-upgrades)
  local physical_packages=(bluez chrony iw linux-firmware-realtek wpasupplicant)

  for package in "${common_packages[@]}"; do
    printf '  - %s\n' "$package"
  done

  if [[ "$platform" == "physical" ]]; then
    for package in "${physical_packages[@]}"; do
      printf '  - %s\n' "$package"
    done
  fi
}

render_tree() {
  local output="$1"
  local tree="$2"
  local source=""
  local target=""
  local mode=""

  while IFS= read -r source; do
    target="${source#"$tree"}"
    mode="0644"
    [[ -x "$source" ]] && mode="0755"
    append_file_entry "$output" "$target" "$mode" "$source"
  done < <(find "$tree" -type f | LC_ALL=C sort)
}

render_write_files() {
  local output="$1"
  local platform="$2"
  local config="$3"

  render_tree "$output" "$NODES_PROJECT_DIR/rootfs/common"
  if [[ "$platform" == "physical" ]]; then
    render_tree "$output" "$NODES_PROJECT_DIR/rootfs/physical"
  fi
  append_text_entry "$output" "/etc/nodes/config" "0600" "$config"
}

render_user_data() {
  local output="$1"
  local node_hostname="$2"
  local role="$3"
  local platform="$4"
  local server_hostname="$5"
  local tailscale_authkey="$6"
  local github_token="$7"
  local template="$NODES_PROJECT_DIR/cloud-init/user-data.yaml"
  local line=""
  local config=""

  validate_hostname "$node_hostname"
  [[ "$role" == "init" || "$role" == "join" || "$role" == "agent" ]] \
    || die "role must be 'init', 'join' or 'agent'"
  [[ "$platform" == "physical" || "$platform" == "cloud" ]] \
    || die "platform must be 'physical' or 'cloud'"
  [[ -n "$tailscale_authkey" ]] || die "Tailscale auth key is required"

  if [[ "$role" == "init" ]]; then
    [[ -n "$github_token" ]] || die "GitHub token is required for the init role"
  else
    validate_hostname "$server_hostname"
  fi

  [[ -f "$template" ]] || die "$template not found"
  printf -v config 'NODE_HOSTNAME=%q\nNODE_ROLE=%q\nNODE_PLATFORM=%q\nSERVER_HOSTNAME=%q\nTAILSCALE_AUTHKEY=%q\nGITHUB_TOKEN=%q\n' \
    "$node_hostname" "$role" "$platform" "$server_hostname" "$tailscale_authkey" "$github_token"

  : > "$output"
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      'hostname: __HOSTNAME__')
        printf 'hostname: %s\n' "$(yaml_quote "$node_hostname")" >> "$output"
        ;;
      '__SSH_AUTHORIZED_KEYS__')
        render_authorized_keys >> "$output"
        ;;
      '__PACKAGES__')
        render_packages "$platform" >> "$output"
        ;;
      '__WRITE_FILES__')
        render_write_files "$output" "$platform" "$config"
        ;;
      *)
        printf '%s\n' "$line" >> "$output"
        ;;
    esac
  done < "$template"
}

render_network_config() {
  local output="$1"
  local wifi_ssid="$2"
  local wifi_password="$3"
  local template="$NODES_PROJECT_DIR/cloud-init/network.yaml"
  local line=""
  local password_length=${#wifi_password}

  [[ -n "$wifi_ssid" && ${#wifi_ssid} -le 32 ]] || die "Wi-Fi SSID must be between 1 and 32 characters"
  if [[ ! "$wifi_password" =~ ^[[:xdigit:]]{64}$ ]]; then
    [[ $password_length -ge 8 && $password_length -le 63 ]] \
      || die "Wi-Fi password must be 8-63 characters or a 64-digit PSK"
  fi

  : > "$output"
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      *'__WIFI_SSID__:'*)
        printf '        %s:\n' "$(yaml_quote "$wifi_ssid")" >> "$output"
        ;;
      *'password: __WIFI_PASSWORD__'*)
        printf '            password: %s\n' "$(yaml_quote "$wifi_password")" >> "$output"
        ;;
      *)
        printf '%s\n' "$line" >> "$output"
        ;;
    esac
  done < "$template"
}
