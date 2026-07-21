#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR=${NODE_HEALTH_RUN_DIR:-/run/node-health}
UPTIME_FILE=${NODE_HEALTH_UPTIME_FILE:-/proc/uptime}
CLUSTER_PEER_CACHE=${NODE_HEALTH_PEER_CACHE:-/var/lib/node-health/cluster-peer-ips}
WIFI_HELPER=${NODE_HEALTH_WIFI_HELPER:-${BASH_SOURCE[0]%/*}/node-wifi-recovery.sh}
RECOVERY_STATE=$RUNTIME_DIR/tailscale-recovery
HEALTH_STATE=$RUNTIME_DIR/tailscale-health
JOURNAL_CURSOR=$RUNTIME_DIR/kernel-journal-cursor
TAILSCALE_FAILURE_DELAY=300
TAILSCALE_MAX_PROBES=5
K3S_FLANNEL_STARTUP_GRACE=180
K3S_UNIT=k3s
PENDING=0
STARTED=0
DUE=0
OBSERVED=0
ATTEMPTED=0
SUCCESS_STARTED=0
SUCCESSES=0
STARTUP_ATTEMPTED=0
DEFERRED=0
WARNED=0
PROBE_STATE=unknown
PROBE=tsmp
TARGET=-
REASON=no_peer_candidates
STARTUP_ACTION=
TAILSCALE_BACKEND=unknown

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
  log "event=recovery_action layer=tailscale action=$name result=$result exit_code=$rc"
  return "$rc"
}

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

diagnose_kernel() {
  local cursor= previous= records summary count temp
  local -a args=(--dmesg --boot --no-pager --output=json)
  if [[ -r "$JOURNAL_CURSOR" ]]; then
    previous=$(read_record "$JOURNAL_CURSOR" || true)
  fi
  if [[ -n "$previous" && ${#previous} -le 4096 ]]; then
    args+=(--after-cursor="$previous" --lines=+100)
  else
    args+=(--lines=100)
  fi
  records=$(timeout --kill-after=1s 2s journalctl "${args[@]}" 2>/dev/null) || return 0
  [[ -n "$records" ]] || return 0
  summary=$(jq -ers '
    [.[] | select((.__CURSOR | type) == "string")] as $records
    | select($records | length > 0)
    | [$records[-1].__CURSOR, ([
        $records[]
        | .MESSAGE
        | select(type == "string")
        | select(test("brcmfmac"; "i"))
        | select(test("(brcmf_sdio[^: ]*|brcmf_sdiod[^: ]*|backplane)"; "i"))
        | select(test("(^|[^[:alnum:]_])(failed|fail|error|timeout|halt(ed|ing)?)([^[:alnum:]_]|$)|timed out"; "i"))
      ] | length)]
    | @tsv
  ' <<< "$records" 2>/dev/null) || return 0
  IFS=$'\t' read -r cursor count <<< "$summary"
  [[ -n "$cursor" && ${#cursor} -le 4096 && "$count" =~ ^[0-9]{1,3}$ ]] || return 0
  [[ "$cursor" != "$previous" ]] || return 0
  temp=$(mktemp "$RUNTIME_DIR/kernel-journal-cursor.XXXXXX") || return 0
  (
    trap 'rm -f -- "$temp"' EXIT
    printf '%s\n' "$cursor" > "$temp" &&
      mv -f -- "$temp" "$JOURNAL_CURSOR"
  ) || return 0
  if ((count > 0)); then
    log "event=diagnostic_warning layer=wifi probe=kernel_journal target=- reason=brcmfmac_sdio_fatal records=$count"
  fi
}

load_recovery() {
  local record
  [[ -r "$RECOVERY_STATE" ]] || return 0
  if ! record=$(read_record "$RECOVERY_STATE") ||
    [[ ! "$record" =~ ^v2\ ([0-9]{1,12})\ ([0-9]{1,12})\ ([0-9]{1,12})\ ([01])\ ([0-9]{1,12})\ ([0-3])\ ([01])\ ([01])\ ([01])$ ]]; then
    return 0
  fi
  STARTED=$((10#${BASH_REMATCH[1]}))
  DUE=$((10#${BASH_REMATCH[2]}))
  OBSERVED=$((10#${BASH_REMATCH[3]}))
  ATTEMPTED=${BASH_REMATCH[4]}
  SUCCESS_STARTED=$((10#${BASH_REMATCH[5]}))
  SUCCESSES=${BASH_REMATCH[6]}
  STARTUP_ATTEMPTED=${BASH_REMATCH[7]}
  DEFERRED=${BASH_REMATCH[8]}
  WARNED=${BASH_REMATCH[9]}
  if ((STARTED <= OBSERVED && OBSERVED <= NOW && DUE >= STARTED &&
    SUCCESS_STARTED <= OBSERVED)) &&
    { ((SUCCESSES == 0 && SUCCESS_STARTED == 0)) ||
      ((SUCCESSES > 0 && SUCCESS_STARTED >= STARTED)); }; then
    PENDING=1
  fi
}

save_recovery() {
  local temp
  temp=$(mktemp "$RUNTIME_DIR/tailscale-recovery.XXXXXX") || return 1
  (
    trap 'rm -f -- "$temp"' EXIT
    printf 'v2 %s %s %s %s %s %s %s %s %s\n' \
      "$STARTED" "$DUE" "$NOW" "$ATTEMPTED" "$SUCCESS_STARTED" \
      "$SUCCESSES" "$STARTUP_ATTEMPTED" "$DEFERRED" "$WARNED" > "$temp" &&
      mv -f -- "$temp" "$RECOVERY_STATE"
  )
}

transition() {
  local state=$1 reason=$2 previous= temp elapsed=
  if [[ -r "$HEALTH_STATE" ]]; then
    previous=$(read_record "$HEALTH_STATE" || true)
  fi
  [[ "$previous" != "v1 $state" ]] || return 0
  temp=$(mktemp "$RUNTIME_DIR/tailscale-health.XXXXXX") || return 1
  (
    trap 'rm -f -- "$temp"' EXIT
    printf 'v1 %s\n' "$state" > "$temp" &&
      mv -f -- "$temp" "$HEALTH_STATE"
  ) || return 1
  if [[ "$reason" == sustained_recovery ]]; then
    elapsed=" elapsed_seconds=$((NOW - STARTED))"
  fi
  log "event=health_transition layer=tailscale state=$state probe=$PROBE target=$TARGET reason=$reason$elapsed"
}

observe_tailscale() {
  case "$PROBE_STATE" in
    healthy)
      if ((PENDING)); then
        if ((SUCCESSES == 0)); then
          SUCCESS_STARTED=$NOW
        fi
        ((SUCCESSES < 3)) && SUCCESSES=$((SUCCESSES + 1))
        if ((SUCCESSES >= 3 && NOW - SUCCESS_STARTED >= 30)); then
          rm -f -- "$RECOVERY_STATE" || return 1
          PENDING=0
          transition healthy sustained_recovery
        else
          save_recovery || return 1
          transition unknown confirming_recovery
        fi
      else
        transition healthy "$REASON"
      fi
      ;;
    unhealthy)
      if ((!PENDING)); then
        PENDING=1
        STARTED=$NOW
        DUE=$((NOW + TAILSCALE_FAILURE_DELAY))
        ATTEMPTED=0
        STARTUP_ATTEMPTED=0
        DEFERRED=0
        WARNED=0
      fi
      SUCCESS_STARTED=0
      SUCCESSES=0
      save_recovery || return 1
      transition unhealthy "$REASON"
      ;;
    unknown)
      if ((PENDING)); then
        SUCCESS_STARTED=0
        SUCCESSES=0
        save_recovery || return 1
      fi
      transition unknown "$REASON"
      ;;
  esac
  if ((PENDING && !WARNED && NOW >= DUE)) && [[ "$PROBE_STATE" != healthy ]]; then
    WARNED=1
    save_recovery || return 1
    log "event=diagnostic_warning layer=tailscale probe=$PROBE target=$TARGET reason=$REASON elapsed_seconds=$((NOW - STARTED))"
  fi
}

persist_cluster_peer_ips() {
  local cache_dir=${CLUSTER_PEER_CACHE%/*}
  local peers="$1" temp
  install -d -m 0755 "$cache_dir" || return 1
  if [[ -r "$CLUSTER_PEER_CACHE" ]] &&
    cmp -s "$CLUSTER_PEER_CACHE" <(printf '%s\n' "$peers"); then
    return 0
  fi
  temp=$(mktemp "${CLUSTER_PEER_CACHE}.XXXXXX") || return 1
  (
    trap 'rm -f -- "$temp"' EXIT
    printf '%s\n' "$peers" > "$temp" &&
      chmod 0644 "$temp" &&
      mv -f -- "$temp" "$CLUSTER_PEER_CACHE"
  )
}

cluster_peer_ips() {
  local flannel_fdb live_peers
  flannel_fdb=$(timeout --kill-after=1s 1s bridge -json fdb show dev flannel.1 2>/dev/null || true)
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

probe_tailscale() {
  local peer output attempts=0
  if ! timeout --kill-after=1s 1s systemctl is-active --quiet tailscaled.service; then
    PROBE_STATE=unhealthy
    PROBE=service
    REASON=service_inactive
    STARTUP_ACTION=start_tailscaled
    return 0
  fi
  output=$(timeout --kill-after=1s 2s tailscale status --json 2>/dev/null || true)
  TAILSCALE_BACKEND=$(jq -r '.BackendState // "unknown"' <<< "$output" 2>/dev/null || true)
  case "$TAILSCALE_BACKEND" in
    Running)
      while IFS= read -r peer; do
        [[ -n "$peer" ]] || continue
        [[ "$peer" =~ ^[0-9a-fA-F:.]+$ ]] || continue
        ((attempts += 1))
        TARGET=$peer
        if output=$(timeout --kill-after=1s 4s tailscale ping --tsmp --c 1 --timeout 2s "$peer" 2>/dev/null) &&
          [[ "$output" == "pong from "* || "$output" == *$'\n'"pong from "* ]]; then
          PROBE_STATE=healthy
          REASON=peer_pong
          return 0
        fi
        ((attempts >= TAILSCALE_MAX_PROBES)) && break
      done < <(cluster_peer_ips)
      if ((attempts > 0)); then
        PROBE_STATE=unhealthy
        REASON=peer_unreachable
      fi
      ;;
    Stopped)
      PROBE_STATE=unhealthy
      PROBE=backend
      REASON=backend_stopped
      STARTUP_ACTION=tailscale_up
      ;;
    NeedsLogin)
      PROBE=backend
      REASON=authentication_required
      ;;
    *)
      PROBE_STATE=unhealthy
      PROBE=backend
      REASON=backend_unavailable
      ;;
  esac
}

recover_tailscale() {
  [[ "$PROBE_STATE" == unhealthy ]] || return 0
  if ((UNDERLAY_RESULT != 0 && UNDERLAY_RESULT != 3)); then
    if ((NOW >= DUE && !ATTEMPTED && !DEFERRED)); then
      DEFERRED=1
      save_recovery || return 1
      log "event=recovery_action layer=tailscale action=restart_tailscaled result=deferred reason=underlay_unusable"
    fi
    return 0
  fi
  if [[ -n "$STARTUP_ACTION" ]] && ((!STARTUP_ATTEMPTED)); then
    STARTUP_ATTEMPTED=1
    DEFERRED=0
    save_recovery || return 1
    case "$STARTUP_ACTION" in
      start_tailscaled)
        action start_tailscaled requested 2 systemctl --no-block start tailscaled.service || true
        ;;
      tailscale_up)
        action tailscale_up succeeded 20 tailscale up || true
        ;;
    esac
    return 0
  fi
  ((NOW >= DUE && !ATTEMPTED)) || return 0
  ATTEMPTED=1
  DEFERRED=0
  save_recovery || return 1
  action restart_tailscaled requested 2 systemctl --no-block restart tailscaled.service || true
}

k3s_active_seconds() {
  local active_enter
  active_enter=$(timeout --kill-after=1s 1s systemctl show "$K3S_UNIT" \
    --property=ActiveEnterTimestampMonotonic --value 2>/dev/null || true)
  [[ "$active_enter" =~ ^[0-9]{1,18}$ ]] || return 1
  ((active_enter > 0 && NOW * 1000000 >= active_enter)) || return 1
  printf '%s\n' "$((NOW - active_enter / 1000000))"
}

manage_k3s() {
  local state active_seconds
  [[ "$TAILSCALE_BACKEND" == Running && "$PROBE_STATE" != unhealthy ]] || return 0
  if timeout --kill-after=1s 1s systemctl is-enabled k3s-agent >/dev/null 2>&1; then
    K3S_UNIT=k3s-agent
  fi
  state=$(timeout --kill-after=1s 1s systemctl is-active "$K3S_UNIT" 2>/dev/null || true)
  case "$state" in
    active)
      if ! timeout --kill-after=1s 1s ip link show flannel.1 >/dev/null 2>&1; then
        active_seconds=$(k3s_active_seconds || true)
        if [[ "$active_seconds" =~ ^[0-9]+$ ]] &&
          ((active_seconds >= K3S_FLANNEL_STARTUP_GRACE)); then
          action "restart_$K3S_UNIT" requested 2 systemctl --no-block restart "$K3S_UNIT" || true
        fi
      fi
      ;;
    activating | deactivating | reloading)
      ;;
    *)
      action "restart_$K3S_UNIT" requested 2 systemctl --no-block restart "$K3S_UNIT" || true
      ;;
  esac
}

main() {
  umask 077
  install -d -m 0700 "$RUNTIME_DIR" || return 1
  exec 8> "$RUNTIME_DIR/lock"
  flock -n 8 || return 0
  NOW=$(monotonic_now) || return 1
  diagnose_kernel || true
  load_recovery
  probe_tailscale
  observe_tailscale || return 1
  UNDERLAY_RESULT=0
  if [[ "$PROBE_STATE" == healthy ]]; then
    "$WIFI_HELPER" --tailnet-peer "$TARGET" || UNDERLAY_RESULT=$?
  else
    "$WIFI_HELPER" || UNDERLAY_RESULT=$?
  fi
  recover_tailscale || return 1
  manage_k3s
}

main
