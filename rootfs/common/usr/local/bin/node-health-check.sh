#!/usr/bin/env bash
set -euo pipefail

CLUSTER_PEER_CACHE=/var/lib/node-health/cluster-peer-ips
TAILSCALE_FAILURE_DEADLINE_STATE=/run/node-health.tailscale-failure-deadline
TAILSCALE_RECOVERY_ATTEMPTED_STATE=/run/node-health.tailscale-recovery-attempted
TAILSCALE_FAILURE_DELAY=300
TAILSCALE_MAX_PROBES=5
K3S_FLANNEL_STARTUP_GRACE=180
K3S_UNIT=k3s

if systemctl is-enabled k3s-agent >/dev/null 2>&1; then
  K3S_UNIT=k3s-agent
fi

tailscale_status() {
  timeout 2s tailscale status --json 2>/dev/null || true
}

tailscale_backend() {
  local backend

  backend=$(jq -r '.BackendState // "unknown"' <<< "$1" 2>/dev/null || true)
  printf '%s\n' "${backend:-unknown}"
}

k3s_active_seconds() {
  local active_enter
  local ignored
  local uptime

  active_enter=$(
    systemctl show "$K3S_UNIT" --property=ActiveEnterTimestampMonotonic --value 2>/dev/null ||
      true
  )
  read -r uptime ignored < /proc/uptime || return 1
  uptime=${uptime%%.*}

  [[ "$active_enter" =~ ^[0-9]+$ ]] || return 1
  [[ "$uptime" =~ ^[0-9]+$ ]] || return 1
  ((active_enter > 0 && uptime * 1000000 >= active_enter)) || return 1
  printf '%s\n' "$((uptime - active_enter / 1000000))"
}

