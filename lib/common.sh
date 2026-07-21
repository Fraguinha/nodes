#!/usr/bin/env bash

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  local command_name="$1"
  local hint="${2:-}"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    if [[ -n "$hint" ]]; then
      die "$command_name not found ($hint)"
    fi
    die "$command_name not found"
  fi
}

validate_hostname() {
  local value="$1"

  [[ ${#value} -le 63 && "$value" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] \
    || die "invalid hostname '$value'"
}

confirm() {
  local prompt="$1"
  local assume_yes="${2:-0}"
  local answer=""

  if [[ "$assume_yes" == "1" ]]; then
    return
  fi

  read -r -p "$prompt [y/N]: " answer
  [[ "$answer" == [yY] ]] || { echo "Aborted."; exit 1; }
}

pass_value() {
  local path="$1"
  local value=""

  value=$(pass show "$path" 2>/dev/null | head -n 1) || return 1
  [[ -n "$value" ]] || return 1
  printf '%s' "$value"
}

yaml_quote() {
  local value="$1"

  [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] \
    || die "YAML values cannot contain control characters"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '"%s"' "$value"
}

ssh_node() {
  ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new \
    "$@"
}
