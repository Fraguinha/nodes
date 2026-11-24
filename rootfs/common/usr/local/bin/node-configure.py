#!/usr/bin/env python3
import argparse
import csv
import datetime
import fnmatch
import fcntl
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path("/")
POLICY_PATH = "/usr/local/share/nodes/node-policy.json"
APT_PATH = "/etc/apt/apt.conf.d/94cloud-init-config"
HA_PATH = "/etc/rancher/k3s/config.yaml.d/20-server-ha.yaml"
HA_TEMPLATE = "/usr/local/share/nodes/k3s-server-ha.yaml"
CHRONY_PATH = "/etc/chrony/conf.d/ip-fallback.conf"
CHRONY_PENDING_DIR = "/var/lib/nodes/chrony-pending"
CHRONY_PROGRESS_PATH = "/var/lib/nodes/chrony-pending-progress.json"
NVIDIA_KEY = "/usr/local/share/nodes/nvidia-container-toolkit.asc"
NVIDIA_SOURCE = "/etc/apt/sources.list.d/nvidia-container-toolkit.sources"
NVIDIA_RULES = "/etc/udev/rules.d/71-nvidia.rules"
NVIDIA_RULES_TEMPLATE = "/usr/local/share/nodes/nvidia-udev.rules"
NVIDIA_SOURCE_CONTENT = (
    "Types: deb\n"
    "URIs: https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH)\n"
    "Suites: /\n"
    "Signed-By: " + NVIDIA_KEY + "\n"
)
NETPLAN_UMASK = 0o022
ASSETS = {
    "/usr/local/bin/node-configure.py": 0o755,
    "/usr/local/bin/setup-node.sh": 0o755,
    "/usr/local/bin/watchdog-load.sh": 0o755,
    "/usr/local/bin/node-health-check.sh": 0o755,
    "/usr/local/bin/node-wifi-recovery.sh": 0o755,
    POLICY_PATH: 0o644,
    HA_TEMPLATE: 0o644,
    NVIDIA_KEY: 0o644,
    NVIDIA_RULES_TEMPLATE: 0o644,
    CHRONY_PATH: 0o644,
    "/etc/k3s-resolv.conf": 0o644,
    "/etc/sysctl.d/99-tailscale.conf": 0o644,
    "/etc/rsyslog.d/10-k3s.conf": 0o644,
    "/etc/systemd/journald.conf.d/10-limits.conf": 0o644,
    "/etc/systemd/system.conf.d/10-watchdog.conf": 0o644,
    "/etc/systemd/system/watchdog-load.service": 0o644,
    "/etc/systemd/system/node-health.service": 0o644,
    "/etc/systemd/system/node-health.timer": 0o644,
    "/etc/systemd/system/tailscaled.service.d/wait-for-time-sync.conf": 0o644,
    "/etc/systemd/system/tailscaled.service.d/10-oom.conf": 0o644,
    "/etc/systemd/system/ssh.service.d/10-oom.conf": 0o644,
    "/etc/systemd/system/k3s.service.d/restart-after-tailscale.conf": 0o644,
    "/etc/systemd/system/k3s-agent.service.d/restart-after-tailscale.conf": 0o644,
}
PHYSICAL_ASSETS = {
    "/etc/systemd/network/10-wlan0.link": 0o644,
    "/etc/udev/rules.d/70-wifi-powersave-off.rules": 0o644,
}
SYSCTLS = (
    "net.core.rmem_max", "net.core.wmem_max",
    "fs.inotify.max_user_instances", "fs.inotify.max_user_watches",
)
SEED_MEDIA_NAMES = ("user-data", "network-config")


class PolicyError(Exception):
    pass


class NetworkConflict(PolicyError):
    pass


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_command(arguments, timeout=20, env=None, umask=-1):
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    environment.update(env or {})
    try:
        process = subprocess.Popen(
            arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment, start_new_session=True, umask=umask,
        )
    except OSError:
        return Result(127)
    try:
        output, error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        return Result(124)
    return Result(process.returncode, output, error)