persist_cluster_peer_ips() {
  local cache_dir=${CLUSTER_PEER_CACHE%/*}
  local peers="$1"
  local temp

  install -d -m 0755 "$cache_dir" || return 1
  if [[ -r "$CLUSTER_PEER_CACHE" ]] &&
    cmp -s "$CLUSTER_PEER_CACHE" <(printf '%s\n' "$peers"); then
    return 0
  fi

  temp=$(mktemp "${CLUSTER_PEER_CACHE}.XXXXXX") || return 1
  if ! (
    trap 'rm -f "$temp"' EXIT
    printf '%s\n' "$peers" > "$temp" &&
      chmod 0644 "$temp" &&
      mv -f "$temp" "$CLUSTER_PEER_CACHE" &&
      trap - EXIT
  ); then
    return 1
  fi
}

cluster_peer_ips() {
  local flannel_fdb
  local live_peers

  flannel_fdb=$(bridge -json fdb show dev flannel.1 2>/dev/null || true)
  live_peers=$(jq -r '
    [.[] | .dst? // empty | select(contains(":") | not)]
    | unique[]
  ' <<< "$flannel_fdb" 2>/dev/null || true)

  if [[ -n "$live_peers" ]]; then
    persist_cluster_peer_ips "$live_peers" || true
    printf '%s\n' "$live_peers"
  elif [[ -r "$CLUSTER_PEER_CACHE" ]]; then
    cat "$CLUSTER_PEER_CACHE"
  fi
}

tailscale_data_plane_works() {
  local attempts=0
  local peer

  while IFS= read -r peer; do
    [[ -n "$peer" ]] || continue
    ((attempts += 1))
    if timeout 4s tailscale ping --tsmp --c 1 --timeout 2s "$peer" >/dev/null 2>&1; then
      return 0
    fi
    ((attempts >= TAILSCALE_MAX_PROBES)) && break
  done < <(cluster_peer_ips)

  ((attempts > 0)) || return 2
  return 1
}

reset_tailscale_failure() {
  rm -f "$TAILSCALE_FAILURE_DEADLINE_STATE" "$TAILSCALE_RECOVERY_ATTEMPTED_STATE"
}

clear_tailscale_failure() {
  if [[ -e "$TAILSCALE_RECOVERY_ATTEMPTED_STATE" ]]; then
    logger -t node-health "tailnet data plane recovered after the tailscaled recovery attempt"
  elif [[ -e "$TAILSCALE_FAILURE_DEADLINE_STATE" ]]; then
    logger -t node-health "tailnet data plane recovered without intervention"
  fi
  reset_tailscale_failure
}

schedule_tailscale_recovery() {
  local deadline=
  local now
  local reason="$1"

  now=$(date +%s)
  if [[ -r "$TAILSCALE_FAILURE_DEADLINE_STATE" ]]; then
    read -r deadline < "$TAILSCALE_FAILURE_DEADLINE_STATE" || true
  fi
  if [[ ! "$deadline" =~ ^[0-9]+$ ]]; then
    deadline=$((now + TAILSCALE_FAILURE_DELAY))
    printf '%s\n' "$deadline" > "$TAILSCALE_FAILURE_DEADLINE_STATE"
    logger -t node-health \
      "$reason; scheduling one tailscaled restart in ${TAILSCALE_FAILURE_DELAY}s if the failure persists"
    return 0
  fi

  ((now >= deadline)) || return 0
  [[ ! -e "$TAILSCALE_RECOVERY_ATTEMPTED_STATE" ]] || return 0

  touch "$TAILSCALE_RECOVERY_ATTEMPTED_STATE"
  logger -t node-health "$reason persisted through the recovery deadline; restarting tailscaled once"
  systemctl --no-block restart tailscaled.service || true
}

if ! systemctl is-active --quiet tailscaled.service; then
  logger -t node-health "tailscaled is inactive; requesting a start"
  systemctl --no-block start tailscaled.service || true
  exit 0
fi

TAILSCALE_STATUS=$(tailscale_status)
TAILSCALE_BACKEND=$(tailscale_backend "$TAILSCALE_STATUS")
case "$TAILSCALE_BACKEND" in
  Running)
    if tailscale_data_plane_works; then
      clear_tailscale_failure
    else
      TAILSCALE_PROBE_RESULT=$?
      if ((TAILSCALE_PROBE_RESULT == 1)); then
        schedule_tailscale_recovery "tailnet data plane is unreachable"
        exit 0
      fi
      rm -f "$TAILSCALE_FAILURE_DEADLINE_STATE"
    fi
    ;;
  Stopped)
    reset_tailscale_failure
    logger -t node-health "requesting tailscale startup"
    timeout 20s tailscale up >/dev/null 2>&1 || true
    exit 0
    ;;
  NeedsLogin)
    reset_tailscale_failure
    logger -t node-health "tailscale requires authentication; leaving it unchanged"
    exit 0
    ;;
  *)
    schedule_tailscale_recovery "tailscale status is unavailable or backend is $TAILSCALE_BACKEND"
    exit 0
    ;;
esac

K3S_STATE=$(systemctl is-active "$K3S_UNIT" 2>/dev/null || true)
case "$K3S_STATE" in
  active)
    if ! ip link show flannel.1 >/dev/null 2>&1; then
      K3S_ACTIVE_SECONDS=$(k3s_active_seconds || true)
      if [[ "$K3S_ACTIVE_SECONDS" =~ ^[0-9]+$ ]] &&
        ((K3S_ACTIVE_SECONDS >= K3S_FLANNEL_STARTUP_GRACE)); then
        logger -t node-health \
          "flannel interface is still missing after ${K3S_ACTIVE_SECONDS}s; restarting $K3S_UNIT"
        systemctl --no-block restart "$K3S_UNIT" || true
      fi
    fi
    ;;
  activating | deactivating | reloading)
    ;;
  *)
    logger -t node-health "$K3S_UNIT is $K3S_STATE; requesting a restart"
    systemctl --no-block restart "$K3S_UNIT" || true
    ;;
esac
