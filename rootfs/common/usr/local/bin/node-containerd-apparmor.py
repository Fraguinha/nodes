#!/usr/bin/env python3
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys

ROOT = Path("/")
K3S = "/usr/local/bin/k3s"
BUNDLE = "/var/lib/rancher/k3s/data/current/bin"
PROFILE = "/usr/local/share/nodes/containerd-v2.3.7-k3s1.apparmor"
RECEIPT = "/run/nodes/containerd-apparmor.sha256"
SOCKET = "/run/k3s/containerd/containerd.sock"
NAME = "cri-containerd.apparmor.d"
VERSION = "v2.3.7-k3s1"
ADDITIONS = (
    "  signal (send,receive) peer=" + NAME + "//&unconfined,\n",
    "  ptrace (trace,tracedby,read,readby) peer=" + NAME + "//&unconfined,\n",
)


def target(path):
    return ROOT / path.lstrip("/")


def command(arguments, content=None):
    result = subprocess.run(arguments, input=content, text=True, capture_output=True, timeout=120,
                            env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    if result.returncode:
        raise RuntimeError("command failed: " + arguments[0] + ": " + result.stderr.strip())
    return result.stdout


def runtime_identity(output):
    return output.split() == ["ctr", "github.com/k3s-io/containerd/v2", VERSION]


def listener_pid():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        try:
            client.connect(str(target(SOCKET)))
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        pid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[0]
    if pid <= 0:
        raise RuntimeError("containerd listener identity unavailable")
    return str(pid)


def compatible_context():
    for name in ("abi/3.0", "tunables/global", "abstractions/base"):
        if not target("/etc/apparmor.d/" + name).is_file():
            raise RuntimeError("baseline AppArmor imports differ: " + name)
    if target("/proc/self/attr/current").read_text().strip() != "unconfined":
        raise RuntimeError("helper daemon-peer context is not unconfined")
    for unit in ("k3s.service", "k3s-agent.service"):
        if command(["systemctl", "show", unit, "--property=AppArmorProfile", "--value"]).strip():
            raise RuntimeError("K3s unit has an AppArmorProfile override")
    paths = [str(target(BUNDLE)), str(target(BUNDLE + "/aux")),
             str(target("/var/lib/rancher/k3s/data/cni")), *os.environ.get("PATH", "").split(os.pathsep)]
    if shutil.which("rootlesskit", path=os.pathsep.join(paths)):
        raise RuntimeError("rootlesskit changes the runtime baseline")
    verified = set()
    for process in target("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if (process / "comm").read_text().strip() != "containerd":
                continue
            executable = (process / "exe").resolve(strict=True)
            if executable != target(BUNDLE + "/containerd").resolve(strict=True):
                raise RuntimeError("live containerd does not match the selected bundle")
            if (process / "attr/current").read_text().strip() != "unconfined":
                raise RuntimeError("containerd daemon-peer context differs")
            environment = (process / "environ").read_bytes().split(b"\0")
            runtime_paths = [entry[5:].decode() for entry in environment if entry.startswith(b"PATH=")]
            if len(runtime_paths) != 1 or shutil.which("rootlesskit", path=runtime_paths[0]):
                raise RuntimeError("containerd rootlesskit baseline is not proved")
            verified.add(process.name)
        except FileNotFoundError:
            continue
    listener = listener_pid()
    if listener is not None and listener not in verified:
        raise RuntimeError("live containerd listener context could not be proved")


def prepare(check=False, render=False, baseline=False):
    enabled = target("/sys/module/apparmor/parameters/enabled")
    if not enabled.exists() or enabled.read_text().strip() != "Y":
        print("skip: AppArmor is disabled", file=sys.stderr if render else sys.stdout)
        return 0
    release = target("/etc/os-release").read_text()
    if not re.search(r'^ID="?ubuntu"?$', release, re.MULTILINE) or not re.search(r'^VERSION_ID="?26\.04"?$', release, re.MULTILINE):
        print("skip: OS is outside the Ubuntu 26.04 backport scope", file=sys.stderr if render else sys.stdout)
        return 0
    k3s = target(K3S)
    if not k3s.is_file():
        print("skip: K3s is absent", file=sys.stderr if render else sys.stdout)
        return 0
    k3s_version = command([str(k3s), "--version"])
    if not k3s_version.startswith("k3s version v1.37.2+k3s1 (f6973cb8)\n"):
        print("skip: K3s is outside the exact backport allowlist", file=sys.stderr if render else sys.stdout)
        return 0
    readonly = check or render
    if readonly:
        ctr = target(BUNDLE + "/ctr")
        if not ctr.is_file():
            raise RuntimeError("cached runtime unavailable; read-only mode will not unpack K3s")
        identity = command([str(ctr), "--version"])
    else:
        identity = command([str(k3s), "ctr", "--version"])
    if not runtime_identity(identity):
        print("skip: selected containerd is outside the exact backport allowlist", file=sys.stderr if render else sys.stdout)
        return 0
    compatible_context()
    parser = shutil.which("apparmor_parser")
    if not parser:
        raise RuntimeError("apparmor_parser unavailable")
    parser_version = command([parser, "--version"]).strip()
    if not re.search(r"AppArmor parser version 5\.0\.(?:0~beta1|2)(?:\s|$)", parser_version):
        raise RuntimeError("AppArmor parser is outside the verified allowlist: " + parser_version)
    profile = target(PROFILE).read_text()
    for addition in ADDITIONS:
        if profile.count(addition) != 1:
            raise RuntimeError("invalid stacked-peer asset")
        if baseline:
            profile = profile.replace(addition, "")
    command([parser, "-Q", "-K", "-T"], profile)
    if render:
        sys.stdout.write(profile)
        return 0
    profiles = target("/sys/kernel/security/apparmor/profiles")
    loaded = [line for line in profiles.read_text().splitlines() if line.startswith(NAME + " ")]
    if loaded and loaded != [NAME + " (enforce)"]:
        raise RuntimeError("existing containerd profile is not enforcing; replacement refused")
    digest = hashlib.sha256(profile.encode()).hexdigest() + "\n"
    receipt = target(RECEIPT)
    if check:
        if loaded and receipt.is_file() and receipt.read_text() == digest:
            print("match: exact-version stacked-peer backport activated; AppArmor enforcing")
            return 0
        print("pending: compatible exact-version backport has not been activated")
        return 1
    command([parser, "-r", "-K", "-T"], profile)
    if NAME + " (enforce)" not in profiles.read_text().splitlines():
        raise RuntimeError("loaded profile enforcement not proved")
    receipt.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    receipt.write_text(digest)
    receipt.chmod(0o644)
    print("loaded: exact-version stacked-peer backport; AppArmor enforcing")
    return 0


def main(arguments=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--render", action="store_true")
    mode.add_argument("--render-baseline", action="store_true")
    options = parser.parse_args(arguments)
    try:
        return prepare(options.check, options.render or options.render_baseline, options.render_baseline)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
