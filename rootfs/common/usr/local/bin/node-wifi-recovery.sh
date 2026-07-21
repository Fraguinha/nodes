#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != --bounded ]]; then
  exec timeout --kill-after=8s 20s "$0" --bounded "$@"
fi
shift
TAILNET_PEER=
if (($#)); then
  [[ $# == 2 && "$1" == --tailnet-peer && "$2" =~ ^[0-9a-fA-F:.]+$ ]] || exit 64
  TAILNET_PEER=$2
fi

RUNTIME_DIR=${NODE_WIFI_RUNTIME_DIR:-/run/node-health/wifi-recovery}
PERSIST_DIR=${NODE_WIFI_PERSIST_DIR:-/var/lib/node-health/wifi-recovery}
SYSFS_DIR=${NODE_WIFI_SYSFS_DIR:-/sys/class/net}
UPTIME_FILE=${NODE_WIFI_UPTIME_FILE:-/proc/uptime}
PENDING_STATE=$RUNTIME_DIR/recovery
HEALTH_STATE=$RUNTIME_DIR/health
LINK_UP_STATE=$RUNTIME_DIR/link-needs-up
REBOOT_BUDGET=$PERSIST_DIR/reboot-budget
WIFI_UNIT=netplan-wpa-wlan0.service
LINK_NEEDS_UP=0
PENDING=0
STAGE=0
STARTED=0
DUE=0
OBSERVED=0
SUCCESS_STARTED=0
SUCCESSES=0
PROBE=none
TARGET=-
REASON=underlay_unreachable

log() {
  timeout --kill-after=1s 1s logger -t node-health "$*" || true
}

action() {
  local name=$1 result=$2 bound=$3 rc=0
  shift 3
  timeout --kill-after=1s "${bound}s" "$@" >/dev/null 2>&1 || rc=$?
  if ((rc != 0)); then
    result=failed
  fi
  log "event=recovery_action layer=wifi action=$name result=$result exit_code=$rc"
  return "$rc"
}

restore_link() {
  ((LINK_NEEDS_UP)) || return 0
  action link_up succeeded 2 ip link set dev wlan0 up || return 1
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

load_pending() {
  local record
  [[ -r "$PENDING_STATE" ]] || return 0
  if ! record=$(read_record "$PENDING_STATE") ||
    [[ ! "$record" =~ ^v2\ ([0-3])\ ([0-9]{1,12})\ ([0-9]{1,12})\ ([0-9]{1,12})\ ([0-9]{1,12})\ ([0-3])$ ]]; then
    return 0
  fi
  STAGE=${BASH_REMATCH[1]}
  STARTED=$((10#${BASH_REMATCH[2]}))
  DUE=$((10#${BASH_REMATCH[3]}))
  OBSERVED=$((10#${BASH_REMATCH[4]}))
  SUCCESS_STARTED=$((10#${BASH_REMATCH[5]}))
  SUCCESSES=${BASH_REMATCH[6]}
  if ((STARTED <= OBSERVED && OBSERVED <= NOW && DUE >= STARTED &&
    SUCCESS_STARTED <= OBSERVED)) &&
    { ((SUCCESSES == 0 && SUCCESS_STARTED == 0)) ||
      ((SUCCESSES > 0 && SUCCESS_STARTED >= STARTED)); }; then
    PENDING=1
  fi
}

save_pending() {
  local temp
  temp=$(mktemp "$RUNTIME_DIR/recovery.XXXXXX") || return 1
  (
    trap 'rm -f -- "$temp"' EXIT
    printf 'v2 %s %s %s %s %s %s\n' "$STAGE" "$STARTED" "$DUE" \
      "$NOW" "$SUCCESS_STARTED" "$SUCCESSES" > "$temp" &&
      mv -f -- "$temp" "$PENDING_STATE"
  )
}

transition() {
  local state=$1 reason=$2 previous= temp
  if [[ -r "$HEALTH_STATE" ]]; then
    previous=$(read_record "$HEALTH_STATE" || true)
  fi
  [[ "$previous" != "v1 $state" ]] || return 0
  temp=$(mktemp "$RUNTIME_DIR/health.XXXXXX") || return 1
  (
    trap 'rm -f -- "$temp"' EXIT
    printf 'v1 %s\n' "$state" > "$temp" &&
      mv -f -- "$temp" "$HEALTH_STATE"
  ) || return 1
  log "event=health_transition layer=wifi state=$state probe=$PROBE target=$TARGET reason=$reason"
}

observe_positive() {
  if ((PENDING)); then
    if ((SUCCESSES == 0)); then
      SUCCESS_STARTED=$NOW
    fi
    ((SUCCESSES < 3)) && SUCCESSES=$((SUCCESSES + 1))
    if ((SUCCESSES >= 3 && NOW - SUCCESS_STARTED >= 30)); then
      rm -f -- "$PENDING_STATE" || return 1
      PENDING=0
      transition healthy sustained_recovery
    else
      save_pending || return 1
      transition unknown confirming_recovery
    fi
  else
    transition healthy "$REASON"
  fi
}

observe_unknown() {
  if ((PENDING)); then
    SUCCESS_STARTED=0
    SUCCESSES=0
    save_pending || return 1
  fi
  transition unknown "$1"
}

interface_works() {
  local iface=$1 gateway=$2 addresses
  PROBE=default_route
  TARGET=-
  REASON=default_route_unusable
  [[ "$gateway" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  PROBE=address
  REASON=no_global_address
  addresses=$(timeout --kill-after=1s 1s ip -j -4 address show dev "$iface" scope global 2>/dev/null) || return 1
  jq -e 'any(.[].addr_info[]?; .family == "inet" and .scope == "global" and (.local | test("^(169\\.254\\.|127\\.)") | not))' \
    <<< "$addresses" >/dev/null 2>&1 || return 1
  if [[ -n "$TAILNET_PEER" ]]; then
    PROBE=tsmp
    TARGET=$TAILNET_PEER
    REASON=peer_pong
    return 0
  fi
  TARGET=$gateway
  if timeout --kill-after=1s 2s ping -n -I "$iface" -c1 -W1 "$gateway" >/dev/null 2>&1; then
    PROBE=gateway_icmp
    REASON=gateway_reply
    return 0
  fi
  TARGET=1.1.1.1
  if timeout --kill-after=1s 2s curl --silent --output /dev/null --noproxy '*' \
    --interface "$iface" --connect-timeout 1 --max-time 2 http://1.1.1.1/ >/dev/null 2>&1; then
    PROBE=external_http
    REASON=http_reply
    return 0
  fi
  PROBE=ip_traffic
  REASON=traffic_unreachable
  return 1
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
    REASON=clock_unsynchronized
    return 1
  fi
  REASON=clock_unavailable
  now=$(timeout --kill-after=1s 1s date +%s) || return 1
  if [[ ! "$now" =~ ^[0-9]{1,12}$ ]] || ((10#$now < 1704067200)); then
    REASON=clock_invalid
    return 1
  fi
  now=$((10#$now))
  if [[ -e "$REBOOT_BUDGET" || -L "$REBOOT_BUDGET" ]]; then
    if [[ ! -f "$REBOOT_BUDGET" || -L "$REBOOT_BUDGET" || ! -r "$REBOOT_BUDGET" ]]; then
      REASON=budget_unreadable
      return 1
    fi
    if ! record=$(read_record "$REBOOT_BUDGET") ||
      [[ ! "$record" =~ ^v1\ ([0-9]{1,12})\ ([0-9]{1,12})$ ]]; then
      REASON=budget_corrupt
      return 1
    fi
    last_seen=$((10#${BASH_REMATCH[1]}))
    last_reboot=$((10#${BASH_REMATCH[2]}))
    if ((last_seen < 1704067200 || last_reboot > last_seen ||
      (last_reboot != 0 && last_reboot < 1704067200))); then
      REASON=budget_corrupt
      return 1
    fi
    if ((now < last_seen)); then
      REASON=clock_rollback
      return 1
    fi
  fi
  if ((last_reboot > 0 && now - last_reboot < 86400)); then
    if ! persist_budget "$now" "$last_reboot"; then
      REASON=budget_persist_failed
      return 1
    fi
    REASON=budget_spent
    return 1
  fi
  if ! persist_budget "$now" "$now"; then
    REASON=budget_persist_failed
    return 1
  fi
}

recover_wifi() {
  if ((!PENDING)); then
    PENDING=1
    STAGE=0
    STARTED=$NOW
    DUE=$((NOW + 300))
  fi
  SUCCESS_STARTED=0
  SUCCESSES=0
  save_pending || return 1
  transition unhealthy "$REASON" || return 1
  ((STAGE < 3 && NOW >= DUE)) || return 1
  case "$STAGE" in
    0)
      STAGE=1
      DUE=$((NOW + 120))
      save_pending || return 1
      action restart_supplicant succeeded 4 systemctl restart "$WIFI_UNIT" || true
      action reconfigure requested 2 networkctl reconfigure wlan0 || true
      action renew requested 2 networkctl renew wlan0 || true
      ;;
    1)
      STAGE=2
      DUE=$((NOW + 180))
      save_pending || return 1
      if [[ -d "$SYSFS_DIR/wlan0" ]]; then
        : > "$LINK_UP_STATE" || return 1
        LINK_NEEDS_UP=1
        action link_down succeeded 2 ip link set dev wlan0 down || true
        restore_link || return 1
      fi
      action reconfigure requested 2 networkctl reconfigure wlan0 || true
      action renew requested 2 networkctl renew wlan0 || true
      ;;
    2)
      DUE=$((NOW + 300))
      save_pending || return 1
      if reserve_reboot; then
        STAGE=3
        save_pending || return 1
        action reboot requested 2 systemctl --no-block reboot || true
      else
        log "event=recovery_action layer=wifi action=reboot result=deferred reason=$REASON"
      fi
      ;;
  esac
  return 1
}

main() {
  local unit_state routes defaults iface gateway association
  umask 077
  install -d -m 0700 "$RUNTIME_DIR" || return 2
  exec 9> "$RUNTIME_DIR/lock"
  flock -n 9 || return 2
  NOW=$(monotonic_now) || return 2
  load_pending
  if [[ -e "$LINK_UP_STATE" || -L "$LINK_UP_STATE" ]]; then
    LINK_NEEDS_UP=1
    restore_link || true
    observe_unknown link_restore_pending || return 2
    return 1
  fi
  unit_state=$(timeout --kill-after=1s 1s systemctl show "$WIFI_UNIT" --property=LoadState --value 2>/dev/null || true)
  if [[ "$unit_state" == not-found ]]; then
    observe_unknown not_applicable || return 2
    return 3
  fi
  if [[ "$unit_state" != loaded ]]; then
    observe_unknown unit_unavailable || return 2
    return 2
  fi
  routes=$(timeout --kill-after=1s 1s ip -j -4 route show default 2>/dev/null) || routes=
  if ! jq -e 'type == "array" and all(.[]; type == "object")' <<< "$routes" >/dev/null 2>&1; then
    if [[ -z "$TAILNET_PEER" ]]; then
      observe_unknown routes_unavailable || return 2
      return 2
    fi
    defaults=
  else
    defaults=$(jq -r '.[] | select((.dev | type) == "string" and (.gateway | type) == "string") | [.dev, .gateway] | @tsv' <<< "$routes") || return 2
  fi
  if [[ -d "$SYSFS_DIR/wlan0" ]]; then
    association=$(timeout --kill-after=1s 1s iw dev wlan0 link 2>/dev/null || true)
    if [[ "$association" == "Connected to "* ]]; then
      PROBE=default_route
      REASON=no_default_route
      while IFS=$'\t' read -r iface gateway; do
        [[ "$iface" == wlan0 ]] || continue
        if interface_works "$iface" "$gateway"; then
          observe_positive
          return $?
        fi
      done <<< "$defaults"
    else
      PROBE=association
      REASON=not_associated
    fi
  else
    PROBE=interface
    REASON=interface_missing
  fi
  if [[ -n "$TAILNET_PEER" ]]; then
    PROBE=tsmp
    TARGET=$TAILNET_PEER
    REASON=peer_pong
    observe_positive
    return $?
  fi
  while IFS=$'\t' read -r iface gateway; do
    [[ "$iface" == e* && "$iface" != */* && -d "$SYSFS_DIR/$iface" && ! -d "$SYSFS_DIR/$iface/wireless" ]] || continue
    if interface_works "$iface" "$gateway"; then
      observe_positive
      return $?
    fi
  done <<< "$defaults"
  recover_wifi
}

main
