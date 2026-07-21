#!/usr/bin/env python3
import os
import subprocess
import sys

DEVICE = "/dev/watchdog0"
MODULES = ("iTCO_wdt", "hpwdt", "i6300esb", "softdog")


def run(arguments, quiet=False):
    stream = subprocess.DEVNULL if quiet else None
    try:
        code = subprocess.run(arguments, stdout=stream, stderr=stream).returncode
    except OSError as error:
        return 126 if isinstance(error, PermissionError) else 127
    return 128 - code if code < 0 else code


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    first = arguments[0] if arguments else ""
    if first == "":
        reexec = True
    elif first == "--no-reexec":
        reexec = False
    else:
        print("Error: unsupported watchdog loader argument", file=sys.stderr)
        return 1
    if len(arguments) > 1:
        print("Error: too many watchdog loader arguments", file=sys.stderr)
        return 1
    if os.path.exists(DEVICE):
        return 0
    for module in MODULES:
        if run(["timeout", "8s", "modprobe", module], quiet=True) == 0:
            run(["timeout", "6s", "udevadm", "settle", "--timeout=5"], quiet=True)
        if os.path.exists(DEVICE):
            code = run(["logger", "-t", "watchdog-load", "loaded " + module])
            if code:
                return code
            break
    if not os.path.exists(DEVICE):
        return run(["logger", "-t", "watchdog-load", "no watchdog device available"]) or 1
    if reexec:
        return run(["timeout", "15s", "systemctl", "daemon-reexec"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
