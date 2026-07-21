#!/usr/bin/env bash
set -euo pipefail

STATE=/run/node-health.failures
THRESHOLD=3
MIN_UPTIME=900
K3S_UNIT=k3s

if systemctl is-enabled k3s-agent >/dev/null 2>&1; then
  K3S_UNIT=k3s-agent
fi

FAILURES=$(cat "$STATE" 2>/dev/null || echo 0)
[[ "$FAILURES" =~ ^[0-9]+$ ]] || FAILURES=0
UPTIME=$(awk '{print int($1)}' /proc/uptime)

fail() {
  local reason="$1"

  FAILURES=$((FAILURES + 1))
  echo "$FAILURES" > "$STATE"
  logger -t node-health "$reason ($FAILURES/$THRESHOLD)"
  if [[ "$FAILURES" -ge "$THRESHOLD" && "$UPTIME" -ge "$MIN_UPTIME" ]]; then
    logger -t node-health "unhealthy for $FAILURES consecutive checks, rebooting"
    rm -f "$STATE"
    systemctl reboot --force
  fi
  exit 0
}

if ! tailscale status >/dev/null 2>&1; then
  systemctl restart tailscaled || true
  fail "tailscale is unhealthy"
fi

if ! systemctl is-active --quiet "$K3S_UNIT"; then
  systemctl restart "$K3S_UNIT" || true
  fail "$K3S_UNIT is inactive"
fi

if ! ip link show flannel.1 >/dev/null 2>&1; then
  systemctl restart "$K3S_UNIT" || true
  fail "flannel interface is missing"
fi

rm -f "$STATE"
