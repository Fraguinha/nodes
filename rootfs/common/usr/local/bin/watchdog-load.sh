#!/usr/bin/env bash
set -euo pipefail

if [[ -e /dev/watchdog0 ]]; then
  exit 0
fi

for module in iTCO_wdt hpwdt i6300esb softdog; do
  modprobe "$module" >/dev/null 2>&1 || true
  if [[ -e /dev/watchdog0 ]]; then
    logger -t watchdog-load "loaded $module"
    break
  fi
done

if [[ ! -e /dev/watchdog0 ]]; then
  logger -t watchdog-load "no watchdog device available"
  exit 1
fi

systemctl daemon-reexec
