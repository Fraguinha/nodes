#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != --bounded ]]; then
  [[ $# == 0 ]] || exit 1
  exec timeout --kill-after=8s 20s "$0" --bounded
fi
[[ $# == 1 ]] || exit 1

RUNTIME_DIR=${NODE_WIFI_RUNTIME_DIR:-/run/node-health/wifi-recovery}
PERSIST_DIR=${NODE_WIFI_PERSIST_DIR:-/var/lib/node-health/wifi-recovery}
SYSFS_DIR=${NODE_WIFI_SYSFS_DIR:-/sys/class/net}
UPTIME_FILE=${NODE_WIFI_UPTIME_FILE:-/proc/uptime}
PENDING_STATE=$RUNTIME_DIR/recovery
LINK_UP_STATE=$RUNTIME_DIR/link-needs-up
REBOOT_BUDGET=$PERSIST_DIR/reboot-budget
WIFI_UNIT=netplan-wpa-wlan0.service
LINK_NEEDS_UP=0

log() {
  timeout --kill-after=1s 1s logger -t node-health "$*" || true
}

restore_link() {
  ((LINK_NEEDS_UP)) || return 0
  timeout --kill-after=1s 2s ip link set dev wlan0 up >/dev/null 2>&1 || return 1
  rm -f -- "$LINK_UP_STATE" || return 1
  LINK_NEEDS_UP=0
}

trap 'restore_link || true' EXIT
trap 'exit 1' INT TERM

monotonic_now() {
  local uptime ignored

  read -r uptime ignored < "$UPTIME_FILE" || return 1
  [[ "$uptime" =~ ^[0-9]{1,12}(\.[0-9]+)?$ ]] || return 1
  printf '%s\n' "$((10#${uptime%%.*}))"
}

save_pending() {
  local temp

  temp=$(mktemp "$RUNTIME_DIR/recovery.XXXXXX") || return 1
  (
    trap 'rm -f -- "$temp"' EXIT
    printf 'v1 %s %s %s %s\n' "$1" "$2" "$3" "$4" > "$temp" &&
      mv -f -- "$temp" "$PENDING_STATE"
  )
}

read_record() {
  local line extra=

  {
    IFS= read -r line || return 1
    if IFS= read -r extra || [[ -n "$extra" ]]; then
      return 1
    fi
  } < "$1"
  printf '%s\n' "$line"
}

clear_pending() {
  if [[ -e "$PENDING_STATE" ]]; then
    rm -f -- "$PENDING_STATE" || return 1
    log "wlan0 underlay recovered or a usable wired uplink is available; clearing Wi-Fi recovery"
  fi
}

interface_works() {
  local iface=$1
  local gateway=$2
  local addresses neighbors

  [[ "$gateway" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  addresses=$(timeout --kill-after=1s 1s ip -j -4 address show dev "$iface" scope global 2>/dev/null) || return 1
  jq -e 'any(.[].addr_info[]?; .family == "inet" and .scope == "global" and (.local | test("^(169\\.254\\.|127\\.)") | not))' \
    <<< "$addresses" >/dev/null 2>&1 || return 1

  if timeout --kill-after=1s 2s ping -n -I "$iface" -c1 -W1 "$gateway" >/dev/null 2>&1; then
    return 0
  fi

  neighbors=$(timeout --kill-after=1s 1s ip -j -4 neigh show to "$gateway" dev "$iface" 2>/dev/null || true)
  if jq -e 'any(.[]; (.state // []) | index("REACHABLE") != null)' \
    <<< "$neighbors" >/dev/null 2>&1; then
    return 0
  fi

  timeout --kill-after=1s 2s curl --silent --output /dev/null --noproxy '*' \
    --interface "$iface" --connect-timeout 1 --max-time 2 http://1.1.1.1/ >/dev/null 2>&1
}

persist_budget() {
  local temp

  install -d -m 0700 "$PERSIST_DIR" || return 1
  temp=$(mktemp "$PERSIST_DIR/reboot-budget.XXXXXX") || return 1
  (
    trap 'rm -f -- "$temp"' EXIT
    printf 'v1 %s %s\n' "$1" "$2" > "$temp" &&
      timeout --kill-after=1s 2s sync -f "$temp" &&
      mv -f -- "$temp" "$REBOOT_BUDGET" &&
      timeout --kill-after=1s 2s sync -f "$PERSIST_DIR"
  )
}

reserve_reboot() {
  local now record last_seen=0 last_reboot=0

  if ! timeout --kill-after=1s 2s chronyc waitsync 1 0.1 0.0 1 >/dev/null 2>&1; then
    log "wlan0 recovery exhausted; refusing reboot without synchronized time within the correction limit"
    return 1
  fi
  now=$(timeout --kill-after=1s 1s date +%s) || return 1
  if [[ ! "$now" =~ ^[0-9]{1,12}$ ]] || ((10#$now < 1704067200)); then
    log "wlan0 recovery exhausted; refusing reboot with an invalid clock"
    return 1
  fi
  now=$((10#$now))

  if [[ -e "$REBOOT_BUDGET" || -L "$REBOOT_BUDGET" ]]; then
    if [[ ! -f "$REBOOT_BUDGET" || -L "$REBOOT_BUDGET" || ! -r "$REBOOT_BUDGET" ]]; then
      log "wlan0 recovery exhausted; refusing reboot with an unreadable or nonregular reboot budget"
      return 1
    fi
    if ! record=$(read_record "$REBOOT_BUDGET") ||
      [[ ! "$record" =~ ^v1\ ([0-9]{1,12})\ ([0-9]{1,12})$ ]]; then
      log "wlan0 recovery exhausted; refusing reboot with a corrupt reboot budget"
      return 1
    fi
    last_seen=$((10#${BASH_REMATCH[1]}))
    last_reboot=$((10#${BASH_REMATCH[2]}))
    if ((last_seen < 1704067200 || last_reboot > last_seen || (last_reboot != 0 && last_reboot < 1704067200))); then
      log "wlan0 recovery exhausted; refusing reboot with a corrupt reboot budget"
      return 1
    fi
    if ((now < last_seen)); then
      log "wlan0 recovery exhausted; refusing reboot after clock rollback"
      return 1
    fi
  fi

  if ((last_reboot > 0 && now - last_reboot < 86400)); then
    persist_budget "$now" "$last_reboot" || log "cannot persist the Wi-Fi reboot budget; reboot remains blocked"
    log "wlan0 recovery exhausted; the one-reboot-per-24-hours budget is spent"
    return 1
  fi
  if ! persist_budget "$now" "$now"; then
    log "cannot durably reserve the Wi-Fi reboot budget; refusing reboot"
    return 1
  fi
}

recover_wifi() {
  local now=$1
  local stage=0 started=$1 due observed record

  if [[ -r "$PENDING_STATE" ]]; then
    if record=$(read_record "$PENDING_STATE") &&
      [[ "$record" =~ ^v1\ ([0-3])\ ([0-9]{1,12})\ ([0-9]{1,12})\ ([0-9]{1,12})$ ]]; then
      stage=${BASH_REMATCH[1]}
      started=$((10#${BASH_REMATCH[2]}))
      due=$((10#${BASH_REMATCH[3]}))
      observed=$((10#${BASH_REMATCH[4]}))
      if ((started <= observed && observed <= now && due >= started)); then
        if ((stage == 3 || now < due)); then
          return 1
        fi
        case "$stage" in
          0)
            save_pending 1 "$started" "$((now + 120))" "$now" || return 1
            log "wlan0 underlay failed for $((now - started))s; restarting its netplan supplicant and renewing its network configuration once"
            timeout --kill-after=1s 4s systemctl restart "$WIFI_UNIT" || true
            timeout --kill-after=1s 2s networkctl reconfigure wlan0 || true
            timeout --kill-after=1s 2s networkctl renew wlan0 || true
            ;;
          1)
            save_pending 2 "$started" "$((now + 180))" "$now" || return 1
            log "wlan0 is still unusable; recycling only its link and renewing its network configuration once"
            if [[ -d "$SYSFS_DIR/wlan0" ]]; then
              : > "$LINK_UP_STATE" || return 1
              LINK_NEEDS_UP=1
              timeout --kill-after=1s 2s ip link set dev wlan0 down || true
              restore_link || true
            fi
            timeout --kill-after=1s 2s networkctl reconfigure wlan0 || true
            timeout --kill-after=1s 2s networkctl renew wlan0 || true
            ;;
          2)
            save_pending 2 "$started" "$((now + 300))" "$now" || return 1
            if reserve_reboot; then
              save_pending 3 "$started" "$((now + 300))" "$now" || return 1
              log "wlan0 recovery actions are exhausted with no usable wired uplink; requesting one budgeted reboot"
              timeout --kill-after=1s 2s systemctl --no-block reboot || true
            fi
            ;;
        esac
        return 1
      fi
    fi
  fi

  save_pending 0 "$now" "$((now + 300))" "$now" || return 1
  log "wlan0 has no usable underlay; scheduling Wi-Fi recovery in 300s if the failure persists"
  return 1
}

main() {
  local unit_state routes defaults iface gateway association now

  umask 077
  install -d -m 0700 "$RUNTIME_DIR" || return 1
  exec 9> "$RUNTIME_DIR/lock"
  flock -n 9 || return 1
  if [[ -e "$LINK_UP_STATE" || -L "$LINK_UP_STATE" ]]; then
    LINK_NEEDS_UP=1
    restore_link || true
  fi
  unit_state=$(timeout --kill-after=1s 1s systemctl show "$WIFI_UNIT" --property=LoadState --value 2>/dev/null || true)
  if [[ "$unit_state" == not-found ]]; then
    clear_pending
    return 0
  fi
  [[ "$unit_state" == loaded ]] || return 1

  now=$(monotonic_now) || return 1
  routes=$(timeout --kill-after=1s 1s ip -j -4 route show default 2>/dev/null) || return 1
  jq -e 'type == "array" and all(.[]; type == "object")' <<< "$routes" >/dev/null 2>&1 || return 1
  defaults=$(jq -r '.[] | select((.dev | type) == "string" and (.gateway | type) == "string") | [.dev, .gateway] | @tsv' <<< "$routes") || return 1

  if [[ -d "$SYSFS_DIR/wlan0" ]]; then
    association=$(timeout --kill-after=1s 1s iw dev wlan0 link 2>/dev/null || true)
    if [[ "$association" == "Connected to "* ]]; then
      while IFS=$'\t' read -r iface gateway; do
        [[ "$iface" == wlan0 ]] || continue
        if interface_works "$iface" "$gateway"; then
          clear_pending
          return 0
        fi
      done <<< "$defaults"
    fi
  fi

  while IFS=$'\t' read -r iface gateway; do
    [[ "$iface" == e* && "$iface" != */* && -d "$SYSFS_DIR/$iface" && ! -d "$SYSFS_DIR/$iface/wireless" ]] || continue
    if interface_works "$iface" "$gateway"; then
      clear_pending
      return 0
    fi
  done <<< "$defaults"

  recover_wifi "$now"
}

main