def read_text(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise PolicyError("required regular policy input unavailable")
    return path.read_text()


def parse_assignments(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise PolicyError("unsupported policy assignment")
        values[key.strip()] = value.strip()
    return values


def parse_ini(text):
    result = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line:
            key, value = line.split("=", 1)
            result[(section, key.strip())] = value.strip()
    return result


def duration(value):
    if str(value).isdigit():
        return float(value)
    total = 0.0
    rest = str(value).strip()
    for number, unit in re.findall(r"([0-9.]+)\s*(min|ms|us|h|s)", rest):
        total += float(number) * {"h": 3600, "min": 60, "s": 1, "ms": .001, "us": .000001}[unit]
        rest = rest.replace(number + unit, "", 1).strip()
    if rest:
        raise PolicyError("unsupported systemd duration")
    return total


def load_policy(path):
    policy = json.loads(read_text(path))
    if not isinstance(policy, dict) or set(policy) != {
        "version", "timezone", "users", "root", "packages", "apt", "swap", "network"
    } or policy["version"] != 1:
        raise PolicyError("unsupported canonical policy schema")
    if not isinstance(policy["timezone"], str) or not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+", policy["timezone"]):
        raise PolicyError("invalid timezone policy")
    if policy["root"] != {"password": "!", "expire": False} or policy["swap"] != {"size": 0}:
        raise PolicyError("unsupported account or swap policy")
    if set(policy["users"]) != {"pi"} or set(policy["users"]["pi"]) != {"groups", "shell", "sudo"}:
        raise PolicyError("unsupported managed user policy")
    user = policy["users"]["pi"]
    if not user["groups"] or not all(re.fullmatch(r"[a-z_][a-z0-9_-]*", group) for group in user["groups"]):
        raise PolicyError("invalid required groups")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]+", user["shell"]) or user["sudo"] != "ALL=(ALL) NOPASSWD:ALL":
        raise PolicyError("unsupported shell or sudo policy")
    if not isinstance(policy["packages"], dict) or set(policy["packages"]) != {"common", "physical", "nvidia"}:
        raise PolicyError("invalid package policy")
    for packages in policy["packages"].values():
        if not isinstance(packages, list) or not packages or not all(isinstance(package, str) and re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", package) for package in packages):
            raise PolicyError("invalid required packages")
    nvidia = policy["packages"]["nvidia"]
    if len([package for package in nvidia if re.fullmatch(r"linux-modules-nvidia-[0-9]+-server-generic", package)]) != 1 or any("-open" in package for package in nvidia):
        raise PolicyError("unsupported NVIDIA driver package policy")
    apt = policy["apt"]
    if set(apt) != {"package_update", "package_upgrade", "conf"} or not all(isinstance(apt[key], bool) for key in ("package_update", "package_upgrade")):
        raise PolicyError("invalid APT policy")
    if not isinstance(apt["conf"], dict) or not apt["conf"] or not all(re.fullmatch(r"[A-Za-z0-9:-]+", key) and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]+", value) for key, value in apt["conf"].items()):
        raise PolicyError("invalid APT configuration")
    network = policy["network"]
    if set(network) != {"version", "ethernet", "wifi", "profiles"} or network["version"] != 2:
        raise PolicyError("unsupported network policy")
    ethernet, wifi = network["ethernet"], network["wifi"]
    if set(ethernet) != {"id", "match_name", "dhcp4", "optional", "route_metric"} or set(wifi) != {"interface", "dhcp4", "optional", "route_metric", "key_management"}:
        raise PolicyError("unsupported network fields")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ethernet["id"]) or not re.fullmatch(r"[A-Za-z0-9*_-]+", ethernet["match_name"]) or wifi["interface"] != "wlan0" or wifi["key_management"] != "psk":
        raise PolicyError("unsupported interface policy")
    for link in (ethernet, wifi):
        if not isinstance(link["dhcp4"], bool) or not isinstance(link["optional"], bool) or type(link["route_metric"]) is not int or not 0 <= link["route_metric"] <= 4294967295:
            raise PolicyError("invalid DHCP policy")
    if not isinstance(network["profiles"], dict):
        raise PolicyError("invalid per-node network profiles")
    for hostname, profile in network["profiles"].items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", hostname) or set(profile) != {"ethernets", "wifis"} or set(profile["wifis"]) != {"wlan0"}:
            raise PolicyError("invalid per-node network identity")
        for kind in ("ethernets", "wifis"):
            for name, link in profile[kind].items():
                if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(link, dict) or set(link) - {"match", "dhcp4", "optional", "dhcp4-overrides", "addresses", "routes", "nameservers"}:
                    raise PolicyError("unsupported per-node nonsecret network fields")
                if not isinstance(link.get("dhcp4"), bool) or not isinstance(link.get("optional"), bool):
                    raise PolicyError("per-node DHCP and optional policy required")
                if "match" in link and (set(link["match"]) != {"name"} or not re.fullmatch(r"[A-Za-z0-9*_-]+", link["match"]["name"])):
                    raise PolicyError("unsupported network selector")
                for address in link.get("addresses", []):
                    ipaddress.IPv4Interface(address)
                if "nameservers" in link:
                    if set(link["nameservers"]) != {"addresses"}:
                        raise PolicyError("unsupported nameserver policy")
                    for address in link["nameservers"]["addresses"]:
                        ipaddress.ip_address(address)
                for route in link.get("routes", []):
                    if set(route) != {"to", "via", "metric"} or route["to"] != "default" or type(route["metric"]) is not int:
                        raise PolicyError("unsupported static route policy")
                    ipaddress.IPv4Address(route["via"])
                if "dhcp4-overrides" in link and (set(link["dhcp4-overrides"]) != {"route-metric"} or type(link["dhcp4-overrides"]["route-metric"]) is not int):
                    raise PolicyError("unsupported DHCP overrides")
    return policy


def merge_mapping(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_mapping(base[key], value)
        else:
            base[key] = value
    return base


def owned_route_key(route):
    if not isinstance(route, dict) or route.get("to") not in ("default", "0.0.0.0/0"):
        return None
    table = route.get("table", 254)
    if table == "main":
        table = 254
    gateway = route.get("via")
    if route.get("to") == "default":
        try:
            if ipaddress.ip_address(gateway).version != 4:
                return None
        except (ValueError, TypeError):
            return "unknown-default", table
    return "ipv4-default", table


def owned_projection_matches(actual, desired, path=()):
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(key in actual and owned_projection_matches(actual[key], value, path + (key,)) for key, value in desired.items())
    if isinstance(desired, list):
        if not isinstance(actual, list):
            return False
        if path and path[-1] == "routes":
            for route in desired:
                key = owned_route_key(route)
                matches = [value for value in actual if owned_route_key(value) == key]
                if key is None or len(matches) != 1 or any(owned_route_key(value) == ("unknown-default", key[1]) for value in actual) or not owned_projection_matches(matches[0], route, path + ("route",)):
                    return False
            return True
        return all(any(owned_projection_matches(value, expected, path + ("entry",)) for value in actual) for expected in desired)
    return type(actual) is type(desired) and actual == desired


def merge_owned_fields(actual, desired, path=()):
    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            raise NetworkConflict("managed link structure conflicts with existing data; explicit reconciliation required")
        for key, value in desired.items():
            if key in actual:
                actual[key] = merge_owned_fields(actual[key], value, path + (key,))
            else:
                actual[key] = json.loads(json.dumps(value))
        return actual
    if isinstance(desired, list):
        if not isinstance(actual, list):
            raise NetworkConflict("managed list structure conflicts with existing data; explicit reconciliation required")
        if path and path[-1] == "routes":
            keys = [owned_route_key(route) for route in desired]
            if any(key is None for key in keys) or len(set(keys)) != len(keys):
                raise NetworkConflict("canonical route ownership is ambiguous; existing routes retained")
            for route, key in zip(desired, keys):
                matches = [index for index, value in enumerate(actual) if owned_route_key(value) == key]
                if len(matches) > 1 or any(owned_route_key(value) == ("unknown-default", key[1]) for value in actual):
                    raise NetworkConflict("existing default route ownership is ambiguous; all routes retained")
                if matches:
                    index = matches[0]
                    actual[index] = merge_owned_fields(actual[index], route, path + ("route",))
                else:
                    actual.append(json.loads(json.dumps(route)))
        else:
            for value in desired:
                if not any(owned_projection_matches(existing, value, path + ("entry",)) for existing in actual):
                    actual.append(json.loads(json.dumps(value)))
        return actual
    return desired


def network_policy(policy, hostname):
    network = policy["network"]
    if hostname in network["profiles"]:
        result = json.loads(json.dumps(network["profiles"][hostname]))
    else:
        ethernet, wifi = network["ethernet"], network["wifi"]
        result = {
            "ethernets": {ethernet["id"]: {"match": {"name": ethernet["match_name"]}, "dhcp4": ethernet["dhcp4"], "optional": ethernet["optional"], "dhcp4-overrides": {"route-metric": ethernet["route_metric"]}}},
            "wifis": {wifi["interface"]: {"dhcp4": wifi["dhcp4"], "optional": wifi["optional"], "dhcp4-overrides": {"route-metric": wifi["route_metric"]}}},
        }
    return {"version": network["version"], **result}


def run_netplan_try(verify):
    pid, terminal = pty.fork()
    if pid == 0:
        environment = os.environ.copy()
        environment.update(LC_ALL="C", TERM="dumb")
        os.umask(NETPLAN_UMASK)
        os.execvpe("netplan", ["netplan", "try", "--timeout", "60"], environment)
    accepted = False
    inspected = False
    buffer = b""
    deadline = time.monotonic() + 75
    try:
        while time.monotonic() < deadline:
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                return Result(0 if accepted and os.waitstatus_to_exitcode(status) == 0 else 1)
            if select.select([terminal], [], [], .2)[0]:
                try:
                    data = os.read(terminal, 4096)
                except OSError:
                    data = b""
                buffer = (buffer + data)[-8192:]
                if not inspected and re.search(rb"(?i)(press enter|accept the new configuration|do you want to keep)", buffer):
                    inspected = True
                    accepted = bool(verify())
                    os.write(terminal, b"\n" if accepted else b"\x03")
        os.kill(pid, signal.SIGINT)
        for _ in range(25):
            finished, _ = os.waitpid(pid, os.WNOHANG)
            if finished:
                return Result(1)
            time.sleep(.2)
        os.killpg(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        return Result(124)
    finally:
        os.close(terminal)


class Reconciler:
    def __init__(self, platform, role=None, check=False, source_root=Path("/"), activate=False, bootstrap=False, cleanup_only=False):
        self.platform = platform
        self.requested_role = role
        self.role = None
        self.check = check
        self.activate = bool(activate and not check)
        self.bootstrap = bootstrap
        self.cleanup_only = cleanup_only
        self.source_root = Path(source_root)
        self.records = []
        self.policy = None
        self.ha_changed = False
        self.k3s_ready = False
        self.nvidia_enabled = False

    def target(self, path):
        return ROOT / path.lstrip("/")

    def desired(self, path):
        return self.source_root / path.lstrip("/")

    def report(self, item, state="match", reason=None):
        record = {"item": item, "state": state}
        if reason:
            record["reason"] = reason
        self.records.append(record)

    def command(self, arguments, timeout=20, env=None, required=True, umask=-1):
        result = run_command(arguments, timeout=timeout, env=env, umask=umask)
        if required and result.returncode:
            raise PolicyError("bounded command failed or unsupported")
        return result

    def properties(self, unit, names):
        arguments = ["systemctl", "show"]
        if unit:
            arguments.append(unit)
        names = list(dict.fromkeys((["LoadState"] if unit else []) + list(names)))
        arguments.extend("--property=" + name for name in names)
        result = self.command(arguments, required=False)
        properties = {}
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            properties[key] = properties[key] + "\n" + value if key in properties else value
        if result.returncode and properties.get("LoadState") != "not-found":
            raise PolicyError("systemd property query failed")
        return properties

    def atomic_file(self, path, content, mode):
        destination = self.target(path)
        if destination.is_symlink() or destination.exists() and not destination.is_file():
            raise PolicyError("managed destination is not a regular file")
        data = content.encode() if isinstance(content, str) else content
        same = destination.exists() and destination.read_bytes() == data
        metadata = destination.stat() if destination.exists() else None
        if same and stat.S_IMODE(metadata.st_mode) == mode and metadata.st_uid == 0 and metadata.st_gid == 0:
            return False
        if self.check:
            return True
        for parent in destination.parents:
            if parent == ROOT.parent:
                break
            if parent.is_symlink():
                raise PolicyError("managed destination parent is a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if same:
            os.chmod(destination, mode)
            os.chown(destination, 0, 0)
            return False
        descriptor, temporary = tempfile.mkstemp(prefix=".nodes-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), mode)
                os.fchown(handle.fileno(), 0, 0)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True

    def initialize(self):
        if self.platform not in ("physical", "cloud") or self.requested_role not in (None, "server", "agent"):
            raise PolicyError("explicit supported platform and role required")
        if yaml is None:
            raise PolicyError("PyYAML is required")
        release = parse_assignments(read_text(self.target("/etc/os-release").resolve()))
        if release.get("ID", "").strip('"') != "ubuntu":
            raise PolicyError("only Ubuntu nodes are managed")
        self.policy = load_policy(self.desired(POLICY_PATH))
        roles = []
        for role, unit in (("server", "k3s.service"), ("agent", "k3s-agent.service")):
            properties = self.properties(unit, ["LoadState", "FragmentPath"])
            if properties.get("LoadState") not in ("not-found", "masked", "loaded"):
                raise PolicyError("K3s installation state unavailable")
            if properties.get("FragmentPath"):
                roles.append(role)
        if len(roles) > 1 or self.requested_role and roles and roles != [self.requested_role]:
            raise PolicyError("ambiguous K3s role or requested role change refused")
        if not roles and self.requested_role is None:
            raise PolicyError("installed K3s role unavailable")
        self.role = self.requested_role or roles[0]
        self.report("identity")

    def assets(self):
        assets = dict(ASSETS)
        if self.platform == "physical":
            assets.update(PHYSICAL_ASSETS)
        drift = False
        for path, mode in assets.items():
            expected = self.desired(path)
            actual = self.target(path)
            if expected.is_symlink() or not expected.is_file():
                raise PolicyError("desired managed asset unavailable")
            if actual.is_symlink() or not actual.is_file():
                drift = True
                continue
            metadata = actual.stat()
            if actual.read_bytes() != expected.read_bytes() or stat.S_IMODE(metadata.st_mode) != mode or metadata.st_uid != 0 or metadata.st_gid != 0:
                drift = True
        self.report("managed-files", "drift" if drift else "match", "file transaction required" if drift else None)

    def apt(self):
        conf = self.policy["apt"]["conf"]
        content = "".join(key + " " + json.dumps(value) + ";\n" for key, value in conf.items())
        changed = self.atomic_file(APT_PATH, content, 0o644)
        output = self.command(["apt-config", "dump"]).stdout
        effective = dict(re.findall(r'^([^\s]+)\s+"([^"\n]*)";\s*$', output, re.MULTILINE))
        parity = all(effective.get(key) == value for key, value in conf.items())
        self.report("apt", "drift" if not parity or self.check and changed else "match", "owned APT configuration or effective precedence differs" if not parity or self.check and changed else None)

    def missing_packages(self, required):
        result = self.command(["dpkg-query", "-W", "-f=${Package}\t${db:Status-Status}\n", *required], required=False)
        if result.returncode not in (0, 1):
            raise PolicyError("package inventory unavailable")
        installed = {fields[0] for line in result.stdout.splitlines() if len(fields := line.split("\t")) == 2 and fields[1] == "installed"}
        return [package for package in required if package not in installed]

    def install_packages(self, absent, update=False):
        if self.check or not absent and not update:
            return
        if not any(record["item"] == "root-lock-expiry" and record["state"] == "match" for record in self.records):
            raise PolicyError("root lock and nonexpired password must be proved before package post-install scripts")
        environment = {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
        options = ["-o", "DPkg::Lock::Timeout=30", "-o", "Acquire::http::Timeout=15", "-o", "Acquire::https::Timeout=15"]
        self.command(["apt-get", *options, "update"], timeout=300, env=environment)
        if absent:
            self.command(["apt-get", *options, "-o", "Dpkg::Options::=--force-confold", "install", "--yes", "--no-upgrade", "--no-remove", *absent], timeout=900, env=environment)

    def packages(self):
        required = self.policy["packages"]["common"] + (self.policy["packages"]["physical"] if self.platform == "physical" else [])
        absent = self.missing_packages(required)
        if absent and not self.check:
            self.install_packages(absent)
            absent = self.missing_packages(required)
        self.report("packages", "drift" if absent else "match", "required packages missing" if absent else None)

    def nvidia_devices(self):
        devices = []
        for path in sorted(self.target("/sys/bus/pci/devices").glob("*")):
            if read_text(path / "vendor").strip() == "0x10de" and read_text(path / "class").strip().startswith("0x03"):
                devices.append(path)
        return devices

    def nvidia_kernel_network(self, running):
        kernels = []
        for path in self.target("/boot").glob("vmlinuz-*"):
            version = path.name.removeprefix("vmlinuz-")
            if path.is_file() and re.fullmatch(r"[0-9]+(?:\.[0-9]+)+-[0-9]+-[a-z0-9-]+", version):
                kernels.append(version)
        if not kernels:
            raise PolicyError("installed bootable kernel inventory unavailable")
        def version_key(version):
            return tuple(int(value) for value in re.findall(r"[0-9]+", version))
        newest = max(kernels, key=version_key)
        extras = []
        if not self.missing_packages(["linux-modules-extra-" + running]):
            extras = ["linux-modules-extra-" + version for version in kernels if version_key(version) > version_key(running)]
        absent = self.missing_packages(extras) if extras else []
        if absent and not self.check:
            self.install_packages(absent)
            absent = self.missing_packages(extras)
        routes = json.loads(self.command(["ip", "-j", "-4", "route", "show", "default"]).stdout)
        devices = {route.get("dev") for route in routes}
        if not devices or any(not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", device) or device in (".", "..") for device in devices):
            raise PolicyError("default-route network interface unavailable; reboot safety unproved")
        for device in sorted(devices):
            path = self.target("/sys/class/net/" + device + "/device/driver/module")
            if not path.is_symlink():
                raise PolicyError("default-route driver module unavailable for " + device + "; reboot safety unproved")
            module = Path(os.readlink(path)).name
            if not re.fullmatch(r"[A-Za-z0-9_]+", module):
                raise PolicyError("default-route driver module identity unsupported")
            if self.command(["modinfo", "-k", newest, module], required=False).returncode:
                self.report("nvidia-kernel-network", "drift", "default-route module " + module + " missing for newest kernel " + newest + "; reboot would risk network loss")
                return False
        self.report("nvidia-kernel-network", "drift" if absent else "match", "newer kernels lack required extra modules" if absent else None)
        return not absent

    def nvidia_driver(self, devices, rules_changed=False):
        unbound = [path for path in devices if not (path / "driver").is_symlink() or Path(os.readlink(path / "driver")).name != "nvidia"]
        if not self.check and (unbound or rules_changed):
            if not rules_changed:
                self.command(["udevadm", "control", "--reload-rules"])
            for path in unbound:
                (path / "driver_override").write_text("nvidia\n")
                if (path / "driver").is_symlink():
                    (path / "driver/unbind").write_text(path.name + "\n")
            if unbound and self.target("/sys/module/nouveau").exists():
                self.command(["rmmod", "nouveau"])
            self.command(["modprobe", "nvidia"])
            self.command(["modprobe", "nvidia_uvm"])
            self.command(["systemctl", "restart", "nvidia-persistenced.service"], timeout=30)
            self.command(["systemctl", "restart", "nvidia-cdi-refresh.service"], timeout=60)
            unbound = [path for path in devices if not (path / "driver").is_symlink() or Path(os.readlink(path / "driver")).name != "nvidia"]
        self.report("nvidia-driver", "drift" if unbound else "match", "NVIDIA GPU not bound to nvidia" if unbound else None)

    def nvidia(self):
        devices = self.nvidia_devices()
        self.nvidia_enabled = bool(devices)
        if not devices:
            return
        key_changed = self.atomic_file(NVIDIA_KEY, self.desired(NVIDIA_KEY).read_bytes(), 0o644)
        source_changed = self.atomic_file(NVIDIA_SOURCE, NVIDIA_SOURCE_CONTENT, 0o644)
        rules_changed = self.atomic_file(NVIDIA_RULES, read_text(self.desired(NVIDIA_RULES_TEMPLATE)), 0o644)
        self.report("nvidia-repository", "drift" if self.check and (key_changed or source_changed) else "match")
        self.report("nvidia-udev", "drift" if self.check and rules_changed else "match")
        if rules_changed and not self.check:
            self.command(["udevadm", "control", "--reload-rules"])
        running = read_text(self.target("/proc/sys/kernel/osrelease")).strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+-[0-9]+-[a-z0-9-]+", running):
            raise PolicyError("running kernel release unsupported")
        required = list(self.policy["packages"]["nvidia"])
        modules = next(package.removesuffix("generic") for package in required if re.fullmatch(r"linux-modules-nvidia-[0-9]+-server-generic", package))
        required.append(modules + running)
        absent = self.missing_packages(required)
        installed = bool(absent and not self.check)
        self.install_packages(absent, update=source_changed or key_changed)
        if installed:
            self.command(["update-initramfs", "-u", "-k", "all"], timeout=900)
            absent = self.missing_packages(required)
        self.report("nvidia-packages", "drift" if absent else "match", "required NVIDIA packages missing" if absent else None)
        if not self.nvidia_kernel_network(running):
            return
        self.nvidia_driver(devices, rules_changed)

    def nvidia_runtime_registered(self):
        path = self.target("/var/lib/rancher/k3s/agent/etc/containerd/config.toml")
        if not path.exists():
            return False
        return bool(re.search(r"""(?m)^\s*\[plugins\.(?:'io\.containerd\.(?:cri\.v1\.runtime|grpc\.v1\.cri)'|"io\.containerd\.(?:cri\.v1\.runtime|grpc\.v1\.cri)")\.containerd\.runtimes\.(?:'nvidia'|"nvidia"|nvidia)\]\s*$""", read_text(path)))

    def accounts(self):
        desired = self.policy["users"]["pi"]
        def users():
            return {parts[0]: parts for line in read_text(self.target("/etc/passwd")).splitlines() if len(parts := line.split(":")) == 7}
        before = users()
        if "pi" not in before or "root" not in before or before["root"][2] != "0":
            raise PolicyError("managed accounts unavailable; creation or UID migration refused")
        groups = {parts[0]: parts for line in read_text(self.target("/etc/group")).splitlines() if len(parts := line.split(":")) == 4}
        if any(group not in groups for group in desired["groups"]):
            raise PolicyError("required account group unavailable")
        absent = [group for group in desired["groups"] if "pi" not in groups[group][3].split(",") and groups[group][2] != before["pi"][3]]
        shell_drift = before["pi"][6] != desired["shell"]
        if not self.check:
            if shell_drift:
                self.command(["usermod", "--shell", desired["shell"], "pi"])
            if absent:
                self.command(["usermod", "--append", "--groups", ",".join(absent), "pi"])
            after = users()
            if after["pi"][2:6] != before["pi"][2:6]:
                raise PolicyError("account identity changed unexpectedly")
            groups = {parts[0]: parts for line in read_text(self.target("/etc/group")).splitlines() if len(parts := line.split(":")) == 4}
            shell_drift = after["pi"][6] != desired["shell"]
            absent = [group for group in desired["groups"] if "pi" not in groups[group][3].split(",") and groups[group][2] != after["pi"][3]]
        self.report("pi-account", "drift" if absent or shell_drift else "match")
        sudo = "pi " + desired["sudo"] + "\n"
        def sudo_matches():
            listing = self.command(["sudo", "-n", "-l", "-U", "pi"]).stdout
            rules = [re.sub(r"\s+", "", line) for line in listing.splitlines() if line.lstrip().startswith("(")]
            return bool(rules) and rules[-1] in ("(ALL)NOPASSWD:ALL", "(ALL:ALL)NOPASSWD:ALL")
        effective_sudo = sudo_matches()
        if not effective_sudo and not self.check:
            with tempfile.TemporaryDirectory(prefix="nodes-sudo-") as directory:
                candidate = Path(directory) / "pi"
                candidate.write_text(sudo)
                candidate.chmod(0o440)
                self.command(["visudo", "-c", "-f", str(candidate)])
            self.atomic_file("/etc/sudoers.d/90-nodes-pi", sudo, 0o440)
            effective_sudo = sudo_matches()
        self.report("pi-sudo", "match" if effective_sudo else "drift", None if effective_sudo else "effective passwordless sudo policy differs")

    def root_account(self):
        users = [line.split(":") for line in read_text(self.target("/etc/passwd")).splitlines() if line.startswith("root:")]
        if len(users) != 1 or len(users[0]) != 7 or users[0][2] != "0":
            raise PolicyError("root account identity unavailable; password and package mutation refused")
        def root_shadow():
            entries = [line.split(":") for line in read_text(self.target("/etc/shadow")).splitlines() if line.startswith("root:")]
            if len(entries) != 1 or len(entries[0]) != 9:
                raise PolicyError("root password state unavailable")
            return entries[0]
        root = root_shadow()
        if not self.check:
            if root[1] != "!":
                self.command(["usermod", "--password", "!", "root"])
            if root[2] in ("", "0"):
                self.command(["chage", "--lastday", datetime.date.today().isoformat(), "root"])
            root = root_shadow()
        self.report("root-lock-expiry", "match" if root[1] == "!" and root[2].isdigit() and int(root[2]) > 0 else "drift")

    def timezone(self):
        desired = self.policy["timezone"]
        def matches():
            name = self.command(["timedatectl", "show", "--property=Timezone", "--value"]).stdout.strip()
            zone = self.target("/usr/share/zoneinfo/" + desired)
            local = self.target("/etc/localtime")
            return name == desired and zone.is_file() and local.is_file() and local.read_bytes() == zone.read_bytes()
        if not matches() and not self.check:
            self.command(["timedatectl", "set-timezone", desired])
        self.report("timezone", "match" if matches() else "drift")

    def swap(self):
        fstab = self.target("/etc/fstab")
        content = read_text(fstab)
        kept = []
        persistent = False
        for line in content.splitlines(keepends=True):
            fields = line.split("#", 1)[0].split()
            if len(fields) >= 3 and fields[2] == "swap":
                persistent = True
            else:
                kept.append(line)
        entries = []
        for line in read_text(self.target("/proc/swaps")).splitlines()[1:]:
            fields = line.split()
            if len(fields) < 5 or not fields[2].isdigit() or not fields[3].isdigit():
                raise PolicyError("swap inventory unsupported")
            entries.append(fields)
        if entries and not self.check:
            memory = {key: int(value) * 1024 for key, value in re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)\s+kB$", read_text(self.target("/proc/meminfo")), re.MULTILINE)}
            if set(memory) != {"MemTotal", "MemAvailable"} or any(int(entry[3]) for entry in entries) or memory["MemAvailable"] < max(256 * 1024 * 1024, memory["MemTotal"] // 5):
                self.report("swap", "pending", "active swap retained to avoid memory-pressure or swapped-page OOM risk")
                return
            for entry in entries:
                if not entry[0].startswith("/") or "\\" in entry[0]:
                    raise PolicyError("swap path unsupported")
                self.command(["swapoff", "--", entry[0]], timeout=30)
            if len(read_text(self.target("/proc/swaps")).splitlines()) > 1:
                self.report("swap", "pending", "swap remains active")
                return
        if persistent and not self.check:
            self.atomic_file("/etc/fstab", "".join(kept), stat.S_IMODE(fstab.stat().st_mode))
        inventory = self.command(["systemctl", "list-unit-files", "--type=swap", "--no-legend", "--no-pager"], required=False)
        if inventory.returncode and not (inventory.returncode == 1 and inventory.stdout == "" and inventory.stderr == ""):
            raise PolicyError("swap unit inventory unavailable")
        units = inventory.stdout
        enabled = any(len(fields := line.split()) >= 2 and fields[1] in ("enabled", "enabled-runtime", "linked", "linked-runtime") for line in units.splitlines())
        self.report("swap", "pending" if enabled else "drift" if self.check and (entries or persistent) else "match", "explicit swap unit still enabled" if enabled else None)

    def sysctls(self):
        expected = parse_assignments(read_text(self.desired("/etc/sysctl.d/99-tailscale.conf")))
        if set(expected) != set(SYSCTLS) or not all(value.isdigit() for value in expected.values()):
            raise PolicyError("unsupported owned sysctl policy")
        parity = True
        for key, value in expected.items():
            live = self.target("/proc/sys/" + key.replace(".", "/"))
            if read_text(live).strip() != value and not self.check:
                self.command(["sysctl", "-w", key + "=" + value])
            parity = parity and read_text(live).strip() == value
        self.report("sysctls", "match" if parity else "drift")

    def process_start(self, pid):
        process = read_text(self.target("/proc/" + str(pid) + "/stat"))
        fields = process[process.rfind(")") + 2:].split()
        uptime = read_text(self.target("/proc/uptime")).split()
        if len(fields) <= 19 or not uptime:
            raise PolicyError("process start time unavailable")
        return time.time() - float(uptime[0]) - .01 + int(fields[19]) / os.sysconf("SC_CLK_TCK")

    def unit_command(self, value):
        commands = re.findall(r"argv\[\]=(.*?)\s*;", value)
        if not commands:
            raise PolicyError("loaded service command unavailable")
        return [shlex.split(command) for command in commands]

    def systemd(self):
        units = ["node-health.service", "node-health.timer", "watchdog-load.service", "tailscaled.service", "ssh.service", "k3s.service" if self.role == "server" else "k3s-agent.service"]
        reload_needed = any(self.properties(unit, ["NeedDaemonReload"]).get("NeedDaemonReload") == "yes" for unit in units)
        if reload_needed and not self.check:
            self.command(["systemctl", "daemon-reload"])
        self.report("systemd-loaded", "drift" if reload_needed and self.check else "match")
        timer = self.properties("node-health.timer", ["LoadState", "UnitFileState", "ActiveState", "Unit", "TimersMonotonic", "AccuracyUSec", "RandomizedDelayUSec"])
        service = self.properties("node-health.service", ["LoadState", "UnitFileState", "Type", "TimeoutStartUSec", "ExecStart"])
        if not self.check:
            if service.get("UnitFileState") in ("enabled", "enabled-runtime", "linked", "linked-runtime"):
                self.command(["systemctl", "disable", "node-health.service"])
            if timer.get("UnitFileState") != "enabled":
                self.command(["systemctl", "enable", "node-health.timer"])
            if timer.get("ActiveState") != "active":
                self.command(["systemctl", "start", "node-health.timer"])
            timer = self.properties("node-health.timer", ["LoadState", "UnitFileState", "ActiveState", "Unit", "TimersMonotonic", "AccuracyUSec", "RandomizedDelayUSec"])
            service = self.properties("node-health.service", ["LoadState", "UnitFileState", "Type", "TimeoutStartUSec", "ExecStart"])
        timers = dict(re.findall(r"(OnBootUSec|OnUnitInactiveUSec)=([^;}]*)", timer.get("TimersMonotonic", "")))
        timer_ok = timer.get("LoadState") == "loaded" and timer.get("UnitFileState") == "enabled" and timer.get("ActiveState") == "active" and timer.get("Unit") == "node-health.service" and duration(timer.get("AccuracyUSec", "")) == 1 and duration(timer.get("RandomizedDelayUSec", "")) == 5 and set(timers) == {"OnBootUSec", "OnUnitInactiveUSec"} and duration(timers["OnBootUSec"].strip()) == 60 and duration(timers["OnUnitInactiveUSec"].strip()) == 15
        service_ok = service.get("LoadState") == "loaded" and service.get("UnitFileState") in ("static", "disabled") and service.get("Type") == "oneshot" and duration(service.get("TimeoutStartUSec", "")) == 60 and self.unit_command(service.get("ExecStart", "")) == [["/usr/local/bin/node-health-check.sh"]]
        self.report("health-timer", "match" if timer_ok and service_ok else "drift", None if timer_ok and service_ok else "effective health scheduling policy differs")
        tailscale = self.properties("tailscaled.service", ["UnitFileState", "Wants", "After", "ExecStartPre"])
        if tailscale.get("UnitFileState") != "enabled" and not self.check:
            self.command(["systemctl", "enable", "tailscaled.service"])
            tailscale["UnitFileState"] = self.properties("tailscaled.service", ["UnitFileState"]).get("UnitFileState")
        self.report("tailscale-enabled", "match" if tailscale.get("UnitFileState") == "enabled" else "drift")
        pre = ["/usr/bin/chronyc", "-h", "127.0.0.1,::1", "waitsync", "1", "0.1", "0.0", "1"]
        time_ok = "chrony-wait.service" in tailscale.get("Wants", "").split() and "chrony-wait.service" in tailscale.get("After", "").split() and pre in self.unit_command(tailscale.get("ExecStartPre", ""))
        self.report("tailscale-time-dependency", "match" if time_ok else "drift")
        k3s = self.properties(units[-1], ["LoadState", "After", "PartOf"])
        if k3s.get("LoadState") == "not-found":
            self.report("k3s-dependency", "pending", "K3s not installed; bootstrap role preserved")
        else:
            dependencies = "tailscaled.service" in k3s.get("After", "").split() and "tailscaled.service" in k3s.get("PartOf", "").split()
            self.report("k3s-dependency", "match" if dependencies else "drift")

    def oom(self):
        for unit, name in (("tailscaled.service", "tailscaled"), ("ssh.service", "sshd")):
            properties = self.properties(unit, ["OOMScoreAdjust", "MainPID", "ControlGroup", "ActiveState"])
            item = "oom-" + name
            pid = properties.get("MainPID", "0")
            if properties.get("OOMScoreAdjust") != "-1000":
                self.report(item, "pending" if name == "sshd" else "drift", "loaded OOM policy differs; SSH settings retained" if name == "sshd" else "loaded OOM policy differs")
                continue
            if properties.get("ActiveState") != "active" or not pid.isdigit() or int(pid) <= 1:
                if name == "sshd" and pid == "0":
                    activation = self.properties("ssh.socket", ["ActiveState"])
                    if activation.get("ActiveState") == "active":
                        self.report(item, "match", "socket-activated SSH has no live daemon; loaded OOM policy applies on activation")
                        continue
                self.report(item, "pending", "intended daemon is not active; restart refused")
                continue
            process = self.target("/proc/" + pid)
            executable = Path(os.readlink(process / "exe")).name
            cgroup = read_text(process / "cgroup")
            expected_group = properties.get("ControlGroup", "")
            if executable != name or not expected_group or not any(line.split(":", 2)[-1] == expected_group for line in cgroup.splitlines()):
                raise PolicyError("intended daemon process identity not proved")
            live = process / "oom_score_adj"
            if name == "sshd" and read_text(live).strip() != "-1000":
                self.report(item, "pending", "live SSH OOM drift retained per operator instruction")
                continue
            if read_text(live).strip() != "-1000" and not self.check:
                if self.properties(unit, ["MainPID"]).get("MainPID") != pid:
                    raise PolicyError("daemon process changed during OOM application")
                live.write_text("-1000\n")
            self.report(item, "match" if read_text(live).strip() == "-1000" else "drift")

    def tailscale(self):
        state = json.loads(self.command(["tailscale", "status", "--json"]).stdout)
        if state.get("BackendState") != "Running":
            self.report("tailscale-preferences", "pending", "authenticated Tailscale connection unavailable; auth reset refused")
            return
        hostname = socket.gethostname().split(".", 1)[0]
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
            raise PolicyError("local node hostname unsupported")
        prefs = json.loads(self.command(["tailscale", "debug", "prefs"]).stdout)
        if prefs.get("RunSSH") is not True:
            self.report("tailscale-preferences", "pending", "SSH preference drift retained per operator instruction")
            return
        if prefs.get("Hostname") != hostname and not self.check:
            self.command(["tailscale", "set", "--hostname=" + hostname])
            prefs = json.loads(self.command(["tailscale", "debug", "prefs"]).stdout)
        self.report("tailscale-preferences", "match" if prefs.get("RunSSH") is True and prefs.get("Hostname") == hostname else "drift")

    def multipath(self):
        if not self.target("/sys/block").is_dir():
            raise PolicyError("multipath device inventory unavailable")
        maps = []
        for path in self.target("/sys/block").glob("dm-*/dm/uuid"):
            if read_text(path).strip().lower().startswith("mpath-"):
                maps.append(path)
        if maps:
            self.report("multipath", "pending", "existing multipath maps retained; storage activation refused")
            return
        for unit in ("multipathd.service", "multipathd.socket"):
            props = self.properties(unit, ["LoadState", "UnitFileState", "ActiveState"])
            if not self.check and (props.get("UnitFileState") != "masked" or props.get("ActiveState") not in ("inactive", "failed")):
                self.command(["systemctl", "mask", "--now", unit])
                props = self.properties(unit, ["LoadState", "UnitFileState", "ActiveState"])
            self.report(unit, "match" if props.get("UnitFileState") == "masked" and props.get("ActiveState") in ("inactive", "failed") else "drift")

    def watchdog(self):
        unit = self.properties("watchdog-load.service", ["LoadState", "UnitFileState", "DefaultDependencies", "After", "Before", "Type", "RemainAfterExit", "ExecStart"])
        if unit.get("UnitFileState") != "enabled" and not self.check:
            self.command(["systemctl", "enable", "watchdog-load.service"])
            unit["UnitFileState"] = self.properties("watchdog-load.service", ["UnitFileState"]).get("UnitFileState")
        unit_ok = unit.get("LoadState") == "loaded" and unit.get("UnitFileState") == "enabled" and unit.get("DefaultDependencies") == "no" and "systemd-modules-load.service" in unit.get("After", "").split() and "sysinit.target" in unit.get("Before", "").split() and unit.get("Type") == "oneshot" and unit.get("RemainAfterExit") == "yes" and self.unit_command(unit.get("ExecStart", "")) == [["/usr/local/bin/watchdog-load.sh"]]
        self.report("watchdog-unit", "match" if unit_ok else "drift")
        device = self.target("/dev/watchdog0")
        if not device.exists() and not self.check:
            self.command(["/usr/local/bin/watchdog-load.sh", "--no-reexec"], timeout=65, required=False)
        if not device.exists():
            self.report("watchdog-runtime", "pending", "no supported watchdog device available after bounded loader; explicit host activation required")
            return
        if not stat.S_ISCHR(device.stat().st_mode):
            raise PolicyError("watchdog device type unsupported")
        desired = parse_ini(read_text(self.desired("/etc/systemd/system.conf.d/10-watchdog.conf")))
        names = {"RuntimeWatchdogUSec": "RuntimeWatchdogSec", "RebootWatchdogUSec": "RebootWatchdogSec", "KExecWatchdogUSec": "KExecWatchdogSec"}
        expected = {key: duration(desired[("Manager", value)]) for key, value in names.items()}
        def owners():
            owner = other_owner = False
            for process in self.target("/proc").iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    for descriptor in (process / "fd").iterdir():
                        try:
                            metadata = descriptor.stat()
                        except FileNotFoundError:
                            continue
                        except OSError:
                            raise PolicyError("watchdog descriptor ownership inspection unavailable")
                        if stat.S_ISCHR(metadata.st_mode) and metadata.st_rdev == device.stat().st_rdev:
                            owner = owner or process.name == "1"
                            other_owner = other_owner or process.name != "1"
                except FileNotFoundError:
                    continue
                except PermissionError:
                    raise PolicyError("watchdog process ownership inspection denied")
            return owner, other_owner
        owner, other_owner = owners()
        if other_owner:
            self.report("watchdog-runtime", "pending", "watchdog held by another process; PID1 arming refused")
            return
        properties = self.properties(None, list(names) + ["WatchdogDevice"])
        configured_device = properties.get("WatchdogDevice")
        if configured_device:
            candidate = self.target(configured_device)
            if not candidate.exists() or not stat.S_ISCHR(candidate.stat().st_mode) or candidate.stat().st_rdev != device.stat().st_rdev:
                self.report("watchdog-runtime", "pending", "PID1 configured for a different watchdog device; arming refused")
                return
        for key, seconds in expected.items():
            if (duration(properties.get(key, "")) != seconds or key == "RuntimeWatchdogUSec" and not owner) and not self.check:
                self.command(["busctl", "--timeout=10", "set-property", "org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", key, "t", str(int(seconds * 1000000))], required=False)
        properties = self.properties(None, list(names))
        values_ok = all(key in properties and duration(properties[key]) == seconds for key, seconds in expected.items())
        owner, other_owner = owners()
        if self.activate and (not values_ok or not owner) and not other_owner:
            effective = parse_ini(self.command(["systemd-analyze", "cat-config", "systemd/system.conf"]).stdout)
            if any(effective.get(key) != value for key, value in desired.items()):
                self.report("watchdog-runtime", "pending", "effective PID1 watchdog configuration overridden; reexec refused")
                return
            self.command(["systemctl", "daemon-reexec"], timeout=25)
            properties = self.properties(None, list(names))
            values_ok = all(key in properties and duration(properties[key]) == seconds for key, seconds in expected.items())
            owner, other_owner = owners()
        self.report("watchdog-runtime", "match" if values_ok and owner and not other_owner else "pending", None if values_ok and owner and not other_owner else "PID1 watchdog values or exclusive device ownership not proved; reexec refused")

    def logging(self):
        expected = parse_ini(read_text(self.desired("/etc/systemd/journald.conf.d/10-limits.conf")))
        effective = parse_ini(self.command(["systemd-analyze", "cat-config", "systemd/journald.conf"]).stdout)
        if any(effective.get(key) != value for key, value in expected.items()):
            self.report("journald", "drift", "effective journald configuration overridden")
        else:
            self.activate_log("systemd-journald.service", ["/etc/systemd/journald.conf.d/10-limits.conf"], "journald")
        service = self.properties("rsyslog.service", ["LoadState"])
        if service.get("LoadState") == "not-found":
            self.report("rsyslog", "match", "optional rsyslog consumer absent")
            return
        config = read_text(self.target("/etc/rsyslog.conf"))
        if not (re.search(r"\$IncludeConfig\s+/etc/rsyslog\.d/\*\.conf", config) or re.search(r'include\s*\(\s*file\s*=\s*"/etc/rsyslog\.d/\*\.conf"', config)):
            raise PolicyError("rsyslog managed include not proved")
        self.command(["rsyslogd", "-N1", "-f", "/etc/rsyslog.conf"])
        self.activate_log("rsyslog.service", ["/etc/rsyslog.conf", "/etc/rsyslog.d/10-k3s.conf"], "rsyslog")

    def activate_log(self, unit, paths, item):
        def current():
            properties = self.properties(unit, ["ActiveState", "MainPID"])
            pid = properties.get("MainPID", "0")
            return properties.get("ActiveState") == "active" and pid.isdigit() and int(pid) > 1 and self.process_start(pid) >= max(self.target(path).stat().st_mtime for path in paths)
        if not current() and not self.check:
            self.command(["systemctl", "restart", unit], timeout=30)
        self.report(item, "match" if current() else "pending", None if current() else "logging process has not loaded managed configuration")

    def ha(self):
        legacy = self.target("/etc/rancher/k3s/config.yaml.d/20-leader-election.yaml")
        active = self.target(HA_PATH)
        if legacy.is_symlink() or active.is_symlink():
            raise PolicyError("managed HA destination is a symlink")
        if legacy.exists():
            if not legacy.is_file():
                raise PolicyError("obsolete HA destination is not a regular file")
            if self.check:
                self.report("obsolete-ha", "drift")
            else:
                legacy.unlink()
                self.ha_changed = True
        template = read_text(self.desired(HA_TEMPLATE))
        if self.role == "server":
            changed = self.atomic_file(HA_PATH, template, 0o644)
            self.ha_changed = self.ha_changed or changed
            self.report("server-ha-file", "drift" if self.check and changed else "match")
        elif active.exists():
            if yaml.safe_load(read_text(active)) != yaml.safe_load(template):
                raise PolicyError("unrecognised agent HA content retained")
            if self.check:
                self.report("agent-ha-file", "drift")
            else:
                active.unlink()
                self.ha_changed = True
                self.report("agent-ha-file")
        else:
            self.report("agent-ha-file")

    def parse_flags(self, arguments):
        flags = {}
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value.startswith("--"):
                key, separator, argument = value[2:].partition("=")
                if not separator and index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                    index += 1
                    argument = arguments[index]
                elif not separator:
                    argument = True
                flags.setdefault(key, []).append(argument)
            index += 1
        return flags

    def k3s(self):
        self.k3s_ready = False
        unit = "k3s.service" if self.role == "server" else "k3s-agent.service"
        properties = self.properties(unit, ["LoadState", "ActiveState", "SubState", "MainPID", "ControlGroup", "ExecStart", "Environment", "EnvironmentFiles", "FragmentPath", "DropInPaths"])
        pid = properties.get("MainPID", "0")
        if properties.get("LoadState") == "not-found" or properties.get("ActiveState") != "active" or properties.get("SubState") != "running" or not pid.isdigit() or int(pid) <= 1:
            self.report("k3s-runtime", "pending", "K3s inactive or join not complete; start/restart and membership changes refused")
            return
        process = self.target("/proc/" + pid)
        if Path(os.readlink(process / "exe")).name not in ("k3s", "k3s-server", "k3s-agent"):
            raise PolicyError("K3s process identity not proved")
        group = properties.get("ControlGroup", "")
        if not group or not any(line.split(":", 2)[-1] == group for line in read_text(process / "cgroup").splitlines()):
            raise PolicyError("K3s service process ownership not proved")
        running = (process / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
        installed = self.unit_command(properties.get("ExecStart", ""))
        if len(installed) != 1 or not installed[0] or Path(installed[0][0]).name != "k3s" or self.role not in installed[0][1:3]:
            raise PolicyError("loaded K3s command role not proved")
        titled = len(running) == 1 and running[0].strip() == installed[0][0] + " " + self.role
        if not titled and self.role not in running[1:3]:
            raise PolicyError("K3s effective command role differs")
        environment = {}
        for entry in shlex.split(properties.get("Environment", "")):
            key, separator, value = entry.partition("=")
            if separator:
                environment[key] = value
        fragment = properties.get("FragmentPath", "")
        if not fragment.startswith("/"):
            raise PolicyError("loaded K3s startup source unavailable")
        main_unit = self.target(fragment)
        if main_unit.is_symlink() or not main_unit.is_file():
            raise PolicyError("loaded K3s startup source is not a regular file")
        paths = [main_unit]
        for name in shlex.split(properties.get("DropInPaths", "")):
            if not name.startswith("/"):
                raise PolicyError("loaded K3s drop-in source unsupported")
            path = self.target(name)
            if re.search(r"(?m)^\s*(ExecStart|Environment|EnvironmentFile)\s*=", read_text(path)):
                paths.append(path)
        files = re.findall(r"(\S+)\s+\(ignore_errors=(?:yes|no)\)", properties.get("EnvironmentFiles", ""))
        if properties.get("EnvironmentFiles") and not files:
            raise PolicyError("K3s environment-file precedence unsupported")
        for name in files:
            path = self.target(name)
            if not path.exists():
                continue
            paths.append(path)
            for line in read_text(path).splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = shlex.split(line)
                if len(parts) != 1 or "=" not in parts[0]:
                    raise PolicyError("K3s environment-file syntax unsupported")
                key, value = parts[0].split("=", 1)
                environment[key] = value
        running_environment = dict(entry.split("=", 1) for entry in (process / "environ").read_bytes().decode().split("\0") if "=" in entry)
        config_name = environment.get("K3S_CONFIG_FILE", "/etc/rancher/k3s/config.yaml")
        if not config_name.startswith("/"):
            raise PolicyError("K3s config path unsupported")
        config_path = self.target(config_name)
        config_files = ([config_path] if config_path.exists() else []) + sorted(self.target(config_name + ".d").glob("*.yaml"))
        config = {}
        for path in config_files:
            document = yaml.safe_load(read_text(path)) or {}
            if not isinstance(document, dict):
                raise PolicyError("K3s configuration syntax unsupported")
            for key, value in document.items():
                append = key.endswith("+")
                key = key.rstrip("+")
                if append and key in config:
                    if not isinstance(value, list) or not isinstance(config[key], list):
                        raise PolicyError("K3s append syntax unsupported")
                    config[key] += value
                else:
                    config[key] = value
            paths.append(path)
        def effective(arguments, env):
            result = {key: value if isinstance(value, list) else [value] for key, value in config.items()}
            for key, value in env.items():
                if key.startswith("K3S_"):
                    name = key[4:].lower().replace("_", "-")
                    result[name] = value.split(",") if name in ("disable", "tls-san") else [value]
            result.update(self.parse_flags(arguments))
            return result
        disk = effective(installed[0], environment)
        live = effective(installed[0] if titled else running, running_environment)
        ip = self.command(["tailscale", "ip", "-4"]).stdout.strip().splitlines()
        if len(ip) != 1 or ipaddress.ip_address(ip[0]).version != 4:
            raise PolicyError("Tailscale node address unavailable")
        expected = {"node-ip": ip[0], "node-external-ip": ip[0], "flannel-iface": "tailscale0", "resolv-conf": "/etc/k3s-resolv.conf"}
        def policy_matches(values):
            if any(values.get(key) != [value] for key, value in expected.items()):
                return False
            if "allowed-unsafe-sysctls=net.ipv4.ip_forward" not in values.get("kubelet-arg", []):
                return False
            if self.role == "server":
                if values.get("secrets-encryption") not in ([True], ["true"]):
                    return False
                if values.get("write-kubeconfig-mode") not in (["0644"], ["644"], [644]):
                    return False
                if "servicelb" not in values.get("disable", []):
                    return False
                desired_ha = yaml.safe_load(read_text(self.desired(HA_TEMPLATE)))
                if any(values.get(key) != value for key, value in desired_ha.items()):
                    return False
            return True
        if not policy_matches(disk):
            self.report("k3s-runtime", "drift", "installed non-secret K3s flag or HA precedence differs; identity/config rewrite refused")
            return
        active_template = self.target(HA_PATH)
        if self.role == "server" and active_template not in paths:
            self.report("k3s-runtime", "drift", "active K3s config path does not consume managed HA template")
            return
        restart_reason = None
        if self.role == "server":
            required_sans = [ip[0]]
            if self.platform == "physical":
                required_sans.append(socket.gethostname().split(".", 1)[0] + ".local")
                route = self.command(["ip", "-4", "route", "get", "1.1.1.1"], required=False)
                source = re.search(r"\bsrc\s+([0-9.]+)\b", route.stdout)
                if route.returncode or not source:
                    self.report("k3s-tls", "pending", "physical LAN address unavailable; SAN policy not guessed")
                    return
                required_sans.append(str(ipaddress.ip_address(source[1])))
            certificate = self.command(["openssl", "x509", "-in", "/var/lib/rancher/k3s/server/tls/serving-kube-apiserver.crt", "-noout", "-ext", "subjectAltName"], required=False)
            if certificate.returncode:
                self.report("k3s-tls", "pending", "public server certificate SAN evidence unavailable")
                return
            actual_sans = set(re.findall(r"(?:DNS:|IP Address:)([^,\s]+)", certificate.stdout))
            if any(value not in actual_sans for value in required_sans):
                sans = disk.get("tls-san", [])
                if not all(isinstance(value, str) for value in sans):
                    raise PolicyError("TLS SAN configuration syntax unsupported")
                if not all(value in sans for value in required_sans):
                    if "tls-san" in self.parse_flags(installed[0]) or "K3S_TLS_SAN" in environment:
                        self.report("k3s-tls", "pending", "protected service TLS SAN override requires explicit correction; credentials retained")
                        return
                    tls_path = "/etc/rancher/k3s/config.yaml.d/30-node-tls.yaml"
                    existing = self.target(tls_path)
                    if existing.exists():
                        owned = yaml.safe_load(read_text(existing))
                        if not isinstance(owned, dict) or set(owned) != {"tls-san"}:
                            raise PolicyError("unrecognised TLS policy fragment retained")
                    later_override = any(path.name > "30-node-tls.yaml" and any(key.rstrip("+") == "tls-san" for key in (yaml.safe_load(read_text(path)) or {})) for path in config_files if path != config_path)
                    if later_override:
                        self.report("k3s-tls", "pending", "later TLS SAN override requires explicit correction; credentials retained")
                        return
                    self.atomic_file(tls_path, yaml.safe_dump({"tls-san": list(dict.fromkeys(sans + required_sans))}, sort_keys=False), 0o644)
                restart_reason = "additional managed TLS SAN policy or certificate requires activation"
            else:
                self.report("k3s-tls")
        newer = any(path.stat().st_mtime > self.process_start(pid) for path in paths)
        if self.ha_changed or newer or not policy_matches(live):
            restart_reason = restart_reason or "managed HA or argument configuration newer than running K3s"
        if self.nvidia_enabled and self.target("/usr/bin/nvidia-container-runtime").is_file() and not self.nvidia_runtime_registered():
            restart_reason = "NVIDIA container runtime requires K3s registration"
        kubeconfig = "/etc/rancher/k3s/k3s.yaml" if self.role == "server" else "/var/lib/rancher/k3s/agent/kubelet.kubeconfig"
        hostname = socket.gethostname().split(".", 1)[0]
        ready = self.command(["k3s", "kubectl", "--kubeconfig", kubeconfig, "get", "node", hostname, "-o", "json", "--request-timeout=10s"], timeout=15, required=False)
        if ready.returncode:
            self.report("k3s-runtime", "pending", "node registration/readiness unavailable; blocked join is not parity")
            return
        node = json.loads(ready.stdout)
        conditions = node.get("status", {}).get("conditions", [])
        is_ready = node.get("metadata", {}).get("name") == hostname and any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions)
        if not is_ready:
            self.report("k3s-runtime", "pending", "node not registered Ready; restart cannot fix a blocked join")
            return
        if titled:
            addresses = node.get("status", {}).get("addresses", [])
            internal = {value.get("address") for value in addresses if value.get("type") == "InternalIP"}
            external = {value.get("address") for value in addresses if value.get("type") == "ExternalIP"}
            if ip[0] not in internal or external and ip[0] not in external:
                self.report("k3s-runtime", "pending", "registered node address evidence does not confirm titled-process policy; no identity rewrite")
                return
        self.k3s_ready = True
        if restart_reason:
            blocked = any(record["state"] in ("unknown", "drift") or record["item"] in ("physical-netplan", "network") and record["state"] != "match" for record in self.records)
            if not self.activate or blocked:
                self.report("k3s-runtime", "pending", restart_reason + "; explicit safe activation required")
                return
            old_start = self.process_start(pid)
            self.command(["systemctl", "restart", unit], timeout=90)
            active = self.properties(unit, ["ActiveState", "SubState", "MainPID"])
            new_pid = active.get("MainPID", "0")
            if active.get("ActiveState") != "active" or active.get("SubState") != "running" or not new_pid.isdigit() or int(new_pid) <= 1 or self.process_start(new_pid) <= old_start:
                self.report("k3s-runtime", "pending", "authorized K3s restart not active; no repeated restart or membership action")
                return
            self.ha_changed = False
            self.activate = False
            try:
                self.k3s()
            finally:
                self.activate = True
            return
        self.report("k3s-runtime")

    def netplan_optional_wait_disabled(self):
        unit = "systemd-networkd-wait-online.service"
        dropin = "/run/systemd/generator.late/" + unit + ".d/10-netplan.conf"
        condition_path = "/run/systemd/generator/network-online.target.wants/" + unit
        path = self.target(dropin)
        if not path.exists() and not path.is_symlink():
            return False
        if parse_ini(read_text(path)).get(("Unit", "ConditionPathIsSymbolicLink")) != condition_path:
            return False
        properties = self.properties(unit, ["LoadState", "DropInPaths", "NeedDaemonReload"])
        if properties.get("LoadState") != "loaded" or properties.get("NeedDaemonReload") != "no" or dropin not in shlex.split(properties.get("DropInPaths", "")):
            return False
        response = self.command([
            "busctl", "--json=short", "--timeout=5", "get-property", "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1/unit/systemd_2dnetworkd_2dwait_2donline_2eservice",
            "org.freedesktop.systemd1.Unit", "Conditions",
        ], timeout=6)
        payload = json.loads(response.stdout)
        conditions = payload.get("data")
        if payload.get("type") != "a(sbbsi)" or not isinstance(conditions, list) or any(not isinstance(row, list) or len(row) != 5 for row in conditions):
            raise PolicyError("loaded network-online condition report unsupported")
        if not any(row[0] == "ConditionPathIsSymbolicLink" and row[1] is False and row[2] is False and row[3] == condition_path for row in conditions):
            return False
        enablement = self.target(condition_path)
        return not enablement.exists() and not enablement.is_symlink()

    def network_control(self):
        state = json.loads(self.command(["tailscale", "status", "--json"], timeout=5).stdout)
        addresses = self.command(["tailscale", "ip", "-4"], timeout=5).stdout.strip().splitlines()
        if state.get("BackendState") != "Running" or len(addresses) != 1:
            raise PolicyError("authenticated Tailscale control connection unavailable")
        peer = None
        for value in (state.get("Peer") or {}).values():
            if value.get("Online"):
                peer = next((address for address in value.get("TailscaleIPs", []) if ":" not in address), None)
                if peer:
                    break
        if not peer or self.command(["tailscale", "ping", "--tsmp", "-c", "1", "--timeout=5s", peer], timeout=7, required=False).returncode:
            raise PolicyError("reachable Tailscale peer required for guarded activation")
        return addresses[0], peer

    def network_control_matches(self, control):
        state = self.command(["tailscale", "status", "--json"], timeout=4, required=False)
        address = self.command(["tailscale", "ip", "-4"], timeout=4, required=False)
        if state.returncode or address.returncode or json.loads(state.stdout).get("BackendState") != "Running" or address.stdout.strip() != control[0]:
            return False
        return self.command(["tailscale", "ping", "--tsmp", "-c", "1", "--timeout=3s", control[1]], timeout=5, required=False).returncode == 0

    def network_matches(self, desired):
        interfaces = json.loads(self.command(["ip", "-j", "-4", "address", "show"], timeout=4).stdout)
        routes = json.loads(self.command(["ip", "-j", "-4", "route", "show", "default"], timeout=4).stdout)
        actual = {value["ifname"]: value for value in interfaces}
        generated = [parse_ini(read_text(path)) for path in self.target("/run/systemd/network").glob("*netplan*.network")]
        optional_wait_disabled = None
        relevant_routes = 0
        for kind in ("ethernets", "wifis"):
            for identifier, link in desired[kind].items():
                selector = link.get("match", {}).get("name", identifier)
                candidates = [name for name in actual if fnmatch.fnmatch(name, selector)]
                policy = [value for value in generated if value.get(("Match", "Name")) == selector]
                metric = link.get("dhcp4-overrides", {}).get("route-metric")
                policy = [value for value in policy if (value.get(("Network", "DHCP")) in ("yes", "ipv4")) == link["dhcp4"] and (not link["dhcp4"] or value.get(("DHCPv4", "RouteMetric"), value.get(("DHCP", "RouteMetric"))) == str(metric))]
                if not policy:
                    return False
                legacy_optional = any((value.get(("Link", "RequiredForOnline")) == "no") == link["optional"] for value in policy)
                if not legacy_optional:
                    if not link["optional"]:
                        return False
                    if optional_wait_disabled is None:
                        optional_wait_disabled = self.netplan_optional_wait_disabled()
                    if not optional_wait_disabled:
                        return False
                if not candidates and not link["optional"]:
                    return False
                for name in candidates:
                    details = json.loads(self.command(["networkctl", "--json=short", "--no-pager", "status", name], timeout=4).stdout)
                    if details.get("RequiredForOnline") is not (not link["optional"]):
                        if not link["optional"]:
                            return False
                        if optional_wait_disabled is None:
                            optional_wait_disabled = self.netplan_optional_wait_disabled()
                        if not optional_wait_disabled:
                            return False
                    present = {str(address["local"]) + "/" + str(address["prefixlen"]) for address in actual[name].get("addr_info", []) if address.get("family") == "inet"}
                    if not set(link.get("addresses", [])).issubset(present):
                        return False
                    if link.get("nameservers"):
                        output = self.command(["resolvectl", "dns", name], timeout=4).stdout
                        servers = set(re.findall(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])", output))
                        if not set(link["nameservers"]["addresses"]).issubset(servers):
                            return False
                    for route in (route for route in routes if route.get("dev") == name):
                        relevant_routes += 1
                        if link["dhcp4"]:
                            if route.get("metric", 0) != metric or route.get("protocol") != "dhcp":
                                return False
                        elif not any(expected["to"] == "default" and expected["via"] == route.get("gateway") and expected["metric"] == route.get("metric", 0) for expected in link.get("routes", [])):
                            return False
                    if link.get("routes") and not any(route.get("dev") == name for route in routes):
                        return False
        return relevant_routes > 0

    def network_input(self):
        files = {}
        for directory in ("/lib/netplan", "/etc/netplan", "/run/netplan"):
            for path in self.target(directory).glob("*.yaml"):
                files[path.name] = path
        documents = {}
        producers = []
        for path in files.values():
            if path.is_symlink() or not path.is_file():
                raise NetworkConflict("netplan producer provenance is not a regular file; all inputs retained")
            document = yaml.safe_load(read_text(path))
            if not isinstance(document, dict) or not isinstance(document.get("network"), dict):
                raise NetworkConflict("netplan input structure is unsupported; all inputs retained")
            documents[path] = document
            access_points = document["network"].get("wifis", {}).get("wlan0", {}).get("access-points")
            if access_points:
                producers.append(path)
        if not producers:
            raise PolicyError("authoritative historical Wi-Fi credentials unavailable; replacement refused")
        if len(producers) != 1 or producers[0].parent != self.target("/etc/netplan"):
            raise NetworkConflict("authoritative Wi-Fi producer is ambiguous or read-only; all definitions and credentials retained")
        path = producers[0]
        return path, documents[path], files, documents

    def network_candidate(self, directory, content, producer):
        root = Path(directory)
        for source_directory in ("/lib/netplan", "/etc/netplan", "/run/netplan"):
            destination = root / source_directory.lstrip("/")
            destination.mkdir(parents=True, exist_ok=True)
            for source in self.target(source_directory).glob("*.yaml"):
                candidate = destination / source.name
                candidate.write_text(read_text(source))
                candidate.chmod(0o600)
        target = root / producer.relative_to(ROOT)
        target.write_text(content)
        target.chmod(0o600)
        self.command(["netplan", "generate", "--root-dir", str(root)], timeout=25, umask=NETPLAN_UMASK)

    def activate_network(self, content, desired, target):
        control = self.network_control()
        original = read_text(target)
        logical_target = "/" + str(target.relative_to(ROOT))
        original_document = yaml.safe_load(original)["network"]
        original_addresses = {address for kind in ("ethernets", "wifis") for link in original_document.get(kind, {}).values() for address in link.get("addresses", [])}
        self.target("/run").mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="nodes-network-", dir=self.target("/run")))
        retained = False
        unit = "nodes-network-rollback-" + uuid.uuid4().hex[:12]
        marker = directory / "pending"
        backup = directory / "previous.yaml"
        lock = directory / "lock"
        try:
            self.network_candidate(directory / "previous", original, target)
            self.network_candidate(directory / "candidate", content, target)
            backup.write_text(original)
            backup.chmod(0o600)
            marker.touch(mode=0o600)
            script = directory / "rollback.sh"
            destination = str(target)
            script.write_text(
                "#!/bin/sh\nset -eu\nexec 9>" + shlex.quote(str(lock)) + "\nflock -x 9\n"
                "test -f " + shlex.quote(str(marker)) + " || exit 0\n"
                "install -m 0600 " + shlex.quote(str(backup)) + " " + shlex.quote(destination + ".nodes-rollback") + "\n"
                "mv -f " + shlex.quote(destination + ".nodes-rollback") + " " + shlex.quote(destination) + "\n"
                "(umask " + format(NETPLAN_UMASK, "03o") + "; timeout 40s netplan apply)\nrm -f " + shlex.quote(str(marker)) + "\n"
            )
            script.chmod(0o700)
            self.command(["systemd-run", "--quiet", "--unit=" + unit, "--on-active=120s", "--timer-property=AccuracySec=1s", "/bin/sh", str(script)], timeout=15)
            if self.properties(unit + ".timer", ["ActiveState"]).get("ActiveState") != "active":
                raise PolicyError("independent network rollback timer not armed")
            retained = True
            self.atomic_file(logical_target, content, 0o600)
            def verified():
                deadline = time.monotonic() + 22
                while time.monotonic() < deadline:
                    try:
                        if self.network_matches(desired) and self.network_control_matches(control):
                            return True
                    except (PolicyError, ValueError, OSError):
                        pass
                    time.sleep(1)
                return False
            result = run_netplan_try(verified)
            success = result.returncode == 0 and verified()
            if not success:
                self.atomic_file(logical_target, original, 0o600)
                recovery = self.command(["netplan", "apply"], timeout=45, required=False, umask=NETPLAN_UMASK)
                addresses = json.loads(self.command(["ip", "-j", "-4", "address", "show"], timeout=5).stdout)
                present = {str(address["local"]) + "/" + str(address["prefixlen"]) for interface in addresses for address in interface.get("addr_info", []) if address.get("family") == "inet"}
                if recovery.returncode or not original_addresses.issubset(present) or not self.network_control_matches(control):
                    raise PolicyError("network activation failed; independent validated rollback retained")
            with lock.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                if not marker.exists():
                    raise PolicyError("network rollback raced activation; no parity claim")
                marker.unlink()
            self.command(["systemctl", "stop", unit + ".timer", unit + ".service"], timeout=15, required=False)
            retained = False
            if not success:
                raise PolicyError("guarded network activation failed and was rolled back; desired policy not active")
        finally:
            if not retained:
                self.command(["systemctl", "stop", unit + ".timer", unit + ".service"], timeout=15, required=False)
                shutil.rmtree(directory)

    def merge_network_links(self, candidate, desired):
        names = None
        def targets(identifier, link):
            nonlocal names
            if not isinstance(link, dict) or "set-name" in link:
                return None
            if names is None:
                interfaces = json.loads(self.command(["ip", "-j", "link", "show"], timeout=4).stdout)
                names = {value["ifname"] for value in interfaces}
            if "match" in link:
                match = link["match"]
                if not isinstance(match, dict) or set(match) != {"name"} or not isinstance(match["name"], str):
                    return None
                return {name for name in names if fnmatch.fnmatch(name, match["name"])}
            return {identifier} if identifier in names else set()
        ethernet = candidate.setdefault("ethernets", {})
        for identifier, link in desired["ethernets"].items():
            for alias in {"ethernet", "eth0"} - set(desired["ethernets"]):
                if alias not in ethernet:
                    continue
                previous = ethernet[alias]
                old_targets = targets(alias, previous)
                new_targets = targets(identifier, link)
                if old_targets and new_targets and old_targets.isdisjoint(new_targets):
                    continue
                if old_targets is None or new_targets is None or len(old_targets) != 1 or old_targets != new_targets:
                    raise NetworkConflict("alternate Ethernet definition cannot be proved to select the same link; all definitions retained")
                migrated = json.loads(json.dumps(previous))
                if "match" not in link:
                    migrated.pop("match", None)
                if identifier in ethernet and ethernet[identifier] != migrated:
                    raise NetworkConflict("multiple definitions of the same Ethernet link need explicit consolidation; all data retained")
                ethernet[identifier] = migrated
                del ethernet[alias]
        for kind in ("ethernets", "wifis"):
            links = candidate.setdefault(kind, {})
            if not isinstance(links, dict):
                raise NetworkConflict("existing network link namespace is not a mapping; all data retained")
            for identifier, link in desired[kind].items():
                if identifier not in links:
                    links[identifier] = {}
                merge_owned_fields(links[identifier], link, (kind, identifier))

    def network(self):
        path = self.target("/etc/netplan/50-cloud-init.yaml")
        if self.platform == "physical":
            try:
                path, document, files, documents = self.network_input()
            except NetworkConflict as error:
                self.report("physical-netplan", "pending", str(error))
                return
        if path.is_symlink() or path.exists() and not path.is_file():
            raise PolicyError("owned netplan destination is not a regular file")
        mode_drift = path.exists() and stat.S_IMODE(path.stat().st_mode) != 0o600
        if mode_drift and not self.check:
            os.chmod(path, 0o600)
        self.report("netplan-permissions", "drift" if mode_drift and self.check else "match")
        if self.platform != "physical":
            self.report("physical-network", "match", "not applicable to cloud")
            return
        disabled = self.atomic_file("/etc/cloud/cloud-init.disabled", "", 0o644)
        self.report("physical-cloud-init", "drift" if self.check and disabled else "match")
        desired = network_policy(self.policy, socket.gethostname().split(".", 1)[0])
        net = document["network"]
        credentials = net.get("wifis", {}).get("wlan0", {}).get("access-points")
        def credential_present(value):
            return isinstance(value, dict) and (isinstance(value.get("password"), str) and bool(value["password"]) or isinstance(value.get("auth"), dict) and value["auth"].get("key-management") == "psk" and isinstance(value["auth"].get("password"), str) and bool(value["auth"]["password"]))
        if not isinstance(credentials, dict) or not credentials or not all(isinstance(name, str) and credential_present(value) for name, value in credentials.items()):
            raise PolicyError("authoritative historical Wi-Fi credentials unavailable; replacement refused")
        desired["wifis"]["wlan0"]["access-points"] = credentials
        candidate = json.loads(json.dumps(document))
        candidate_net = candidate["network"]
        candidate_net["version"] = desired["version"]
        try:
            self.merge_network_links(candidate_net, desired)
        except NetworkConflict as error:
            self.report("physical-netplan", "pending", str(error))
            return
        changed = candidate != document
        content = yaml.safe_dump(candidate, sort_keys=False)
        merged = {}
        for current in files.values():
            if current == path:
                continue
            if "access-points" in documents[current]["network"].get("wifis", {}).get("wlan0", {}):
                raise NetworkConflict("multiple credential producers retained; network activation refused")
        for name in sorted(files):
            merge_mapping(merged, candidate if files[name] == path else documents[files[name]])
        effective = merged.get("network", {})
        parity = owned_projection_matches(effective, desired)
        if not parity:
            self.report("physical-netplan", "drift", "unmanaged netplan precedence conflicts with canonical per-node profile")
        elif changed or not self.network_matches(desired):
            if self.activate:
                self.activate_network(content, desired, path)
                self.report("physical-netplan", "match" if self.network_matches(desired) else "drift")
            else:
                with tempfile.TemporaryDirectory(prefix="nodes-netplan-") as temporary:
                    self.network_candidate(temporary, content, path)
                self.report("physical-netplan", "pending", "canonical static-preserving profile validated privately; explicit --activate required")
        else:
            self.report("physical-netplan")
        if not self.target("/sys/class/net/wlan0").exists():
            self.report("physical-wifi", "pending", "optional Wi-Fi interface unavailable; no forced rename/link cycle")
            return
        if not self.check:
            self.command(["udevadm", "control", "--reload"], timeout=15)
        power = self.command(["iw", "dev", "wlan0", "get", "power_save"]).stdout
        if "Power save: off" not in power and not self.check:
            self.command(["iw", "dev", "wlan0", "set", "power_save", "off"])
            power = self.command(["iw", "dev", "wlan0", "get", "power_save"]).stdout
        self.report("physical-wifi", "match" if "Power save: off" in power else "pending")

    def chrony_sources(self, text):
        result = {}
        for line in text.splitlines():
            fields = shlex.split(line, comments=True)
            if not fields or fields[0] == "makestep":
                continue
            if fields[0] not in ("server", "pool") or len(fields) < 2 or not re.fullmatch(r"[A-Za-z0-9.:-]+", fields[1]):
                raise PolicyError("unsupported managed chrony directive")
            if fields[1] in result:
                raise PolicyError("duplicate managed chrony source")
            result[fields[1]] = fields
        if not result:
            raise PolicyError("managed chrony sources unavailable")
        return result

    def chrony_snapshot(self):
        def rows(command):
            return list(csv.reader(io.StringIO(self.command(["chronyc", "-n", "-c", *command], timeout=10).stdout)))
        sources = rows(["sources", "-a"])
        selection_rows = rows(["selectdata", "-a"])
        if any(len(row) != 18 for row in selection_rows):
            raise PolicyError("chrony CSV selection report unsupported")
        selection = {row[1]: row[:3] + ["".join(row[3:8]), "".join(row[8:13])] + row[13:] for row in selection_rows}
        auth = {row[0]: row for row in rows(["authdata", "-a"]) if len(row) >= 10}
        result = []
        for row in sources:
            if len(row) < 7 or row[0] not in ("^", "=", "#"):
                raise PolicyError("chrony source report unsupported")
            address = row[2]
            original = self.command(["chronyc", "-n", "sourcename", address], timeout=10).stdout.strip()
            if not re.fullmatch(r"[A-Za-z0-9.:-]+", original):
                raise PolicyError("chrony original source identity unavailable")
            result.append({"address": address, "name": original, "state": row[1], "reach": row[5], "selection": selection.get(address), "auth": auth.get(address)})
        return result

    def chrony_private_file(self, path):
        metadata = path.stat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 2 * 1024 * 1024:
            raise PolicyError("private managed chrony file metadata unproved")

    def chrony_pending_file(self, path):
        self.chrony_private_file(path)
        if not re.fullmatch(r"[0-9a-f]{64}\.conf", path.name):
            raise PolicyError("pending managed chrony snapshot name unproved")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() + ".conf" != path.name:
            raise PolicyError("pending managed chrony snapshot integrity unproved")
        return self.chrony_sources(content.decode("utf-8"))

    def chrony_pending_context(self):
        directory = self.target(CHRONY_PENDING_DIR)
        history, snapshots = {}, []
        if directory.exists() or directory.is_symlink():
            metadata = directory.stat()
            if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise PolicyError("pending managed chrony directory ownership unproved")
            for entry in directory.iterdir():
                if entry.name.startswith(".snapshot-") and entry.name.endswith(".tmp"):
                    self.chrony_private_file(entry)
                elif entry.suffix != ".conf":
                    raise PolicyError("unknown pending managed chrony entry retained")
            for path in sorted(directory.glob("*.conf")):
                for name, fields in self.chrony_pending_file(path).items():
                    versions = history.setdefault(name, [])
                    if fields not in versions:
                        versions.append(fields)
                snapshots.append(path)
        progress = {}
        path = self.target(CHRONY_PROGRESS_PATH)
        if path.exists() or path.is_symlink():
            self.chrony_private_file(path)
            parent = path.parent.stat()
            if path.parent.is_symlink() or parent.st_uid != 0 or parent.st_gid != 0 or stat.S_IMODE(parent.st_mode) & 0o022:
                raise PolicyError("managed chrony progress ownership unproved")
            payload = json.loads(read_text(path))
            if not isinstance(payload, dict) or set(payload) != {"version", "sources"} or payload["version"] != 1 or not isinstance(payload["sources"], dict) or any(not isinstance(fields, list) or not all(isinstance(field, str) for field in fields) for fields in payload["sources"].values()):
                raise PolicyError("managed chrony progress schema unsupported")
            if payload["sources"]:
                progress = self.chrony_sources("\n".join(shlex.join(fields) for fields in payload["sources"].values()))
            if progress != payload["sources"]:
                raise PolicyError("managed chrony progress source identity unproved")
        return history, snapshots, progress

    def save_chrony_progress(self, progress):
        if self.check:
            return
        parent = self.target(CHRONY_PROGRESS_PATH).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = parent.stat()
        if parent.is_symlink() or metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PolicyError("managed chrony progress directory ownership unproved")
        self.atomic_file(CHRONY_PROGRESS_PATH, json.dumps({"version": 1, "sources": progress}, sort_keys=True) + "\n", 0o600)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def clear_chrony_pending(self, snapshots):
        if self.check:
            return
        directory = self.target(CHRONY_PENDING_DIR)
        if set(directory.glob("*.conf")) != set(snapshots):
            raise PolicyError("managed chrony ownership context changed; retained")
        for path in snapshots:
            self.chrony_pending_file(path)
        for path in snapshots:
            path.unlink()
        progress = self.target(CHRONY_PROGRESS_PATH)
        if progress.exists() or progress.is_symlink():
            progress.unlink()

    def chrony(self):
        desired = self.chrony_sources(read_text(self.desired(CHRONY_PATH)))
        history, snapshots, progress = self.chrony_pending_context()
        owned = set(desired) | set(history)
        expanded = self.command(["chronyd", "-p", "-f", "/etc/chrony/chrony.conf"]).stdout
        expanded_lines = [shlex.split(line, comments=True) for line in expanded.splitlines()]
        if any(fields not in expanded_lines for fields in desired.values()):
            self.report("chrony", "drift", "effective chrony configuration does not consume managed source policy")
            return
        snapshot = self.chrony_snapshot()
        def family(source):
            try:
                return ipaddress.ip_address(source["address"]).version
            except ValueError:
                return None
        def wrong_family(source, fields):
            return "ipv4" in fields and family(source) not in (None, 4)
        def responding(source):
            return source["reach"] not in ("0", "000")
        def policy_matches(source, fields):
            selection, auth = source["selection"], source["auth"]
            if not selection or not auth or wrong_family(source, fields):
                return False
            configured = selection[3]
            effective = selection[4]
            options = {"noselect": "N", "prefer": "P", "trust": "T", "require": "R"}
            return all((name in fields) == (letter in configured) for name, letter in options.items()) and all(letter in effective for name, letter in options.items() if name in fields) and auth[1] == ("NTS" if "nts" in fields else "-") and ("nts" in fields or selection[2] == "N")
        def authenticated(source):
            return source["selection"] and source["selection"][2] == "Y" and source["auth"] and source["auth"][1] == "NTS" and len(source["auth"]) > 8 and source["auth"][8].isdigit() and int(source["auth"][8]) > 0 and responding(source)
        def nts_ready(current):
            return all(any(source["name"] == name and policy_matches(source, fields) and authenticated(source) and ("ipv4" not in fields or family(source) == 4) for source in current) for name, fields in desired.items() if "nts" in fields)
        def transport(fields):
            return [field for field in fields if field not in {"noselect", "prefer", "trust", "require", "nts"}]
        def transport_pending(name, fields):
            return any(transport(old) != transport(fields) for old in history.get(name, [])) and (name not in progress or transport(progress[name]) != transport(fields))
        def applied(name, fields):
            progress[name] = fields
            self.save_chrony_progress(progress)
        def restore_online(current):
            candidates = [source for source in current if source["name"] in desired and family(source) is not None and not responding(source) and not wrong_family(source, desired[source["name"]]) and "offline" not in desired[source["name"]]]
            if not candidates:
                return False
            activity = self.command(["chronyc", "-n", "-c", "activity"], timeout=5).stdout.strip().split(",")
            if len(activity) != 5 or not all(value.isdigit() for value in activity):
                raise PolicyError("managed chrony connectivity inventory unavailable")
            if int(activity[1]) == 0:
                return False
            links = {}
            restored = False
            for source in candidates:
                result = self.command(["ip", "-j", "route", "get", source["address"]], timeout=5, required=False)
                if result.returncode:
                    continue
                for route in json.loads(result.stdout):
                    device = route.get("dev")
                    address = route.get("prefsrc", route.get("src"))
                    if route.get("type", "unicast") != "unicast" or not device or not address:
                        continue
                    address = ipaddress.ip_address(address)
                    if address.version != family(source) or address.is_unspecified or address.is_loopback or address.is_multicast:
                        continue
                    if device not in links:
                        result = self.command(["ip", "-j", "link", "show", "dev", device], timeout=5, required=False)
                        links[device] = result.returncode == 0 and any(link.get("ifname") == device and "UP" in link.get("flags", []) and link.get("operstate") in ("UP", "UNKNOWN") for link in json.loads(result.stdout))
                    if links[device]:
                        self.command(["chronyc", "online", source["address"]], timeout=5)
                        restored = True
                        break
            return restored
        def retire_obsolete(current):
            obsolete = [source for source in current if source["name"] in owned and source["name"] not in desired]
            if not obsolete:
                return True
            retiring = {source["name"] for source in obsolete}
            if any("nts" in fields for fields in desired.values()):
                alternatives = [source for source in current if source["name"] not in retiring and source["state"] in ("*", "+") and authenticated(source)]
            else:
                alternatives = [source for source in current if source["name"] in desired and source["state"] in ("*", "+", "-") and responding(source) and policy_matches(source, desired[source["name"]])]
            if not alternatives or self.command(["chronyc", "waitsync", "1", "0.1", "0.0", "1"], timeout=5, required=False).returncode:
                return False
            for source in obsolete:
                self.command(["chronyc", "delete", source["address"]], timeout=10)
            return True
        if not self.check:
            if any(source["name"] in owned and source["name"] not in desired for source in snapshot) and retire_obsolete(snapshot):
                snapshot = self.chrony_snapshot()
            changed = False
            for name, fields in desired.items():
                current = [source for source in snapshot if source["name"] == name]
                if not current:
                    self.command(["chronyc", "add", *fields], timeout=10)
                    applied(name, fields)
                    changed = True
                    continue
                maximum = int(fields[fields.index("maxsources") + 1]) if "maxsources" in fields else 1 if fields[0] == "server" else None
                recreate = transport_pending(name, fields) or maximum is not None and sum(responding(source) for source in current) > maximum or any(wrong_family(source, fields) or not source["auth"] or source["auth"][1] != ("NTS" if "nts" in fields else "-") for source in current)
                if recreate:
                    alternatives = [other for other in snapshot if other["name"] != name and other["state"] in ("*", "+", "-") and responding(other) and other["selection"] and other["selection"][0] in ("*", "+", "P", "D", "U")]
                    if not alternatives or self.command(["chronyc", "waitsync", "3", "0.1", "0.0", "1"], timeout=8, required=False).returncode:
                        self.report("chrony", "pending", "no synchronized alternative for seamless managed source replacement")
                        return
                    for source in current:
                        self.command(["chronyc", "delete", source["address"]], timeout=10)
                    self.command(["chronyc", "add", *fields], timeout=10)
                    applied(name, fields)
                    changed = True
                    continue
                for source in current:
                    if not policy_matches(source, fields):
                        options = [("+" if option in fields else "-") + option for option in ("noselect", "prefer", "trust", "require")]
                        self.command(["chronyc", "selectopts", source["address"], *options], timeout=10)
                        applied(name, fields)
                        changed = True
            if changed:
                self.command(["chronyc", "reselect"], timeout=10)
                self.command(["chronyc", "waitsync", "5", "0.1", "0.0", "1"], timeout=10, required=False)
            snapshot = self.chrony_snapshot()
            if restore_online(snapshot):
                snapshot = self.chrony_snapshot()
            if not retire_obsolete(snapshot):
                self.report("chrony", "pending", "obsolete managed source retained until authenticated replacement is synchronized")
                return
            snapshot = self.chrony_snapshot()
        parity = all(any(source["name"] == name and policy_matches(source, fields) for source in snapshot) and not transport_pending(name, fields) and not any(source["name"] == name and wrong_family(source, fields) for source in snapshot) for name, fields in desired.items()) and not any(source["name"] in owned and source["name"] not in desired for source in snapshot)
        for name, fields in desired.items():
            if "maxsources" in fields:
                parity = parity and sum(source["name"] == name and responding(source) for source in snapshot) <= int(fields[fields.index("maxsources") + 1])
        synced = self.command(["chronyc", "waitsync", "1", "0.1", "0.0", "1"], timeout=5, required=False).returncode == 0
        if not parity:
            self.report("chrony", "drift", "live managed NTS/require/prefer/family source policy differs")
        elif not nts_ready(snapshot) or not synced:
            self.report("chrony", "pending", "managed NTS establishment or time synchronization pending; clock step refused")
        else:
            self.clear_chrony_pending(snapshots)
            self.report("chrony")



    def cleanup_seed_paths(self, paths, check, media=False):
        check = check or (media and not self.activate)
        remaining = False
        failed = False
        changed = False
        cloud = self.target("/var/lib/cloud")
        for path in sorted(paths):
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_relative_to(cloud) and not path.parent.resolve().is_relative_to(cloud.resolve()):
                failed = True
                continue
            if not path.is_symlink() and not path.is_file():
                failed = True
                continue
            if check:
                remaining = True
                continue
            try:
                path.unlink()
                changed = True
                if path.exists() or path.is_symlink():
                    failed = True
            except OSError:
                failed = True
        return remaining, failed, changed

    def seed_media_identity(self, device):
        if not device.startswith("/dev/") or any(character.isspace() for character in device):
            raise PolicyError("CIDATA device path unproved; cleanup refused")
        path = self.target(device)
        metadata = path.stat()
        if not stat.S_ISBLK(metadata.st_mode):
            raise PolicyError("CIDATA block device identity unproved; cleanup refused")
        actual = path.resolve(strict=True)
        if not actual.is_relative_to(self.target("/dev").resolve(strict=True)):
            raise PolicyError("CIDATA resolved device path unproved; cleanup refused")
        return "/" + str(actual.relative_to(ROOT.resolve())), (os.major(metadata.st_rdev), os.minor(metadata.st_rdev))

    def seed_media_device(self):
        result = self.command(["blkid", "-c", "/dev/null", "-t", "LABEL=CIDATA", "-o", "device"], timeout=5, required=False)
        if result.returncode == 2 and not result.stdout and not result.stderr:
            return None
        if result.returncode or not result.stdout:
            raise PolicyError("CIDATA media inventory unavailable; cleanup refused")
        identities = {}
        for device in result.stdout.splitlines():
            name, identity = self.seed_media_identity(device)
            identities.setdefault(identity, set()).add(name)
        if len(identities) != 1:
            raise PolicyError("multiple distinct CIDATA devices found; no seed or cache cleanup")
        identity, names = next(iter(identities.items()))
        return min(names), identity

    def seed_media_targets(self, device):
        result = self.command(["findmnt", "-nr", "-S", device, "-o", "TARGET"], timeout=5, required=False)
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return []
        if result.returncode:
            raise PolicyError("seed media mount inventory unavailable; cleanup refused")
        targets = result.stdout.splitlines()
        if not targets or any(not target.startswith("/") or "\\" in target for target in targets):
            raise PolicyError("seed media mount destination unproved; cleanup refused")
        return targets

    def private_seed_media(self, device, identity):
        with tempfile.TemporaryDirectory(prefix="nodes-seed-media-", dir=self.target("/run")) as directory:
            mountpoint = Path(directory) / "media"
            mountpoint.mkdir(mode=0o700)
            arguments = [
                "unshare", "--mount", "--propagation", "private", sys.executable, str(Path(__file__).resolve()),
                "--platform", self.platform, "--role", self.role, "--source-root", str(self.source_root),
                "--seed-media-device", device, "--seed-media-identity", f"{identity[0]}:{identity[1]}", "--seed-media-mountpoint", str(mountpoint),
                "--activate" if self.activate else "--check",
            ]
            if self.bootstrap and self.cleanup_only:
                arguments.extend(("--bootstrap", "--cleanup-seeds"))
            result = self.command(arguments, timeout=180, required=False)
            try:
                records = [json.loads(line) for line in result.stdout.splitlines()]
                records = [record for record in records if isinstance(record, dict) and record.get("item") == "seed-media"]
            except (TypeError, ValueError):
                raise PolicyError("private seed media assessment unavailable; no cleanup proof") from None
            if len(records) != 1 or records[0].get("state") not in ("match", "drift", "unknown", "pending"):
                raise PolicyError("private seed media assessment unavailable; no cleanup proof")
            state = records[0]["state"]
            expected_status = 0 if state == "match" else 2 if state == "pending" else 1
            if result.returncode != expected_status:
                raise PolicyError("private seed media worker did not finish safely; no cleanup proof")
            return state

    def execute_seed_media(self, device, mountpoint, expected_identity):
        attempted = False
        outcome = "unknown"
        reason = "private CIDATA assessment or cleanup failed; no parity claim"
        try:
            self.initialize()
            config = self.target("/etc/nodes/config")
            internal_completion = self.bootstrap and self.cleanup_only
            if (config.exists() or config.is_symlink()) and not internal_completion:
                self.report("seed-media", "pending", "pending bootstrap credentials retained; seed media not mounted")
                return 2
            self.cleanup_role_proof()
            if not self.k3s_ready or internal_completion and not any(record["item"] == "k3s-runtime" and record["state"] == "match" for record in self.records):
                self.report("seed-media", "pending", "existing ready role not proved; seed media not mounted")
                return 2
            if (config.exists() or config.is_symlink()) and not internal_completion:
                self.report("seed-media", "pending", "bootstrap state changed; seed media not mounted")
                return 2
            if os.readlink(self.target("/proc/self/ns/mnt")) == os.readlink(self.target("/proc/1/ns/mnt")):
                raise PolicyError("private mount namespace not proved")
            parent = mountpoint.parent
            if parent.parent != self.target("/run") or not parent.name.startswith("nodes-seed-media-") or mountpoint.name != "media":
                raise PolicyError("private seed media staging path unproved")
            for directory in (parent, mountpoint):
                metadata = directory.stat()
                if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise PolicyError("private seed media staging ownership unproved")
            propagation = self.command(["findmnt", "-nr", "--target", str(parent), "-o", "PROPAGATION"], timeout=5).stdout.strip()
            if propagation != "private":
                raise PolicyError("private mount propagation not proved")
            parts = expected_identity.split(":")
            if len(parts) != 2 or not all(part.isdecimal() for part in parts):
                raise PolicyError("expected CIDATA device identity unavailable")
            expected = tuple(int(part) for part in parts)
            current = self.seed_media_device()
            if current is None or current[1] != expected or self.seed_media_identity(device)[1] != expected:
                raise PolicyError("CIDATA singleton device identity changed; cleanup refused")
            identity = self.command(["blkid", "-c", "/dev/null", "-s", "LABEL", "-s", "TYPE", "-o", "export", device], timeout=5)
            properties = dict(line.split("=", 1) for line in identity.stdout.splitlines() if "=" in line)
            if properties.get("LABEL") != "CIDATA" or properties.get("TYPE") != "vfat" or self.seed_media_targets(device):
                raise PolicyError("unmounted CIDATA filesystem identity unproved")
            writable = self.activate and not self.check
            mode = "rw" if writable else "ro"
            attempted = True
            self.command(["mount", "-t", "vfat", "-o", mode + ",nodev,nosuid,noexec,umask=077", "--", device, str(mountpoint)], timeout=10)
            mounted = self.command(["findmnt", "-nr", "--mountpoint", str(mountpoint), "-o", "FSTYPE,OPTIONS"], timeout=5).stdout.split()
            options = set(mounted[1].split(",")) if len(mounted) == 2 else set()
            readonly = bool(os.statvfs(mountpoint).f_flag & os.ST_RDONLY)
            if len(mounted) != 2 or mounted[0] != "vfat" or not {mode, "nodev", "nosuid", "noexec"}.issubset(options) or readonly == writable:
                raise PolicyError("private seed media mount safety not proved")
            remaining, failed, changed = self.cleanup_seed_paths({mountpoint / name for name in SEED_MEDIA_NAMES}, self.check, media=True)
            if failed:
                raise PolicyError("owned private seed media removal not proved")
            if changed:
                self.command(["sync", "-f", str(mountpoint)], timeout=15)
            outcome = "drift" if remaining else "match"
            reason = "completed CIDATA seed material remains" if remaining else None
        except PolicyError as error:
            reason = str(error)
        except Exception:
            reason = "private CIDATA assessment or cleanup unavailable; no parity claim"
        finally:
            if attempted:
                try:
                    result = self.command(["umount", "--", str(mountpoint)], timeout=10, required=False)
                    if result.returncode:
                        outcome, reason = "unknown", "private CIDATA unmount failed; no cleanup proof"
                except Exception:
                    outcome, reason = "unknown", "private CIDATA unmount unavailable; no cleanup proof"
        self.report("seed-media", outcome, reason)
        return 0 if outcome == "match" else 1

    def cleanup_role_proof(self):
        check, activate = self.check, self.activate
        self.check, self.activate = True, False
        try:
            self.k3s()
        finally:
            self.check, self.activate = check, activate

    def cleanup_seeds(self):
        config = self.target("/etc/nodes/config")
        internal_completion = self.bootstrap and self.cleanup_only
        config_present = config.exists() or config.is_symlink()
        if config_present and not internal_completion:
            self.report("bootstrap-seeds", "pending", "pending bootstrap credentials retained; successful canonical setup cleanup required")
            return
        if not self.k3s_ready or internal_completion and not any(record["item"] == "k3s-runtime" and record["state"] == "match" for record in self.records):
            self.report("bootstrap-seeds", "pending", "existing ready role not proved; bootstrap seed material retained")
            return
        paths = {self.target(base) / name for base in ("/boot/firmware", "/boot") for name in SEED_MEDIA_NAMES}
        paths.update(
            self.target(path) for path in (
                "/var/lib/cloud/instance/user-data.txt",
                "/var/lib/cloud/instance/user-data.txt.i",
                "/var/lib/cloud/instance/obj.pkl",
                "/var/lib/cloud/instance/cloud-config.txt",
                "/var/lib/cloud/instance/network-config.json",
            )
        )
        paths.update(self.target("/var/lib/cloud/seed").glob("nocloud*/user-data"))
        media_remaining = False
        media_pending = False
        media_paths = set()
        media = self.seed_media_device()
        if media is not None:
            device, identity = media
            targets = self.seed_media_targets(device)
            if not targets:
                state = self.private_seed_media(device, identity)
                if state in ("unknown", "pending"):
                    self.report("bootstrap-seeds", state, "private CIDATA assessment or cleanup not proved; bootstrap credentials retained")
                    return
                media_remaining = state == "drift"
                media_pending = media_remaining and not self.check
            for target in targets:
                media_paths.update(self.target(target) / name for name in SEED_MEDIA_NAMES)
        mounted_remaining, mounted_failed, mounted_changed = self.cleanup_seed_paths(media_paths, self.check, media=True)
        media_remaining = media_remaining or mounted_remaining
        media_pending = media_pending or (mounted_remaining and not self.check)
        remaining, failed, changed = self.cleanup_seed_paths(paths, self.check)
        failed = failed or mounted_failed
        changed = changed or mounted_changed
        if changed:
            self.command(["sync"], timeout=15)
        if failed:
            self.report("bootstrap-seeds", "unknown", "owned seed/cache removal not proved; bootstrap credentials retained")
        elif media_pending:
            self.report("bootstrap-seeds", "pending", "CIDATA seed material remains; explicit --activate required for media cleanup")
        elif self.check and (remaining or media_remaining or internal_completion and config_present):
            self.report("bootstrap-seeds", "drift", "completed bootstrap seed/cache material remains")
        else:
            if internal_completion and config_present:
                if not config.is_symlink() and not config.is_file():
                    raise PolicyError("bootstrap credential destination is not a file; retained")
                config.unlink()
                if config.exists() or config.is_symlink():
                    raise PolicyError("bootstrap credential removal not proved")
                self.command(["sync"], timeout=15)
            self.report("bootstrap-seeds")

    def execute(self):
        try:
            self.initialize()
        except PolicyError as error:
            self.report("identity-policy", "unknown", str(error))
            return 1
        except Exception:
            self.report("identity-policy", "unknown", "platform, role, Ubuntu installation or canonical policy unavailable/unsupported")
            return 1
        stages = (
            ("root-lock-expiry", self.root_account),
            ("managed-files", self.assets), ("apt", self.apt), ("packages", self.packages),
            ("nvidia", self.nvidia),
            ("accounts", self.accounts), ("timezone", self.timezone), ("swap", self.swap),
            ("sysctls", self.sysctls), ("systemd", self.systemd), ("oom", self.oom),
            ("tailscale", self.tailscale), ("multipath", self.multipath),
            ("watchdog", self.watchdog), ("logging", self.logging),
            ("ha", self.ha), ("network", self.network), ("k3s", self.k3s),
            ("chrony", self.chrony),
        )
        if self.cleanup_only:
            stages = (("k3s", self.cleanup_role_proof), ("bootstrap-seeds", self.cleanup_seeds))
        elif not self.bootstrap:
            stages += (("bootstrap-seeds", self.cleanup_seeds),)
        for item, function in stages:
            try:
                function()
            except PolicyError as error:
                self.report(item, "unknown", str(error))
            except Exception:
                self.report(item, "unknown", "effective policy input unavailable or unsupported; no parity claim")
        states = {record["state"] for record in self.records}
        return 1 if states & {"unknown", "drift"} else 2 if "pending" in states else 0


def main(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=("physical", "cloud"))
    parser.add_argument("--role", choices=("server", "agent"))
    activation = parser.add_mutually_exclusive_group()
    activation.add_argument("--check", action="store_true")
    activation.add_argument("--activate", action="store_true")
    parser.add_argument("--source-root", type=Path, default=Path("/"))
    parser.add_argument("--bootstrap", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cleanup-seeds", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seed-media-device", help=argparse.SUPPRESS)
    parser.add_argument("--seed-media-identity", help=argparse.SUPPRESS)
    parser.add_argument("--seed-media-mountpoint", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)
    media_options = (options.seed_media_device, options.seed_media_identity, options.seed_media_mountpoint)
    if any(media_options) and not all(media_options):
        parser.error("private seed media device, identity and staging path must be provided together")
    if os.geteuid() != 0:
        print(json.dumps({"item": "privilege", "state": "unknown", "reason": "root is required"}))
        return 1
    reconciler = Reconciler(options.platform, options.role, options.check, options.source_root, options.activate, options.bootstrap, options.cleanup_seeds)
    status = reconciler.execute_seed_media(options.seed_media_device, options.seed_media_mountpoint, options.seed_media_identity) if options.seed_media_device else reconciler.execute()
    for record in reconciler.records:
        print(json.dumps(record, sort_keys=True))
    scope = "seed-media" if options.seed_media_device else "seed-cleanup" if options.cleanup_seeds else "bootstrap-common" if options.bootstrap else "full-node"
    print(json.dumps({"status": "match" if status == 0 else "pending" if status == 2 else "drift-or-unknown", "scope": scope}, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
