#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  "") reexec=1 ;;
  --no-reexec) reexec=0 ;;
  *) printf 'Error: unsupported watchdog loader argument\n' >&2; exit 1 ;;
esac
[[ $# -le 1 ]] || { printf 'Error: too many watchdog loader arguments\n' >&2; exit 1; }

if [[ -e /dev/watchdog0 ]]; then
  exit 0
fi

for module in iTCO_wdt hpwdt i6300esb softdog; do
  if timeout 8s modprobe "$module" >/dev/null 2>&1; then
    timeout 6s udevadm settle --timeout=5 >/dev/null 2>&1 || true
  fi
  if [[ -e /dev/watchdog0 ]]; then
    logger -t watchdog-load "loaded $module"
    break
  fi
done

if [[ ! -e /dev/watchdog0 ]]; then
  logger -t watchdog-load "no watchdog device available"
  exit 1
fi

if [[ "$reexec" == "1" ]]; then
  timeout 15s systemctl daemon-reexec
fi
