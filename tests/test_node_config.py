import contextlib
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("node_configure", PROJECT / "rootfs/common/usr/local/bin/node-configure.py")
NODE = importlib.util.module_from_spec(SPEC)
exec(compile(Path(SPEC.origin).read_text(), SPEC.origin, "exec"), NODE.__dict__)


def netplan_permission_tool(directory):
    program = directory / "netplan"
    program.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1] == 'generate':\n"
        "    root = Path(sys.argv[sys.argv.index('--root-dir') + 1])\n"
        "elif sys.argv[1:] == ['apply']:\n"
        "    root = Path(os.environ['NODES_NETPLAN_PERMISSION_ROOT'])\n"
        "else:\n"
        "    raise SystemExit(4)\n"
        "for name, mode in [('run/systemd/network/reader-probe.network', 0o640), ('run/netplan/private-probe.conf', 0o600)]:\n"
        "    path = root / name\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)\n"
        "    os.write(fd, b'[Match]\\nName=eth0\\n')\n"
        "    os.close(fd)\n"
    )
    program.chmod(0o755)


class NetplanPermissionTest(unittest.TestCase):
    def test_private_candidate_generation_keeps_backend_group_readable_and_credentials_private(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            tools = temporary / "tools"
            tools.mkdir(mode=0o700)
            netplan_permission_tool(tools)
            target = temporary / "node"
            producer = target / "etc/netplan/50-cloud-init.yaml"
            producer.parent.mkdir(parents=True)
            content = "network:\n  version: 2\n  wifis:\n    wlan0:\n      dhcp4: true\n      access-points:\n        PRIVATE_SSID:\n          password: PRIVATE_PSK\n"
            producer.write_text(content)
            producer.chmod(0o600)
            candidate = temporary / "candidate"
            candidate.mkdir(mode=0o700)
            saved_mask = os.umask(0o077)
            try:
                with mock.patch.object(NODE, "ROOT", target), mock.patch.dict(os.environ, {"PATH": str(tools)}):
                    NODE.Reconciler("physical").network_candidate(candidate, content, producer)
                caller_mask = os.umask(0o077)
            finally:
                os.umask(saved_mask)
            self.assertEqual(caller_mask, 0o077)
            self.assertEqual(stat.S_IMODE((candidate / "run/systemd/network/reader-probe.network").stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE((candidate / "run/netplan/private-probe.conf").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((candidate / "etc/netplan/50-cloud-init.yaml").stat().st_mode), 0o600)
            self.assertEqual(producer.read_text(), content)
            self.assertEqual(stat.S_IMODE(producer.stat().st_mode), 0o600)


class NetplanPtyTest(unittest.TestCase):
    def child_result(self, accept):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            result_path = temporary / "result"
            program = temporary / "netplan"
            program.write_text(
                f"#!{sys.executable}\n"
                "import os, select, signal, sys\n"
                "def result(value):\n"
                "    with open(os.environ['NODES_PTY_RESULT'], 'w') as handle:\n"
                "        handle.write(value)\n"
                "def cancelled(number, frame):\n"
                "    result('cancelled')\n"
                "    raise SystemExit(2)\n"
                "signal.signal(signal.SIGINT, cancelled)\n"
                "if sys.argv[1:] != ['try', '--timeout', '60']:\n"
                "    result('wrong arguments')\n"
                "    raise SystemExit(5)\n"
                "for name, mode in [('backend.network', 0o640), ('private-wpa.conf', 0o600)]:\n"
                "    fd = os.open(os.path.join(os.path.dirname(os.environ['NODES_PTY_RESULT']), name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)\n"
                "    os.close(fd)\n"
                "print('Do you want to keep these settings? Press ENTER to accept the new configuration', flush=True)\n"
                "if not select.select([0], [], [], 1.5)[0]:\n"
                "    result('timeout')\n"
                "    raise SystemExit(3)\n"
                "data = os.read(0, 64)\n"
                "if data != bytes([10]):\n"
                "    result('not Enter')\n"
                "    raise SystemExit(4)\n"
                "result('accepted')\n"
            )
            program.chmod(0o755)
            verify = mock.Mock(return_value=accept)
            saved_mask = os.umask(0o077)
            try:
                with mock.patch.dict(os.environ, {"PATH": str(temporary), "NODES_PTY_RESULT": str(result_path)}):
                    completed = NODE.run_netplan_try(verify)
                caller_mask = os.umask(0o077)
            finally:
                os.umask(saved_mask)
            verify.assert_called_once_with()
            modes = {name: stat.S_IMODE((temporary / name).stat().st_mode) for name in ("backend.network", "private-wpa.conf")}
            modes["caller"] = caller_mask
            return completed.returncode, result_path.read_text(), modes

    def test_production_pty_sends_real_enter_after_successful_verification(self):
        status, result, _ = self.child_result(True)
        self.assertEqual(status, 0)
        self.assertEqual(result, "accepted")

    def test_production_pty_sends_real_interrupt_after_rejected_verification(self):
        status, result, _ = self.child_result(False)
        self.assertNotEqual(status, 0)
        self.assertEqual(result, "cancelled")

    def test_native_try_keeps_backend_group_readable_without_relaxing_caller_or_secrets(self):
        status, result, modes = self.child_result(True)
        self.assertEqual((status, result), (0, "accepted"))
        self.assertEqual(modes, {"backend.network": 0o640, "private-wpa.conf": 0o600, "caller": 0o077})


class Machine:
    def __init__(self, directory, platform="cloud", role="server", hostname="node-a"):
        self.root = Path(directory) / "target"
        self.source = Path(directory) / "desired"
        self.platform = platform
        self.hostname = hostname
        self.clock = 0
        self.network_online = True
        self.network_try_result = "success"
        self.netplan_modern = False
        self.wait_conditions = []
        self.default_routes_override = None
        self.ready = True
        self.extra_link_names = []
        self.swap_inventory = NODE.Result(1)
        self.network_file = "etc/netplan/50-cloud-init.yaml"
        self.node_addresses = [{"type": "InternalIP", "address": "100.64.0.1"}, {"type": "ExternalIP", "address": "100.64.0.1"}]
        self.cidata = False
        self.cidata_target = None
        self.media_store = self.root / "fixture-cidata-content"
        self.media_mountpoint = None
        self.media_readonly = True
        self.media_candidates = ["/dev/fixture-cidata"]
        self.media_device_numbers = {"/dev/fixture-cidata": os.makedev(8, 2)}
        self.role = role
        shutil.copytree(PROJECT / "rootfs/common", self.source)
        if platform == "physical":
            shutil.copytree(PROJECT / "rootfs/physical", self.source, dirs_exist_ok=True)
        shutil.copytree(self.source, self.root)
        (self.root / "run").mkdir(mode=0o755, exist_ok=True)
        self.media_store.mkdir(mode=0o700)
        self.write("/dev/fixture-cidata", "", 0o600)
        self.policy = json.loads((self.source / NODE.POLICY_PATH.lstrip("/")).read_text())
        self.calls = []
        self.fail = None
        self.added_sources_ready = True
        self.pool_address = None
        self.chrony_offline = set()
        self.chrony_routes = {}
        self.chrony_links = {}
        self.timezone_name = self.policy["timezone"]
        self.prefs = {"RunSSH": True, "Hostname": hostname}
        self.installed = set(self.policy["packages"]["common"] + (self.policy["packages"]["physical"] if platform == "physical" else []))
        self.sysctls = NODE.parse_assignments((self.source / "etc/sysctl.d/99-tailscale.conf").read_text())
        self.write("/etc/os-release", 'ID=ubuntu\nVERSION_ID="24.04"\n')
        self.write("/etc/passwd", "root:x:0:0:root:/root:/bin/bash\npi:x:1000:1000:Pi:/home/pi:/bin/bash\nother:x:1001:1001:Other:/home/other:/bin/sh\n")
        self.write("/etc/group", "root:x:0:\npi:x:1000:\nadm:x:4:pi\nsudo:x:27:pi\nother:x:1001:\n")
        self.write("/etc/shadow", "root:!:20000:0:99999:7:::\npi:$6$PRIVATE_PASSWORD:20000:0:99999:7:::\nother:OTHER_PRIVATE:20000:0:99999:7:::\n", 0o600)
        self.write("/etc/sudoers.d/90-nodes-pi", "pi ALL=(ALL) NOPASSWD:ALL\n", 0o440)
        self.write("/etc/apt/apt.conf.d/94cloud-init-config", "".join(f'{key} "{value}";\n' for key, value in self.policy["apt"]["conf"].items()))
        self.write("/usr/share/zoneinfo/Europe/Lisbon", "lisbon-zone")
        self.write("/usr/share/zoneinfo/UTC", "utc-zone")
        self.write("/etc/localtime", "lisbon-zone")
        self.write("/etc/fstab", "UUID=keep / ext4 defaults 0 1\n")
        self.write("/proc/swaps", "Filename Type Size Used Priority\n")
        self.write("/proc/meminfo", "MemTotal: 2000000 kB\nMemAvailable: 1500000 kB\n")
        self.write("/proc/stat", "btime 1000\n")
        self.write("/proc/uptime", "1000.00 0.00\n")
        for key, value in self.sysctls.items():
            self.write("/proc/sys/" + key.replace(".", "/"), value + "\n")
        self.write("/proc/sys/net/ipv4/ip_forward", "0\n")
        (self.root / "sys/block").mkdir(parents=True)
        self.write("/dev/watchdog0", "watchdog-device")
        (self.root / "proc/1/fd").mkdir(parents=True)
        (self.root / "proc/1/fd/0").symlink_to("../../../dev/watchdog0")
        self.write("/var/lib/node-health/wifi-recovery/reboot-budget", "PRESERVED_BUDGET\n", 0o600)
        self.write("/home/pi/.ssh/authorized_keys", "FIRST_KEY\nSECOND_KEY\n", 0o600)
        self.write("/etc/systemd/system/k3s.service.env", "K3S_TOKEN='PRIVATE_K3S_TOKEN'\n", 0o600)
        self.write("/var/lib/rancher/k3s/server/node-token", "PRIVATE_BOOTSTRAP_TOKEN\n", 0o600)
        self.write("/etc/chrony/chrony.conf", "confdir /etc/chrony/conf.d\n")
        self.units = {
            "node-health.timer": {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active", "Unit": "node-health.service", "TimersMonotonic": "{ OnBootUSec=1min ; next_elapse=0 } { OnUnitInactiveUSec=15s ; next_elapse=0 }", "AccuracyUSec": "1s", "RandomizedDelayUSec": "5s"},
            "node-health.service": {"LoadState": "loaded", "UnitFileState": "static", "ActiveState": "inactive", "Type": "oneshot", "TimeoutStartUSec": "1min", "ExecStart": self.exec_property(["/usr/local/bin/node-health-check.sh"])},
            "watchdog-load.service": {"LoadState": "loaded", "UnitFileState": "enabled", "DefaultDependencies": "no", "After": "systemd-modules-load.service", "Before": "sysinit.target", "Type": "oneshot", "RemainAfterExit": "yes", "ExecStart": self.exec_property(["/usr/local/bin/watchdog-load.sh"])},
            "tailscaled.service": {"LoadState": "loaded", "UnitFileState": "enabled", "Wants": "chrony-wait.service", "After": "chrony-wait.service", "ExecStartPre": self.exec_property(["/usr/bin/chronyc", "-h", "127.0.0.1,::1", "waitsync", "1", "0.1", "0.0", "1"]), "OOMScoreAdjust": "-1000", "MainPID": "20", "ControlGroup": "/system.slice/tailscaled.service", "ActiveState": "active"},
            "ssh.service": {"LoadState": "loaded", "OOMScoreAdjust": "-1000", "MainPID": "21", "ControlGroup": "/system.slice/ssh.service", "ActiveState": "active"},
            "multipathd.service": {"LoadState": "masked", "UnitFileState": "masked", "ActiveState": "inactive"},
            "multipathd.socket": {"LoadState": "masked", "UnitFileState": "masked", "ActiveState": "inactive"},
            "systemd-journald.service": {"LoadState": "loaded", "MainPID": "23", "ActiveState": "active"},
            "rsyslog.service": {"LoadState": "not-found"},
        }
        self.manager = {"RuntimeWatchdogUSec": "15s", "RebootWatchdogUSec": "3min", "KExecWatchdogUSec": "3min"}
        arguments = ["/usr/local/bin/k3s", role, "--node-ip", "100.64.0.1", "--node-external-ip", "100.64.0.1", "--flannel-iface", "tailscale0", "--resolv-conf", "/etc/k3s-resolv.conf", "--kubelet-arg", "allowed-unsafe-sysctls=net.ipv4.ip_forward", "--token", "PRIVATE_K3S_TOKEN"]
        if role == "server":
            arguments += ["--secrets-encryption", "--write-kubeconfig-mode", "0644", "--disable", "servicelb", "--tls-san", "100.64.0.1"]
            if platform == "physical":
                arguments += ["--tls-san", hostname + ".local"]
            self.write(NODE.HA_PATH, (self.source / NODE.HA_TEMPLATE.lstrip("/")).read_text())
        self.arguments = arguments
        self.k3s_unit = "k3s.service" if role == "server" else "k3s-agent.service"
        opposite = "k3s-agent.service" if role == "server" else "k3s.service"
        self.units[opposite] = {"LoadState": "not-found", "FragmentPath": ""}
        self.units[self.k3s_unit] = {"LoadState": "loaded", "FragmentPath": "/etc/systemd/system/" + self.k3s_unit, "ControlGroup": "/system.slice/" + self.k3s_unit, "After": "tailscaled.service", "PartOf": "tailscaled.service", "ActiveState": "active", "SubState": "running", "MainPID": "22", "ExecStart": self.exec_property(arguments), "Environment": "", "EnvironmentFiles": "/etc/systemd/system/k3s.service.env (ignore_errors=yes)", "DropInPaths": ""}
        self.write("/etc/systemd/system/" + self.k3s_unit, "[Service]\nType=notify\n")
        for pid, name, unit in ((20, "tailscaled", "tailscaled.service"), (21, "sshd", "ssh.service"), (22, "k3s", self.k3s_unit), (23, "systemd-journald", "systemd-journald.service")):
            self.write(f"/proc/{pid}/stat", f"{pid} ({name}) S " + "0 " * 18 + "100000 0\n")
            self.write(f"/proc/{pid}/cgroup", "0::/system.slice/" + unit + "\n")
            self.write(f"/proc/{pid}/oom_score_adj", "-1000\n")
            (self.root / f"proc/{pid}/exe").symlink_to("/usr/sbin/" + name)
        self.write("/proc/22/cmdline", "\0".join(arguments) + "\0")
        self.write("/proc/22/environ", "K3S_TOKEN=PRIVATE_K3S_TOKEN\0")
        self.sources = {
            "162.159.200.1": self.source_record("162.159.200.1", require=True, state="+"),
            "203.0.113.10": self.source_record("time.cloudflare.com", nts=True, prefer=True, state="*"),
            "91.189.91.157": self.source_record("ntp.ubuntu.com", state="-"),
        }
        self.wifi_power = False
        self.lan_address = "192.168.68.64" if hostname == "fraguinha" else "192.168.68.68" if hostname == "carolion" else "192.0.2.1"
        self.certificate_sans = ["100.64.0.1", hostname + ".local", self.lan_address]
        if platform == "physical":
            self.write("/etc/cloud/cloud-init.disabled", "")
            netplan = {"network": NODE.network_policy(self.policy, hostname)}
            netplan["network"]["wifis"]["wlan0"]["access-points"] = {"HISTORICAL_SSID": {"auth": {"key-management": "psk", "password": "PRIVATE_WIFI_PSK"}}}
            self.write("/" + self.network_file, yaml.safe_dump(netplan), 0o600)
            self.live_network = json.loads(json.dumps(netplan["network"]))
            self.apply_network_backend(self.live_network)
            (self.root / "sys/class/net/wlan0").mkdir(parents=True)
        for path in self.root.rglob("*"):
            if not path.is_symlink():
                os.utime(path, (100, 100))

    def advance_clock(self):
        self.clock += 1
        return self.clock

    def apply_network_backend(self, network):
        self.live_network = json.loads(json.dumps(network))
        for path in (self.root / "run/systemd/network").glob("*netplan*.network"):
            path.unlink()
        for kind in ("ethernets", "wifis"):
            for identifier, link in network[kind].items():
                selector = link.get("match", {}).get("name", identifier)
                metric = link.get("dhcp4-overrides", {}).get("route-metric", 600)
                content = "[Match]\nName=" + selector + "\n"
                if not self.netplan_modern:
                    content += "[Link]\nRequiredForOnline=" + ("no" if link.get("optional") else "yes") + "\n"
                content += "[Network]\n"
                if link.get("dhcp4"):
                    content += "DHCP=ipv4\n[DHCPv4]\nRouteMetric=" + str(metric) + "\n"
                self.write("/run/systemd/network/10-netplan-" + identifier + ".network", content)
        if self.netplan_modern:
            unit = "systemd-networkd-wait-online.service"
            condition_path = "/run/systemd/generator/network-online.target.wants/" + unit
            dropin = "/run/systemd/generator.late/" + unit + ".d/10-netplan.conf"
            self.write(dropin, "[Unit]\nConditionPathIsSymbolicLink=" + condition_path + "\n")
            self.units[unit] = {"LoadState": "loaded", "ActiveState": "inactive", "DropInPaths": dropin, "NeedDaemonReload": "no", "ConditionResult": "no"}
            self.wait_conditions = [
                ["ConditionPathIsSymbolicLink", False, False, condition_path, 0],
                ["ConditionCapability", False, False, "CAP_NET_ADMIN", 0],
            ]

    def try_network(self, verify):
        self.calls.append((["native-netplan-try"], 60, None))
        candidate = yaml.safe_load((self.root / self.network_file).read_text())["network"]
        self.apply_network_backend(candidate)
        self.network_online = self.network_try_result == "success"
        accepted = verify()
        return NODE.Result(0 if accepted else 1)

    @staticmethod
    def exec_property(arguments):
        return "{ path=" + arguments[0] + " ; argv[]=" + shlex.join(arguments) + " ; ignore_errors=no ; }"

    @staticmethod
    def source_record(name, nts=False, prefer=False, require=False, state="*"):
        return {"name": name, "nts": nts, "prefer": prefer, "require": require, "noselect": False, "trust": False, "state": state, "reach": "377", "cookies": "8" if nts else "0"}

    def write(self, path, content, mode=0o644):
        destination = self.root / path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        destination.chmod(mode)

    def command(self, arguments, timeout=20, env=None, umask=-1):
        args = list(arguments)
        self.calls.append((args, timeout, env))
        if self.fail and self.fail(args):
            return NODE.Result(1, "PRIVATE_FAILURE_OUTPUT PRIVATE_K3S_TOKEN PRIVATE_WIFI_PSK")
        if args[:2] == ["systemctl", "show"]:
            name = args[2] if len(args) > 2 and not args[2].startswith("--") else None
            values = self.manager if name is None else self.units.get(name, {"LoadState": "not-found"})
            names = [value.split("=", 1)[1] for value in args if value.startswith("--property=")]
            lines = []
            for key in names:
                value = values.get(key, "no" if key == "NeedDaemonReload" else "")
                lines.extend(key + "=" + entry for entry in (value if isinstance(value, list) else [value]))
            return NODE.Result(0, "\n".join(lines) + "\n")
        if args[:2] == ["systemctl", "daemon-reload"]:
            for unit in self.units.values():
                unit["NeedDaemonReload"] = "no"
            return NODE.Result()
        if args[:2] == ["systemctl", "daemon-reexec"]:
            self.manager.update(RuntimeWatchdogUSec="15s", RebootWatchdogUSec="3min", KExecWatchdogUSec="3min")
            return NODE.Result()
        if args[:2] == ["systemctl", "stop"]:
            for name in args[2:]:
                if name in self.units:
                    self.units[name]["ActiveState"] = "inactive"
            return NODE.Result()
        if args[:2] == ["systemctl", "restart"]:
            if args[-1] != self.k3s_unit:
                raise AssertionError("Unexpected service restart")
            newest = max(path.stat().st_mtime for path in self.root.rglob("*") if path.is_file())
            ticks = int((newest + 5 - 1000) * 100)
            self.write("/proc/22/stat", "22 (k3s) S " + "0 " * 18 + str(ticks) + " 0\n")
            self.certificate_sans = ["100.64.0.1", self.hostname + ".local", self.lan_address]
            return NODE.Result()
        if args[0] == "systemd-run":
            name = next(value.split("=", 1)[1] for value in args if value.startswith("--unit="))
            self.units[name + ".timer"] = {"LoadState": "loaded", "ActiveState": "active"}
            return NODE.Result()
        if args[:2] == ["netplan", "generate"]:
            candidates = list((Path(args[-1]) / "etc/netplan").glob("*.yaml"))
            documents = [yaml.safe_load(candidate.read_text()) for candidate in candidates]
            if not any(document.get("network", {}).get("version") == 2 for document in documents):
                return NODE.Result(1)
            return NODE.Result()
        if args[:2] == ["netplan", "apply"]:
            if self.network_try_result == "recovery-failure":
                return NODE.Result(1)
            self.apply_network_backend(yaml.safe_load((self.root / self.network_file).read_text())["network"])
            self.network_online = True
            return NODE.Result()
        if args[:2] in (["systemctl", "enable"], ["systemctl", "disable"], ["systemctl", "start"], ["systemctl", "mask"]):
            action, name = args[1], args[-1]
            if action == "enable":
                self.units[name]["UnitFileState"] = "enabled"
            elif action == "disable":
                self.units[name]["UnitFileState"] = "static"
            elif action == "start":
                self.units[name]["ActiveState"] = "active"
            else:
                self.units[name].update(UnitFileState="masked", LoadState="masked", ActiveState="inactive")
            return NODE.Result()
        if args[:2] == ["systemctl", "list-unit-files"]:
            return self.swap_inventory
        if args[:2] == ["apt-config", "dump"]:
            return NODE.Result(0, (self.root / NODE.APT_PATH.lstrip("/")).read_text())
        if args[0] == "dpkg-query":
            return NODE.Result(0, "".join(package + "\tinstalled\n" for package in sorted(self.installed)))
        if args[0] == "apt-get":
            if "install" in args:
                root = (self.root / "etc/shadow").read_text().splitlines()[0].split(":")
                if root[1] != "!" or root[2] in ("", "0"):
                    return NODE.Result(1, "PAM package-account creation refused")
                self.installed.update(args[args.index("--no-remove") + 1:])
            return NODE.Result()
        if args[:2] == ["usermod", "--password"]:
            shadow = self.root / "etc/shadow"
            entries = shadow.read_text().splitlines()
            entries[0] = "root:!:" + ":".join(entries[0].split(":")[2:])
            shadow.write_text("\n".join(entries) + "\n")
            return NODE.Result()
        if args[:2] == ["chage", "--lastday"]:
            shadow = self.root / "etc/shadow"
            entries = shadow.read_text().splitlines()
            fields = entries[0].split(":")
            fields[2] = "21000"
            entries[0] = ":".join(fields)
            shadow.write_text("\n".join(entries) + "\n")
            return NODE.Result()
        if args[:2] == ["usermod", "--shell"]:
            passwd = self.root / "etc/passwd"
            entries = passwd.read_text().splitlines()
            fields = entries[1].split(":")
            fields[6] = args[2]
            entries[1] = ":".join(fields)
            passwd.write_text("\n".join(entries) + "\n")
            return NODE.Result()
        if args[:3] == ["usermod", "--append", "--groups"]:
            group_file = self.root / "etc/group"
            entries = []
            for line in group_file.read_text().splitlines():
                fields = line.split(":")
                if fields[0] in args[3].split(","):
                    fields[3] += ("," if fields[3] else "") + "pi"
                entries.append(":".join(fields))
            group_file.write_text("\n".join(entries) + "\n")
            return NODE.Result()
        if args[0] == "blkid" and "-t" in args:
            return NODE.Result(0, "\n".join(self.media_candidates) + "\n") if self.cidata else NODE.Result(2)
        if args[0] == "blkid" and "-s" in args:
            return NODE.Result(0, "LABEL=CIDATA\nTYPE=vfat\n")
        if args[0] == "unshare":
            device = args[args.index("--seed-media-device") + 1]
            mountpoint = Path(args[args.index("--seed-media-mountpoint") + 1])
            helper = NODE.Reconciler(
                self.platform, self.role, "--check" in args, self.source,
                activate="--activate" in args, bootstrap="--bootstrap" in args, cleanup_only="--cleanup-seeds" in args,
            )
            with self.private_media_namespace():
                status = helper.execute_seed_media(device, mountpoint, args[args.index("--seed-media-identity") + 1])
            output = "\n".join(json.dumps(record) for record in helper.records)
            return NODE.Result(status, output)
        if args[0] == "findmnt":
            if args[-1] == "PROPAGATION":
                return NODE.Result(0, "private\n")
            if args[-1] == "FSTYPE,OPTIONS":
                return NODE.Result(0, "vfat " + ("ro" if self.media_readonly else "rw") + ",nodev,nosuid,noexec,umask=0077\n")
            return NODE.Result(0, self.cidata_target + "\n") if self.cidata_target else NODE.Result(1)
        if args[0] == "mount":
            self.media_mountpoint = Path(args[-1])
            self.media_readonly = args[args.index("-o") + 1].split(",")[0] == "ro"
            self.media_mountpoint.rmdir()
            self.media_mountpoint.symlink_to(self.media_store, target_is_directory=True)
            return NODE.Result()
        if args[0] == "umount":
            if self.media_mountpoint is None:
                return NODE.Result(1)
            self.media_mountpoint.unlink()
            self.media_mountpoint.mkdir(mode=0o700)
            self.media_mountpoint = None
            return NODE.Result()
        if args[0] == "sync":
            return NODE.Result()
        if args[0] == "visudo":
            self.assert_sudo_candidate(Path(args[-1]).read_text())
            return NODE.Result()
        if args[:4] == ["sudo", "-n", "-l", "-U"]:
            present = self.root / "etc/sudoers.d/90-nodes-pi"
            return NODE.Result(0, "User pi may run the following commands:\n    (ALL) NOPASSWD: ALL\n" if present.exists() else "    (ALL) ALL\n")
        if args[:2] == ["timedatectl", "show"]:
            return NODE.Result(0, self.timezone_name + "\n")
        if args[:2] == ["timedatectl", "set-timezone"]:
            self.timezone_name = args[2]
            self.write("/etc/localtime", (self.root / "usr/share/zoneinfo" / args[2]).read_text())
            return NODE.Result()
        if args[:2] == ["sysctl", "-w"]:
            key, value = args[2].split("=", 1)
            self.write("/proc/sys/" + key.replace(".", "/"), value + "\n")
            return NODE.Result()
        if args[:2] == ["tailscale", "status"]:
            return NODE.Result(0, json.dumps({"BackendState": "Running" if self.network_online else "Stopped", "Peer": {"peer": {"Online": True, "TailscaleIPs": ["100.64.0.2"]}}}))
        if args[:3] == ["tailscale", "debug", "prefs"]:
            return NODE.Result(0, json.dumps(self.prefs))
        if args[:2] == ["tailscale", "set"]:
            self.prefs.update(Hostname=args[-1].split("=", 1)[1])
            return NODE.Result()
        if args[:3] == ["tailscale", "ip", "-4"]:
            return NODE.Result(0, "100.64.0.1\n")
        if args[:2] == ["tailscale", "ping"]:
            return NODE.Result(0 if self.network_online else 1)
        if args[:3] == ["systemd-analyze", "cat-config", "systemd/journald.conf"]:
            return NODE.Result(0, (self.root / "etc/systemd/journald.conf.d/10-limits.conf").read_text())
        if args[:3] == ["systemd-analyze", "cat-config", "systemd/system.conf"]:
            return NODE.Result(0, (self.root / "etc/systemd/system.conf.d/10-watchdog.conf").read_text())
        if args[:2] == ["k3s", "kubectl"]:
            return NODE.Result(0, json.dumps({"metadata": {"name": self.hostname}, "status": {"conditions": [{"type": "Ready", "status": "True" if self.ready else "False"}], "addresses": self.node_addresses}}))
        if args[:2] == ["chronyd", "-p"]:
            return NODE.Result(0, (self.root / NODE.CHRONY_PATH.lstrip("/")).read_text() + "pool ntp.ubuntu.com iburst\n")
        if args[0] == "chronyc":
            return self.chrony_command([value for value in args[1:] if value not in ("-n", "-c")])
        if args[0] == "/usr/local/bin/watchdog-load.sh":
            return NODE.Result(1)
        if args[0] == "busctl":
            if "get-property" in args and args[-1] == "Conditions":
                return NODE.Result(0, json.dumps({"type": "a(sbbsi)", "data": self.wait_conditions}))
            return NODE.Result(1)
        if args[:2] == ["openssl", "x509"]:
            return NODE.Result(0, "X509v3 Subject Alternative Name:\n" + ", ".join(("IP Address:" if value[0].isdigit() else "DNS:") + value for value in self.certificate_sans) + "\n")
        if args[:4] == ["ip", "-4", "route", "get"]:
            return NODE.Result(0, "1.1.1.1 via 192.0.2.254 dev eth0 src " + self.lan_address + "\n")
        if args[:4] == ["ip", "-j", "route", "get"]:
            route = self.chrony_routes.get(args[4])
            return NODE.Result(2) if route is None else NODE.Result(0, json.dumps([route]))
        if args[:5] == ["ip", "-j", "link", "show", "dev"]:
            name = args[5]
            if name not in self.chrony_links:
                return NODE.Result(1)
            up = self.chrony_links[name]
            return NODE.Result(0, json.dumps([{"ifname": name, "flags": ["UP", "LOWER_UP"] if up else [], "operstate": "UP" if up else "DOWN"}]))
        if args[:4] == ["ip", "-j", "link", "show"]:
            names = {identifier if "match" not in link else "eth0" for kind in ("ethernets", "wifis") for identifier, link in self.live_network[kind].items()}
            names.update(self.extra_link_names)
            return NODE.Result(0, json.dumps([{"ifname": name} for name in sorted(names)]))
        if args[:4] == ["ip", "-j", "-4", "address"]:
            interfaces = []
            for kind in ("ethernets", "wifis"):
                for identifier, link in self.live_network[kind].items():
                    name = "eth0" if "match" in link else identifier
                    addresses = [{"family": "inet", "local": value.split("/")[0], "prefixlen": int(value.split("/")[1])} for value in link.get("addresses", []) if ":" not in value]
                    interfaces.append({"ifname": name, "addr_info": addresses})
            return NODE.Result(0, json.dumps(interfaces))
        if args[:4] == ["ip", "-j", "-4", "route"]:
            if self.default_routes_override is not None:
                return NODE.Result(0, json.dumps(self.default_routes_override))
            routes = []
            for kind in ("ethernets", "wifis"):
                for identifier, link in self.live_network[kind].items():
                    name = "eth0" if "match" in link else identifier
                    if link.get("dhcp4"):
                        routes.append({"dev": name, "metric": link.get("dhcp4-overrides", {}).get("route-metric", 600), "protocol": "dhcp"})
                    routes.extend({"dev": name, "metric": route["metric"], "protocol": "static", "gateway": route["via"]} for route in link.get("routes", []) if route.get("to") in ("default", "0.0.0.0/0") and route.get("table", 254) in (254, "main") and ":" not in route.get("via", ""))
            return NODE.Result(0, json.dumps(routes))
        if args[0] == "networkctl":
            name = args[-1]
            link = next(link for kind in ("ethernets", "wifis") for identifier, link in self.live_network[kind].items() if identifier == name or name == "eth0" and "match" in link)
            return NODE.Result(0, json.dumps({"Name": name, "RequiredForOnline": True if self.netplan_modern else not link.get("optional", False)}))
        if args[:2] == ["resolvectl", "dns"]:
            name = args[-1]
            link = next(link for kind in ("ethernets", "wifis") for identifier, link in self.live_network[kind].items() if identifier == name or name == "eth0" and "match" in link)
            return NODE.Result(0, "Link 2 (" + name + "): " + " ".join(link.get("nameservers", {}).get("addresses", [])) + "\n")
        if args[:3] == ["udevadm", "control", "--reload"]:
            return NODE.Result()
        if args[:2] == ["udevadm", "info"]:
            return NODE.Result(0, "ID_NET_LINK_FILE=/etc/systemd/network/10-wlan0.link\n")
        if args[:2] == ["iw", "dev"]:
            if args[3:5] == ["set", "power_save"]:
                self.wifi_power = args[-1] != "off"
                return NODE.Result()
            return NODE.Result(0, "Power save: " + ("on" if self.wifi_power else "off") + "\n")
        raise AssertionError("Unexpected fake command: " + args[0])

    @staticmethod
    def assert_sudo_candidate(content):
        if content != "pi ALL=(ALL) NOPASSWD:ALL\n":
            raise AssertionError("Unexpected managed sudo policy")

    def chrony_command(self, args):
        command = args[0]
        if command == "sources":
            return NODE.Result(0, "".join(f'^,{source["state"]},{address},3,4,{source["reach"]},1,0,0,0,0\n' for address, source in self.sources.items()))
        if command == "selectdata":
            lines = []
            for address, source in self.sources.items():
                options = "".join(letter if source[name] else "-" for name, letter in (("noselect", "N"), ("prefer", "P"), ("trust", "T"), ("require", "R"))) + "-"
                effective = source.get("effective_options", options)
                lines.append(",".join([source.get("selection_state", source["state"]), address, "Y" if source.get("authenticated", source["nts"]) else "N", *options, *effective, "0", "1", "0", "0", "Normal"]) + "\n")
            return NODE.Result(0, "".join(lines))
        if command == "authdata":
            return NODE.Result(0, "".join(f'{address},{"NTS" if source["nts"] else "-"},1,15,256,1,0,0,{source["cookies"]},100\n' for address, source in self.sources.items()))
        if command == "sourcename":
            return NODE.Result(0, self.sources[args[1]]["name"] + "\n")
        if command == "activity":
            offline = len(self.chrony_offline.intersection(self.sources))
            return NODE.Result(0, f"{len(self.sources) - offline},{offline},0,0,0\n")
        if command == "online":
            if len(args) != 2 or args[1] not in self.sources:
                raise AssertionError("Unscoped or unknown chrony connectivity change")
            self.chrony_offline.discard(args[1])
            return NODE.Result()
        if command == "add":
            name = args[2]
            address = self.pool_address if args[1] == "pool" and self.pool_address else name if name == "162.159.200.1" else "203.0.113." + str(20 + len(self.sources))
            if address in self.sources:
                return NODE.Result(1)
            source = self.source_record(name, nts="nts" in args, require="require" in args, prefer="prefer" in args)
            if not self.added_sources_ready:
                source.update(state="?", reach="0", cookies="0")
            self.sources[address] = source
            return NODE.Result()
        if command == "delete":
            del self.sources[args[1]]
            return NODE.Result()
        if command == "selectopts":
            source = self.sources[args[1]]
            for option in args[2:]:
                source[option[1:]] = option.startswith("+")
            return NODE.Result()
        if command in ("reselect", "waitsync"):
            return NODE.Result()
        raise AssertionError("Unexpected fake chrony operation")

    @contextlib.contextmanager
    def private_media_namespace(self):
        original_readlink = os.readlink
        original_statvfs = os.statvfs
        def readlink(path, *args, **kwargs):
            if Path(path) == self.root / "proc/self/ns/mnt":
                return "mnt:[fixture-private]"
            if Path(path) == self.root / "proc/1/ns/mnt":
                return "mnt:[fixture-host]"
            return original_readlink(path, *args, **kwargs)
        def statvfs(path):
            if Path(path) == self.media_mountpoint:
                return SimpleNamespace(f_flag=os.ST_RDONLY if self.media_readonly else 0)
            return original_statvfs(path)
        try:
            with mock.patch.object(NODE.os, "readlink", readlink), mock.patch.object(NODE.os, "statvfs", statvfs):
                yield
        finally:
            if self.media_mountpoint is not None:
                self.media_mountpoint.unlink()
                self.media_mountpoint.mkdir(mode=0o700)
                self.media_mountpoint = None

    def snapshot(self):
        result = {}
        for path in self.root.rglob("*"):
            if path.is_symlink():
                result[str(path.relative_to(self.root))] = ("link", os.readlink(path))
            elif path.is_file():
                result[str(path.relative_to(self.root))] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
        return result

    @contextlib.contextmanager
    def mounted(self):
        original_stat = Path.stat
        def fake_stat(path, *args, **kwargs):
            metadata = original_stat(path, *args, **kwargs)
            if path.is_relative_to(self.root):
                fields = {name: getattr(metadata, name) for name in dir(metadata) if name.startswith("st_")}
                fields.update(st_uid=0, st_gid=0)
                relative = str(path.relative_to(self.root))
                if relative == "dev/watchdog0" or relative.startswith("proc/") and "/fd/" in relative:
                    fields.update(st_mode=stat.S_IFCHR | 0o600, st_rdev=12345)
                if "/" + relative in self.media_device_numbers and not stat.S_ISLNK(metadata.st_mode):
                    fields.update(st_mode=stat.S_IFBLK | 0o600, st_rdev=self.media_device_numbers["/" + relative])
                return SimpleNamespace(**fields)
            return metadata
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(NODE, "ROOT", self.root))
            stack.enter_context(mock.patch.object(NODE, "run_command", side_effect=self.command))
            stack.enter_context(mock.patch.object(NODE, "run_netplan_try", side_effect=self.try_network))
            stack.enter_context(mock.patch.object(NODE.time, "time", return_value=2000))
            stack.enter_context(mock.patch.object(NODE.time, "monotonic", side_effect=self.advance_clock))
            stack.enter_context(mock.patch.object(NODE.time, "sleep"))
            stack.enter_context(mock.patch.object(NODE.socket, "gethostname", return_value=self.hostname))
            stack.enter_context(mock.patch.object(NODE.os, "sysconf", return_value=100))
            stack.enter_context(mock.patch.object(NODE.os, "getuid", return_value=0))
            stack.enter_context(mock.patch.object(NODE.os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.object(NODE.os, "fchown"))
            stack.enter_context(mock.patch.object(NODE.os, "chown"))
            stack.enter_context(mock.patch.object(Path, "stat", fake_stat))
            yield

    def pending_chrony(self, content):
        directory = self.root / NODE.CHRONY_PENDING_DIR.lstrip("/")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        data = content.encode()
        path = directory / (hashlib.sha256(data).hexdigest() + ".conf")
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def reconcile(self, check=False, role=None, activate=False, bootstrap=False, cleanup_only=False):
        helper = NODE.Reconciler(self.platform, role, check, self.source, activate, bootstrap, cleanup_only)
        status = helper.execute()
        return status, helper.records


class NodeConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.machine = Machine(self.temporary.name)
        self.mount = self.machine.mounted()
        self.mount.__enter__()
        self.addCleanup(self.mount.__exit__, None, None, None)

    def assert_status(self, expected, check=False, **kwargs):
        status, records = self.machine.reconcile(check, **kwargs)
        self.assertEqual(status, expected, records)
        return records

    def test_check_is_read_only_and_proves_consumer_state(self):
        before = self.machine.snapshot()
        self.assert_status(0, check=True)
        self.assertEqual(self.machine.snapshot(), before)
        forbidden = {"start", "restart", "reexec", "daemon-reexec", "set", "delete", "add", "selectopts", "reselect"}
        self.assertFalse(any(len(args) > 1 and args[1] in forbidden for args, _, _ in self.machine.calls))

    def test_repeated_native_timer_properties_converge_and_remain_current(self):
        timer = self.machine.units["node-health.timer"]
        timer.update(
            UnitFileState="disabled", ActiveState="inactive",
            TimersMonotonic=[
                "{ OnUnitInactiveUSec=15s ; next_elapse=7h 16min 26.954359s }",
                "{ OnBootUSec=1min ; next_elapse=1min }",
            ],
        )
        self.assert_status(1, check=True)
        self.assert_status(0)
        self.assertEqual(timer["UnitFileState"], "enabled")
        self.assertEqual(timer["ActiveState"], "active")
        self.assert_status(0, check=True)
        self.assertFalse(any(args[:2] in (["systemctl", "restart"], ["systemctl", "stop"]) for args, _, _ in self.machine.calls))

    def test_apply_converges_owned_drift_then_check_is_idempotent(self):
        self.machine.write("/etc/apt/apt.conf.d/94cloud-init-config", 'Acquire::Retries "1";\n')
        self.machine.write("/proc/sys/net/core/rmem_max", "1\n")
        self.machine.write("/proc/20/oom_score_adj", "0\n")
        self.machine.write("/etc/localtime", "utc-zone")
        self.machine.timezone_name = "UTC"
        self.machine.prefs = {"RunSSH": True, "Hostname": "old-node"}
        passwd = self.machine.root / "etc/passwd"
        passwd.write_text(passwd.read_text().replace("Pi:/home/pi:/bin/bash", "Pi:/home/pi:/bin/sh"))
        group = self.machine.root / "etc/group"
        group.write_text(group.read_text().replace("adm:x:4:pi", "adm:x:4:"))
        shadow = self.machine.root / "etc/shadow"
        shadow.write_text(shadow.read_text().replace("root:!:20000", "root:PROVIDER_HASH:0"))
        (self.machine.root / "etc/sudoers.d/90-nodes-pi").unlink()
        self.machine.units["node-health.timer"].update(UnitFileState="disabled", ActiveState="inactive")
        self.machine.units["node-health.service"]["UnitFileState"] = "enabled"
        preserved = {path: (self.machine.root / path).read_bytes() for path in ("home/pi/.ssh/authorized_keys", "etc/systemd/system/k3s.service.env", "var/lib/rancher/k3s/server/node-token", "var/lib/node-health/wifi-recovery/reboot-budget")}
        self.assert_status(1, check=True)
        self.assert_status(0)
        self.assertIn("pi:$6$PRIVATE_PASSWORD", shadow.read_text())
        self.assertIn("other:OTHER_PRIVATE", shadow.read_text())
        for path, data in preserved.items():
            self.assertEqual((self.machine.root / path).read_bytes(), data)
        before = self.machine.snapshot()
        self.assert_status(0, check=True)
        self.assertEqual(self.machine.snapshot(), before)
        sysctls = [args[2].split("=", 1)[0] for args, _, _ in self.machine.calls if args[:2] == ["sysctl", "-w"]]
        self.assertEqual(sysctls, ["net.core.rmem_max"])
        self.assertEqual((self.machine.root / "proc/sys/net/ipv4/ip_forward").read_text(), "0\n")
        self.assertFalse(any(args[:2] in (["tailscale", "up"], ["tailscale", "down"], ["systemctl", "daemon-reexec"]) for args, _, _ in self.machine.calls))

    def test_cloud_never_reads_or_applies_wifi_policy(self):
        self.assert_status(0)
        self.assertFalse(any(args[0] in ("iw", "udevadm", "netplan") for args, _, _ in self.machine.calls))
        self.assertFalse((self.machine.root / "etc/netplan").exists())

    def test_role_changes_ambiguity_and_unknown_os_refuse_all_mutation(self):
        before = self.machine.snapshot()
        self.assert_status(1, role="agent")
        self.machine.units["k3s-agent.service"] = {"LoadState": "loaded", "FragmentPath": "/etc/systemd/system/k3s-agent.service"}
        self.assert_status(1)
        self.machine.units["k3s-agent.service"] = {"LoadState": "not-found", "FragmentPath": ""}
        self.machine.write("/etc/os-release", "ID=other\n")
        after_input = self.machine.snapshot()
        self.assert_status(1)
        self.assertEqual(self.machine.snapshot(), after_input)
        self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args, _, _ in self.machine.calls))
        self.assertEqual(before["home/pi/.ssh/authorized_keys"], after_input["home/pi/.ssh/authorized_keys"])

    def test_no_installed_role_requires_bootstrap_role_and_stays_pending(self):
        self.machine.units["k3s.service"] = {"LoadState": "not-found", "FragmentPath": ""}
        self.assert_status(1)
        records = self.assert_status(2, role="server")
        self.assertTrue(any(record["item"] == "k3s-runtime" and record["state"] == "pending" for record in records))
        self.assertFalse(any(args[:2] == ["systemctl", "start"] and "k3s" in args[-1] for args, _, _ in self.machine.calls))

    def test_native_single_argv_server_title_uses_startup_and_node_evidence(self):
        self.machine.write("/proc/22/cmdline", "/usr/local/bin/k3s server\0")
        self.assert_status(0, check=True)
        self.machine.write("/etc/systemd/system/k3s.service", "[Service]\nType=notify\n")
        self.assert_status(2, check=True)
        self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args, _, _ in self.machine.calls))

    def test_titled_process_without_registered_address_proof_is_not_match(self):
        self.machine.write("/proc/22/cmdline", "/usr/local/bin/k3s server\0")
        self.machine.node_addresses = [{"type": "InternalIP", "address": "192.0.2.200"}]
        self.assert_status(2, check=True)

    def test_inactive_blocked_join_is_pending_without_restart(self):
        self.machine.units["k3s.service"].update(ActiveState="activating", SubState="start", MainPID="0")
        self.assert_status(2)
        self.assert_status(2, activate=True)
        self.assertFalse(any(args[:2] in (["systemctl", "restart"], ["systemctl", "start"]) and "k3s" in args[-1] for args, _, _ in self.machine.calls))

    def test_ha_unchanged_preserves_mtime_and_changed_needs_activation(self):
        active = self.machine.root / NODE.HA_PATH.lstrip("/")
        original_mtime = active.stat().st_mtime_ns
        self.assert_status(0)
        self.assertEqual(active.stat().st_mtime_ns, original_mtime)
        active.write_text(active.read_text().replace("heartbeat-interval=500", "heartbeat-interval=100"))
        self.assert_status(2)
        self.assertEqual(active.read_bytes(), (self.machine.source / NODE.HA_TEMPLATE.lstrip("/")).read_bytes())
        self.assert_status(2, check=True)
        self.assertFalse(any(args[:2] == ["systemctl", "restart"] and args[-1] == "k3s.service" for args, _, _ in self.machine.calls))

    def test_missing_package_installs_only_missing_without_upgrade(self):
        self.machine.installed.remove("chrony")
        self.assert_status(1, check=True)
        self.assertFalse(any(args[0] == "apt-get" for args, _, _ in self.machine.calls))
        self.assert_status(0)
        installs = [(args, timeout, environment) for args, timeout, environment in self.machine.calls if args[0] == "apt-get" and "install" in args]
        self.assertEqual(len(installs), 1)
        arguments, timeout, environment = installs[0]
        self.assertEqual(arguments[arguments.index("--no-remove") + 1:], ["chrony"])
        self.assertIn("--no-upgrade", arguments)
        self.assertNotIn("upgrade", arguments)
        self.assertEqual(environment, {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"})
        self.assertGreater(timeout, 0)

    def test_package_account_creation_succeeds_with_expired_provider_root(self):
        shadow = self.machine.root / "etc/shadow"
        shadow.write_text(shadow.read_text().replace("root:!:20000", "root:PROVIDER_HASH:0"))
        self.machine.installed.remove("chrony")
        self.assert_status(0)
        self.assertIn("chrony", self.machine.installed)
        root = shadow.read_text().splitlines()[0].split(":")
        self.assertEqual(root[1], "!")
        self.assertGreater(int(root[2]), 0)

    def test_configured_nts_warmup_is_pending_without_recreating_sources_or_claiming_authentication(self):
        source = self.machine.sources["203.0.113.10"]
        source.update(authenticated=False, cookies="0", reach="0", state="?")
        before = {address: dict(value) for address, value in self.machine.sources.items()}
        files = self.machine.snapshot()
        self.assert_status(2, check=True)
        self.assert_status(2)
        self.assertEqual(self.machine.sources, before)
        self.assertEqual(self.machine.snapshot(), files)
        source.update(cookies="8", reach="377")
        self.assert_status(2, check=True)
        self.assertFalse(any(args[:2] in (["chronyc", "add"], ["chronyc", "delete"], ["chronyc", "selectopts"], ["chronyc", "makestep"], ["systemctl", "restart"]) for args, _, _ in self.machine.calls))
        source.update(authenticated=True, state="*", effective_options="-PTR-")
        self.assert_status(0, check=True)

    def test_offline_managed_addresses_resume_without_claiming_stale_nts_samples_are_fresh(self):
        pool = self.machine.sources.pop("203.0.113.10")
        self.machine.sources["162.159.200.123"] = pool
        managed = {"162.159.200.1", "162.159.200.123"}
        for address in managed:
            self.machine.sources[address].update(reach="0", state="+")
        self.machine.sources["2620:2d:4000:1::2123"] = self.machine.source_record("1.ntp.ubuntu.com", nts=True, state="*")
        self.machine.sources["91.189.91.157"]["reach"] = "0"
        self.machine.chrony_offline = managed | {"91.189.91.157"}
        self.machine.chrony_routes = {address: {"dev": "eth0", "prefsrc": "192.0.2.1"} for address in managed}
        self.machine.chrony_links["eth0"] = True
        files = self.machine.snapshot()
        sources = {address: dict(source) for address, source in self.machine.sources.items()}
        self.assert_status(2, check=True)
        self.assertEqual(self.machine.chrony_offline, managed | {"91.189.91.157"})
        self.assertFalse(any(args[:2] == ["chronyc", "online"] for args, _, _ in self.machine.calls))
        self.assert_status(2)
        self.assertEqual(self.machine.chrony_offline, {"91.189.91.157"})
        self.assertEqual({tuple(args) for args, _, _ in self.machine.calls if args[:2] == ["chronyc", "online"]}, {("chronyc", "online", address) for address in managed})
        self.assertEqual(self.machine.sources, sources)
        self.assertEqual(self.machine.snapshot(), files)
        self.assert_status(2)
        self.assertEqual(self.machine.sources, sources)
        self.assertFalse(any(args[:2] in (["chronyc", "add"], ["chronyc", "delete"], ["chronyc", "selectopts"], ["chronyc", "reselect"], ["chronyc", "makestep"], ["chronyc", "burst"], ["systemctl", "restart"]) for args, _, _ in self.machine.calls))
        pool.update(reach="1", authenticated=False)
        self.assert_status(2, check=True)
        pool["authenticated"] = True
        self.assert_status(0, check=True)
        self.assertEqual(self.machine.chrony_offline, {"91.189.91.157"})
        self.assertEqual(self.machine.snapshot(), files)

    def test_offline_managed_source_waits_for_usable_route_and_up_interface(self):
        address = "203.0.113.10"
        self.machine.sources[address].update(reach="0", state="?")
        self.machine.chrony_offline.add(address)
        files = self.machine.snapshot()
        for route, up in ((None, True), ({"type": "blackhole", "dev": "eth0", "prefsrc": "192.0.2.1"}, True), ({"dev": "eth0", "prefsrc": "192.0.2.1"}, False), ({"dev": "eth0", "prefsrc": "2001:db8::1"}, True)):
            with self.subTest(route=route, up=up):
                self.machine.calls.clear()
                self.machine.chrony_routes[address] = route
                self.machine.chrony_links["eth0"] = up
                self.assert_status(2)
                self.assertEqual(self.machine.chrony_offline, {address})
                self.assertFalse(any(args[:2] == ["chronyc", "online"] for args, _, _ in self.machine.calls))
                self.assertEqual(self.machine.snapshot(), files)

    def test_offline_inventory_or_scoped_online_failure_is_unknown_and_secret_safe(self):
        address = "203.0.113.10"
        self.machine.sources[address].update(reach="0", state="?")
        self.machine.chrony_offline.add(address)
        self.machine.chrony_routes[address] = {"dev": "eth0", "prefsrc": "192.0.2.1"}
        self.machine.chrony_links["eth0"] = True
        files = self.machine.snapshot()
        for prefix in (["chronyc", "-n", "-c", "activity"], ["chronyc", "online"]):
            with self.subTest(operation=prefix):
                self.machine.fail = lambda args: args[:len(prefix)] == prefix
                records = self.assert_status(1)
                self.assertTrue(any(record["item"] == "chrony" and record["state"] == "unknown" for record in records))
                self.assertNotIn("PRIVATE_", json.dumps(records))
                self.assertEqual(self.machine.chrony_offline, {address})
                self.assertEqual(self.machine.sources[address]["cookies"], "8")
                self.assertEqual(self.machine.snapshot(), files)

    def test_nts_warmup_does_not_hide_configured_selection_policy_drift(self):
        source = self.machine.sources["203.0.113.10"]
        source.update(prefer=False, authenticated=False, cookies="0", reach="0", state="?")
        records = self.assert_status(1, check=True)
        self.assertTrue(any(record["item"] == "chrony" and record["state"] == "drift" for record in records))
        self.assert_status(2)
        self.assertTrue(source["prefer"])
        self.assertFalse(source["authenticated"])
        self.assertFalse(any(args[:2] in (["chronyc", "add"], ["chronyc", "delete"], ["chronyc", "makestep"], ["systemctl", "restart"]) for args, _, _ in self.machine.calls))
        source.update(authenticated=True, cookies="8", reach="377", state="*")
        self.assert_status(0, check=True)

    def test_unauthenticated_nts_cannot_retire_previous_managed_source(self):
        self.machine.pending_chrony("server old.example.com iburst require\npool time.cloudflare.com iburst maxsources 1 nts prefer\nmakestep 1.0 3\n")
        self.machine.sources["198.51.100.9"] = self.machine.source_record("old.example.com", require=True)
        del self.machine.sources["162.159.200.1"]
        self.machine.sources["203.0.113.10"].update(authenticated=False, state="+")
        self.machine.added_sources_ready = False
        self.assert_status(2)
        self.assertIn("198.51.100.9", self.machine.sources)
        self.assertFalse(any(args == ["chronyc", "delete", "198.51.100.9"] for args, _, _ in self.machine.calls))
        self.assertFalse(any(args[:2] in (["chronyc", "makestep"], ["systemctl", "restart"]) for args, _, _ in self.machine.calls))

    def test_chrony_selective_cutover_preserves_unmanaged_sources_and_clock(self):
        self.machine.pending_chrony("server old.example.com iburst require\npool time.cloudflare.com iburst maxsources 1 nts prefer\nmakestep 1.0 3\n")
        self.machine.sources["198.51.100.9"] = self.machine.source_record("old.example.com", require=True)
        del self.machine.sources["162.159.200.1"]
        self.machine.sources["203.0.113.10"].update(prefer=False)
        ubuntu = dict(self.machine.sources["91.189.91.157"])
        clock_during_retirement = []
        def observe(args):
            if args == ["chronyc", "delete", "198.51.100.9"]:
                clock_during_retirement.append(any(source["nts"] and source.get("authenticated", True) and source["state"] in ("*", "+") and source["reach"] not in ("0", "000") and int(source["cookies"]) > 0 for source in self.machine.sources.values()))
            return False
        self.machine.fail = observe
        self.assert_status(0)
        self.assertEqual(self.machine.sources["91.189.91.157"], ubuntu)
        self.assertNotIn("198.51.100.9", self.machine.sources)
        calls = [args for args, _, _ in self.machine.calls]
        self.assertEqual(clock_during_retirement, [True])
        self.assertIn("162.159.200.1", self.machine.sources)
        self.assertFalse(any("makestep" in args or args[:2] == ["systemctl", "restart"] for args in calls))
        self.assert_status(0, check=True)

    def test_pending_cutover_ownership_survives_multiple_invocations_and_policy_versions(self):
        snapshots = [
            self.machine.pending_chrony("server old.example.com iburst require\nmakestep 1.0 3\n"),
            self.machine.pending_chrony("server older.example.com iburst require\nmakestep 1.0 3\n"),
        ]
        ubuntu = dict(self.machine.sources["91.189.91.157"])
        self.machine.sources = {
            "198.51.100.9": self.machine.source_record("old.example.com", require=True),
            "198.51.100.10": self.machine.source_record("older.example.com", require=True),
            "91.189.91.157": dict(ubuntu),
        }
        self.machine.added_sources_ready = False
        before = self.machine.snapshot()
        self.assert_status(1, check=True)
        self.assertEqual(self.machine.snapshot(), before)
        self.assertFalse((self.machine.root / NODE.CHRONY_PROGRESS_PATH.lstrip("/")).exists())
        self.assert_status(2)
        self.assertTrue(all(path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600 for path in snapshots))
        progress = self.machine.root / NODE.CHRONY_PROGRESS_PATH.lstrip("/")
        self.assertEqual(stat.S_IMODE(progress.stat().st_mode), 0o600)
        pending = self.machine.snapshot()
        self.assert_status(1, check=True)
        self.assertEqual(self.machine.snapshot(), pending)
        for source in self.machine.sources.values():
            if source["name"] in ("162.159.200.1", "time.cloudflare.com"):
                source.update(reach="377", cookies="8" if source["nts"] else "0", state="*", authenticated=source["nts"])
        self.assert_status(0)
        self.assertNotIn("198.51.100.9", self.machine.sources)
        self.assertNotIn("198.51.100.10", self.machine.sources)
        self.assertEqual(self.machine.sources["91.189.91.157"], ubuntu)
        self.assertTrue(all(not path.exists() for path in snapshots))
        self.assertFalse(progress.exists())
        self.assert_status(0, check=True)
        self.assertFalse(any(args[:2] in (["chronyc", "makestep"], ["systemctl", "restart"]) for args, _, _ in self.machine.calls))

    def test_pending_transport_progress_prevents_repeated_pool_recreation(self):
        snapshot = self.machine.pending_chrony("server 162.159.200.1 iburst require\npool time.cloudflare.com iburst maxsources 4 nts prefer\nmakestep 1.0 3\n")
        self.machine.added_sources_ready = False
        self.assert_status(2)
        pool = next((address, source) for address, source in self.machine.sources.items() if source["name"] == "time.cloudflare.com")
        self.assertNotEqual(pool[0], "203.0.113.10")
        start = len(self.machine.calls)
        self.assert_status(2)
        self.assertIn(pool[0], self.machine.sources)
        self.assertFalse(any(args[:2] in (["chronyc", "add"], ["chronyc", "delete"]) for args, _, _ in self.machine.calls[start:]))
        pool[1].update(state="*", reach="377", cookies="8", authenticated=True)
        self.assert_status(0)
        self.assertFalse(snapshot.exists())
        self.assertFalse((self.machine.root / NODE.CHRONY_PROGRESS_PATH.lstrip("/")).exists())

    def test_unresponsive_pool_candidates_are_not_counted_as_responding_maxsources(self):
        candidate = self.machine.source_record("time.cloudflare.com", nts=True, prefer=True, state="?")
        candidate.update(reach="0", cookies="0", authenticated=False)
        self.machine.sources["203.0.113.11"] = candidate
        before = {address: dict(source) for address, source in self.machine.sources.items()}
        self.assert_status(0, check=True)
        self.assert_status(0)
        self.assertEqual(self.machine.sources, before)
        self.assertFalse(any(args[:2] in (["chronyc", "add"], ["chronyc", "delete"]) for args, _, _ in self.machine.calls))

    def test_pending_snapshot_integrity_failure_refuses_source_mutations(self):
        snapshot = self.machine.pending_chrony("server old.example.com iburst require\n")
        snapshot.write_text("server unknown.example.com iburst require\n")
        before = self.machine.snapshot()
        self.assert_status(1)
        self.assertEqual(self.machine.snapshot(), before)
        self.assertFalse(any(args[:2] in (["chronyc", "add"], ["chronyc", "delete"], ["chronyc", "selectopts"]) for args, _, _ in self.machine.calls))

    def test_safe_prepublication_temporary_is_not_policy_or_cleanup_target(self):
        directory = self.machine.root / NODE.CHRONY_PENDING_DIR.lstrip("/")
        directory.mkdir(mode=0o700, parents=True)
        temporary = directory / ".snapshot-interrupted.tmp"
        temporary.write_text("partial unpublished old policy")
        temporary.chmod(0o600)
        self.assert_status(0, check=True)
        self.assert_status(0)
        self.assertTrue(temporary.exists())
        temporary.chmod(0o644)
        self.assert_status(1)
        self.assertTrue(temporary.exists())

    def test_pool_limit_drift_is_detected_and_selectively_recreated(self):
        self.machine.sources["203.0.113.11"] = self.machine.source_record("time.cloudflare.com", nts=True, prefer=True, state="+")
        self.assert_status(1, check=True)
        self.assert_status(0)
        cloud = [source for source in self.machine.sources.values() if source["name"] == "time.cloudflare.com"]
        self.assertEqual(len(cloud), 1)
        self.assertIn("91.189.91.157", self.machine.sources)

    def test_chrony_cannot_replace_authentication_without_synchronized_alternative(self):
        self.machine.sources = {"203.0.113.10": self.machine.source_record("time.cloudflare.com", nts=False, prefer=True)}
        self.machine.added_sources_ready = False
        self.assert_status(1, check=True)
        self.assert_status(2)
        self.assertIn("203.0.113.10", self.machine.sources)
        self.assertFalse(any(args[:2] == ["chronyc", "delete"] for args, _, _ in self.machine.calls))

    def test_ssh_drift_is_reported_without_settings_changes(self):
        self.machine.write("/proc/21/oom_score_adj", "0\n")
        self.machine.prefs["RunSSH"] = False
        before = self.machine.snapshot()
        self.assert_status(2, activate=True)
        self.assertEqual(self.machine.snapshot(), before)
        self.assertFalse(self.machine.prefs["RunSSH"])
        self.assertFalse(any("--ssh=true" in args or args[:2] == ["tailscale", "set"] or args[:2] == ["systemctl", "restart"] for args, _, _ in self.machine.calls))

    def test_authorized_watchdog_fallback_reexecs_only_for_runtime_drift(self):
        self.machine.manager["RuntimeWatchdogUSec"] = "0"
        self.assert_status(0, activate=True)
        self.assert_status(0, activate=True)
        self.assertEqual(sum(args[:2] == ["systemctl", "daemon-reexec"] for args, _, _ in self.machine.calls), 1)

    def test_unregistered_active_server_does_not_restart_for_ha_drift(self):
        active = self.machine.root / NODE.HA_PATH.lstrip("/")
        active.write_text(active.read_text().replace("heartbeat-interval=500", "heartbeat-interval=100"))
        self.machine.ready = False
        self.assert_status(2, activate=True)
        self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args, _, _ in self.machine.calls))

    def test_socket_activated_ssh_has_no_live_oom_target(self):
        self.machine.units["ssh.service"].update(ActiveState="inactive", MainPID="0")
        self.machine.units["ssh.socket"] = {"LoadState": "loaded", "ActiveState": "active"}
        self.assert_status(0, check=True)
        self.assertFalse(any(args[:2] == ["systemctl", "start"] for args, _, _ in self.machine.calls))

    def test_unsupported_watchdog_setter_never_claims_runtime_parity(self):
        self.machine.manager["RuntimeWatchdogUSec"] = "0"
        self.assert_status(2)
        setters = [(args, timeout) for args, timeout, _ in self.machine.calls if args[0] == "busctl"]
        self.assertTrue(setters)
        self.assertTrue(all(timeout > 0 and "--timeout=10" in args for args, timeout in setters))
        self.assertFalse(any("daemon-reexec" in args for args, _, _ in self.machine.calls))

    def test_other_watchdog_owner_prevents_pid1_arming(self):
        (self.machine.root / "proc/99/fd").mkdir(parents=True)
        (self.machine.root / "proc/99/fd/0").symlink_to("../../../dev/watchdog0")
        self.machine.manager["RuntimeWatchdogUSec"] = "0"
        self.assert_status(2)
        self.assertFalse(any(args[0] == "busctl" for args, _, _ in self.machine.calls))

    def test_unsupported_watchdog_is_honest_and_never_reexecutes(self):
        (self.machine.root / "proc/1/fd/0").unlink()
        (self.machine.root / "dev/watchdog0").unlink()
        self.assert_status(2)
        loaders = [args for args, _, _ in self.machine.calls if args[0] == "/usr/local/bin/watchdog-load.sh"]
        self.assertEqual(loaders, [["/usr/local/bin/watchdog-load.sh", "--no-reexec"]])
        self.assertFalse(any("daemon-reexec" in args for args, _, _ in self.machine.calls))

    def test_empty_native_swap_unit_inventory_exit_one_is_valid(self):
        self.machine.swap_inventory = NODE.Result(1, "", "")
        self.assert_status(0, check=True)

    def test_swap_inventory_bus_error_is_not_treated_as_empty(self):
        self.machine.swap_inventory = NODE.Result(1, "", "PRIVATE_BUS_ERROR")
        records = self.assert_status(1, check=True)
        self.assertTrue(any(record["item"] == "swap" and record["state"] == "unknown" for record in records))
        self.assertNotIn("PRIVATE_", json.dumps(records))
        self.machine.swap_inventory = NODE.Result(1, "unexpected output", "")
        self.assert_status(1, check=True)

    def test_used_swap_and_in_use_multipath_remain_pending_and_untouched(self):
        self.machine.write("/proc/swaps", "Filename Type Size Used Priority\n/swap.img file 4096 64 -2\n")
        self.machine.write("/sys/block/dm-0/dm/uuid", "mpath-private-identifier\n")
        self.assert_status(2)
        self.assertFalse(any(args[0] == "swapoff" or args[:2] == ["systemctl", "mask"] for args, _, _ in self.machine.calls))

    def test_failed_command_cannot_claim_complete_or_expose_secrets(self):
        self.machine.fail = lambda args: args[:2] == ["apt-config", "dump"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = NODE.main(["--platform", "cloud", "--check", "--source-root", str(self.machine.source)])
        self.assertEqual(status, 1)
        self.assertNotIn("PRIVATE_", output.getvalue())
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[-1]["status"], "drift-or-unknown")
        self.assertTrue(any(record.get("state") == "unknown" for record in records))

    def test_malformed_secret_yaml_has_no_traceback_or_secret_disclosure(self):
        self.machine.write(NODE.HA_PATH, "token: [PRIVATE_YAML_SECRET\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = NODE.main(["--platform", "cloud", "--check", "--source-root", str(self.machine.source)])
        self.assertEqual(status, 1)
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_external_config_override_is_not_hidden_or_deleted(self):
        self.machine.write("/etc/rancher/k3s/config.yaml.d/99-external.yaml", "etcd-arg:\n  - election-timeout=1000\n")
        original = (self.machine.root / "etc/rancher/k3s/config.yaml.d/99-external.yaml").read_bytes()
        self.assert_status(1)
        self.assertEqual((self.machine.root / "etc/rancher/k3s/config.yaml.d/99-external.yaml").read_bytes(), original)


class PhysicalNodeConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.machine = Machine(self.temporary.name, platform="physical")
        self.mount = self.machine.mounted()
        self.mount.__enter__()
        self.addCleanup(self.mount.__exit__, None, None, None)

    def test_current_physical_consumers_are_proved_without_changes(self):
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 0, records)
        self.assertEqual(self.machine.snapshot(), before)

    def test_owned_metric_drift_is_validated_privately_without_live_activation(self):
        path = self.machine.root / "etc/netplan/50-cloud-init.yaml"
        original = yaml.safe_load(path.read_text())
        historical = original["network"]["wifis"]["wlan0"]["access-points"]
        original["network"]["wifis"]["wlan0"]["dhcp4-overrides"]["route-metric"] = 999
        path.write_text(yaml.safe_dump(original))
        self.machine.wifi_power = True
        status, records = self.machine.reconcile()
        self.assertEqual(status, 2, records)
        updated = yaml.safe_load(path.read_text())
        self.assertEqual(updated["network"]["wifis"]["wlan0"]["access-points"], historical)
        self.assertEqual(updated["network"]["wifis"]["wlan0"]["dhcp4-overrides"]["route-metric"], 999)
        self.assertFalse(self.machine.wifi_power)
        self.assertFalse(any(args[:2] in (["netplan", "apply"], ["netplan", "try"], ["systemctl", "restart"]) or args[0] == "native-netplan-try" or "reboot" in args for args, _, _ in self.machine.calls))
        validation = [args for args, _, _ in self.machine.calls if args[:2] == ["netplan", "generate"]]
        self.assertTrue(validation)
        self.assertTrue(all("--root-dir" in args and Path(args[-1]) != self.machine.root for args in validation))
        self.assertNotIn("PRIVATE_WIFI_PSK", json.dumps(records))
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 2, records)

    def test_missing_authoritative_credentials_are_not_replaced(self):
        path = self.machine.root / "etc/netplan/50-cloud-init.yaml"
        value = yaml.safe_load(path.read_text())
        del value["network"]["wifis"]["wlan0"]["access-points"]
        path.write_text(yaml.safe_dump(value))
        before = path.read_bytes()
        status, records = self.machine.reconcile()
        self.assertEqual(status, 1, records)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(any(record["state"] == "unknown" and record["item"] == "network" for record in records))


class ProfileActivationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.machine = Machine(self.temporary.name, platform="physical", hostname="fraguinha")
        self.real_run_command = NODE.run_command
        self.mount = self.machine.mounted()
        self.mount.__enter__()
        self.addCleanup(self.mount.__exit__, None, None, None)

    def drift_network(self):
        path = self.machine.root / "etc/netplan/50-cloud-init.yaml"
        document = yaml.safe_load(path.read_text())
        document["network"]["wifis"]["wlan0"]["routes"][0]["metric"] = 999
        document["network"]["wifis"]["wlan0"]["optional"] = False
        path.write_text(yaml.safe_dump(document))
        self.machine.apply_network_backend(document["network"])
        return path, path.read_bytes()

    def test_default_preserves_static_addresses_credentials_and_ssh_settings(self):
        path, original = self.drift_network()
        status, records = self.machine.reconcile()
        self.assertEqual(status, 2, records)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(yaml.safe_load(original)["network"]["wifis"]["wlan0"]["addresses"], ["192.168.68.64/22"])
        self.assertFalse(any(args[0] in ("systemd-run", "native-netplan-try") or args[:2] == ["netplan", "apply"] for args, _, _ in self.machine.calls))

    def test_guarded_native_activation_preserves_static_addresses_and_secrets(self):
        path, original = self.drift_network()
        identity_paths = ("etc/systemd/system/k3s.service.env", "var/lib/rancher/k3s/server/node-token", "home/pi/.ssh/authorized_keys", "proc/21/oom_score_adj", "var/lib/node-health/wifi-recovery/reboot-budget")
        identities = {name: (self.machine.root / name).read_bytes() for name in identity_paths}
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        updated = yaml.safe_load(path.read_text())["network"]
        previous = yaml.safe_load(original)["network"]
        self.assertEqual(updated["wifis"]["wlan0"]["addresses"], previous["wifis"]["wlan0"]["addresses"])
        self.assertEqual(updated["wifis"]["wlan0"]["access-points"], previous["wifis"]["wlan0"]["access-points"])
        self.assertEqual(updated["wifis"]["wlan0"]["routes"][0]["metric"], 100)
        for name, content in identities.items():
            self.assertEqual((self.machine.root / name).read_bytes(), content)
        operations = [args for args, _, _ in self.machine.calls]
        armed = next(index for index, args in enumerate(operations) if args[0] == "systemd-run")
        tried = next(index for index, args in enumerate(operations) if args[0] == "native-netplan-try")
        self.assertLess(armed, tried)
        self.assertFalse(any(args[:2] == ["netplan", "apply"] for args in operations))
        self.assertFalse(list((self.machine.root / "run").glob("nodes-network-*")))
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)

    def test_ipv4_pool_cutover_frees_only_proven_old_address_with_healthy_unmanaged_nts(self):
        snapshot = self.machine.pending_chrony("server 162.159.200.1 iburst require\nserver 162.159.200.123 iburst require\nmakestep 1.0 3\n")
        self.machine.sources["162.159.200.123"] = self.machine.source_record("162.159.200.123", require=True, state="?")
        self.machine.sources["162.159.200.123"].update(reach="0")
        self.machine.sources["91.189.91.157"] = self.machine.source_record("ntp.ubuntu.com", nts=True, state="*")
        ubuntu = dict(self.machine.sources["91.189.91.157"])
        del self.machine.sources["203.0.113.10"]
        for address in ("2606:4700:f1::1", "2606:4700:f1::123"):
            source = self.machine.source_record("time.cloudflare.com", nts=True, prefer=True, state="?")
            source.update(reach="0", cookies="0", authenticated=False)
            self.machine.sources[address] = source
        self.machine.pool_address = "162.159.200.123"
        self.machine.added_sources_ready = False
        self.assertEqual(self.machine.reconcile(check=True)[0], 1)
        status, records = self.machine.reconcile()
        self.assertEqual(status, 2, records)
        self.assertEqual(self.machine.sources["162.159.200.123"]["name"], "time.cloudflare.com")
        self.assertFalse(any(":" in address for address, source in self.machine.sources.items() if source["name"] == "time.cloudflare.com"))
        self.assertEqual(self.machine.sources["91.189.91.157"], ubuntu)
        calls = [args for args, _, _ in self.machine.calls]
        retired = calls.index(["chronyc", "delete", "162.159.200.123"])
        added = next(index for index, args in enumerate(calls) if args[:3] == ["chronyc", "add", "pool"])
        self.assertLess(retired, added)
        self.assertIn("ipv4", calls[added])
        start = len(self.machine.calls)
        self.assertEqual(self.machine.reconcile()[0], 2)
        self.assertFalse(any(args[:2] in (["chronyc", "add"], ["chronyc", "delete"]) for args, _, _ in self.machine.calls[start:]))
        self.machine.sources["162.159.200.123"].update(reach="377", cookies="8", authenticated=True, state="*")
        status, records = self.machine.reconcile()
        self.assertEqual(status, 0, records)
        self.assertFalse(snapshot.exists())
        self.assertEqual(self.machine.sources["91.189.91.157"], ubuntu)
        self.assertFalse(any(args[:2] in (["chronyc", "makestep"], ["systemctl", "restart"]) for args, _, _ in self.machine.calls))

    def test_native_optional_wait_condition_allows_guarded_static_activation(self):
        path = self.machine.root / self.machine.network_file
        document = yaml.safe_load(path.read_text())
        access_points = document["network"]["wifis"]["wlan0"]["access-points"]
        addresses = document["network"]["wifis"]["wlan0"]["addresses"]
        document["network"]["wifis"]["wlan0"]["routes"][0]["metric"] = 999
        path.write_text(yaml.safe_dump(document))
        self.machine.netplan_modern = True
        self.machine.apply_network_backend(document["network"])
        protected = {
            name: (self.machine.root / name).read_bytes() for name in (
                "home/pi/.ssh/authorized_keys", "proc/21/oom_score_adj",
                "var/lib/node-health/wifi-recovery/reboot-budget", "etc/systemd/system/k3s.service.env",
            )
        }
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        updated = yaml.safe_load(path.read_text())["network"]["wifis"]["wlan0"]
        self.assertEqual(updated["addresses"], addresses)
        self.assertEqual(updated["access-points"], access_points)
        self.assertEqual(updated["routes"][0]["metric"], 100)
        for name, content in protected.items():
            self.assertEqual((self.machine.root / name).read_bytes(), content)
        self.assertFalse(list((self.machine.root / "run").glob("nodes-network-*")))
        before = self.machine.snapshot()
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)
        self.assertEqual(self.machine.snapshot(), before)

    def test_native_optional_wait_condition_cannot_hide_wrong_live_default_route(self):
        self.machine.netplan_modern = True
        self.machine.apply_network_backend(self.machine.live_network)
        self.machine.default_routes_override = [{"dev": "wlan0", "gateway": "192.168.68.1", "protocol": "dhcp", "metric": 1024}]
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 2, records)
        self.assertEqual(self.machine.snapshot(), before)
        self.assertFalse(any(args[0] == "native-netplan-try" or args[:2] == ["netplan", "apply"] for args, _, _ in self.machine.calls))

    def test_native_generated_optional_condition_requires_loaded_effective_condition(self):
        self.machine.netplan_modern = True
        self.machine.apply_network_backend(self.machine.live_network)
        self.machine.wait_conditions = [["ConditionCapability", False, False, "CAP_NET_ADMIN", 0]]
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 2, records)
        self.assertFalse(any(args[0] == "native-netplan-try" for args, _, _ in self.machine.calls))

    def test_native_optional_enablement_dangling_symlink_is_not_absence_proof(self):
        self.machine.netplan_modern = True
        self.machine.apply_network_backend(self.machine.live_network)
        enablement = self.machine.root / "run/systemd/generator/network-online.target.wants/systemd-networkd-wait-online.service"
        enablement.parent.mkdir(parents=True)
        enablement.symlink_to("missing-unit")
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 2, records)
        self.assertTrue(enablement.is_symlink())
        self.assertFalse(any(args[0] == "native-netplan-try" for args, _, _ in self.machine.calls))

    def test_activation_preserves_unowned_link_fields_routes_and_interfaces(self):
        path, _ = self.drift_network()
        document = yaml.safe_load(path.read_text())
        ethernet = document["network"]["ethernets"]["eth0"]
        ethernet.update(dhcp6=True, mtu=1420, **{"routing-policy": [{"from": "10.40.0.0/16", "table": 100, "priority": 1000}]})
        ethernet["dhcp4-overrides"]["use-dns"] = False
        wifi = document["network"]["wifis"]["wlan0"]
        wifi.update(dhcp6=True, mtu=1400, **{"routing-policy": [{"from": "192.168.68.64/32", "table": 100, "priority": 1001}]})
        wifi["nameservers"]["search"] = ["home.internal"]
        wifi["routes"][0]["on-link"] = True
        extra_route = {"to": "10.40.0.0/16", "via": "192.168.68.1", "table": 100, "metric": 700}
        wifi["routes"].append(extra_route)
        unrelated = {"dhcp4": False, "optional": True, "addresses": ["10.20.0.2/24"], "mtu": 9000}
        document["network"]["ethernets"]["storage0"] = unrelated
        path.write_text(yaml.safe_dump(document))
        self.machine.apply_network_backend(document["network"])
        overlay = self.machine.root / "etc/netplan/99-unmanaged.yaml"
        overlay.write_text("network:\n  ethernets:\n    eth0:\n      accept-ra: true\n  wifis:\n    wlan0:\n      accept-ra: true\n")
        original_overlay = overlay.read_bytes()
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        updated = yaml.safe_load(path.read_text())["network"]
        for name, expected in (("eth0", ethernet), ("storage0", unrelated)):
            actual = updated["ethernets"][name]
            for key in ("dhcp6", "mtu", "routing-policy"):
                if key in expected:
                    self.assertEqual(actual[key], expected[key])
        self.assertFalse(updated["ethernets"]["eth0"]["dhcp4-overrides"]["use-dns"])
        for key in ("dhcp6", "mtu", "routing-policy"):
            self.assertEqual(updated["wifis"]["wlan0"][key], wifi[key])
        self.assertEqual(updated["wifis"]["wlan0"]["nameservers"]["search"], ["home.internal"])
        self.assertIn(extra_route, updated["wifis"]["wlan0"]["routes"])
        self.assertTrue(updated["wifis"]["wlan0"]["routes"][0]["on-link"])
        self.assertEqual(updated["wifis"]["wlan0"]["routes"][0]["metric"], 100)
        self.assertEqual(updated["ethernets"]["storage0"], unrelated)
        self.assertEqual(overlay.read_bytes(), original_overlay)
        self.assertEqual(self.machine.live_network["wifis"]["wlan0"]["dhcp6"], True)
        self.assertEqual(self.machine.live_network["wifis"]["wlan0"]["mtu"], 1400)
        self.assertIn(extra_route, self.machine.live_network["wifis"]["wlan0"]["routes"])
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)

    def test_same_link_alias_migration_preserves_all_unowned_data(self):
        path, _ = self.drift_network()
        document = yaml.safe_load(path.read_text())
        previous = document["network"]["ethernets"].pop("eth0")
        previous.update(match={"name": "e*"}, dhcp6=True, mtu=1450, **{"routing-policy": [{"from": "10.50.0.0/16", "table": 101}]})
        document["network"]["ethernets"]["ethernet"] = previous
        path.write_text(yaml.safe_dump(document))
        self.machine.apply_network_backend(document["network"])
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        updated = yaml.safe_load(path.read_text())["network"]["ethernets"]
        self.assertNotIn("ethernet", updated)
        self.assertNotIn("match", updated["eth0"])
        for key in ("dhcp6", "mtu", "routing-policy"):
            self.assertEqual(updated["eth0"][key], previous[key])
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)

    def test_ambiguous_alias_never_deletes_or_activates_existing_definitions(self):
        path, _ = self.drift_network()
        document = yaml.safe_load(path.read_text())
        previous = document["network"]["ethernets"].pop("eth0")
        previous.update(match={"name": "e*"}, dhcp6=True, mtu=1450)
        document["network"]["ethernets"]["ethernet"] = previous
        path.write_text(yaml.safe_dump(document))
        self.machine.apply_network_backend(document["network"])
        self.machine.extra_link_names = ["enp2s0"]
        original = path.read_bytes()
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 2, records)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(any(args[0] in ("native-netplan-try", "systemd-run") for args, _, _ in self.machine.calls))

    def test_failed_connectivity_confirmation_restores_validated_baseline(self):
        path, original = self.drift_network()
        self.machine.network_try_result = "failure"
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 1, records)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.machine.network_online)
        self.assertFalse(list((self.machine.root / "run").glob("nodes-network-*")))
        operations = [args for args, _, _ in self.machine.calls]
        self.assertTrue(any(args[:2] == ["netplan", "apply"] for args in operations))
        self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args in operations))
        self.assertNotIn("PRIVATE_WIFI_PSK", json.dumps(records))

    def test_immediate_rollback_uses_group_readable_backend_under_private_caller_mask(self):
        path, original = self.drift_network()
        self.machine.network_try_result = "failure"
        tools = Path(self.temporary.name) / "permission-tools"
        tools.mkdir(mode=0o700)
        netplan_permission_tool(tools)
        environment = {"PATH": str(tools), "NODES_NETPLAN_PERMISSION_ROOT": str(self.machine.root)}
        def command(arguments, timeout=20, env=None, umask=-1):
            args = list(arguments)
            if args[:2] != ["netplan", "apply"]:
                return self.machine.command(args, timeout=timeout, env=env, umask=umask)
            self.machine.calls.append((args, timeout, env))
            result = self.real_run_command(args, timeout=timeout, env={**environment, **(env or {})}, umask=umask)
            backend = self.machine.root / "run/systemd/network/reader-probe.network"
            if result.returncode or not stat.S_IMODE(backend.stat().st_mode) & 0o040:
                return NODE.Result(1)
            self.machine.apply_network_backend(yaml.safe_load(path.read_text())["network"])
            self.machine.network_online = True
            return result
        saved_mask = os.umask(0o077)
        try:
            with mock.patch.object(NODE, "run_command", side_effect=command):
                status, records = self.machine.reconcile(activate=True)
            caller_mask = os.umask(0o077)
        finally:
            os.umask(saved_mask)
        self.assertEqual(status, 1, records)
        self.assertEqual(caller_mask, 0o077)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.machine.network_online)
        self.assertFalse(list((self.machine.root / "run").glob("nodes-network-*")))
        self.assertEqual(stat.S_IMODE((self.machine.root / "run/systemd/network/reader-probe.network").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.machine.root / "run/netplan/private-probe.conf").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_independent_rollback_keeps_backend_group_readable_under_private_service_mask(self):
        path, original = self.drift_network()
        self.machine.network_try_result = "recovery-failure"
        self.assertEqual(self.machine.reconcile(activate=True)[0], 1)
        directories = list((self.machine.root / "run").glob("nodes-network-*"))
        self.assertEqual(len(directories), 1)
        tools = Path(self.temporary.name) / "permission-tools"
        tools.mkdir(mode=0o700)
        netplan_permission_tool(tools)
        for name, body in (("flock", "#!/bin/sh\nexit 0\n"), ("timeout", "#!/bin/sh\nshift\nexec \"$@\"\n")):
            program = tools / name
            program.write_text(body)
            program.chmod(0o755)
        for name in ("install", "mv", "rm"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable)
            (tools / name).symlink_to(executable)
        environment = {**os.environ, "PATH": str(tools), "NODES_NETPLAN_PERMISSION_ROOT": str(self.machine.root)}
        completed = subprocess.run(["/bin/sh", str(directories[0] / "rollback.sh")], env=environment, umask=0o077, capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((directories[0] / "pending").exists())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.machine.root / "run/systemd/network/reader-probe.network").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.machine.root / "run/netplan/private-probe.conf").stat().st_mode), 0o600)

    def test_failed_recovery_retains_private_independent_rollback(self):
        _, original = self.drift_network()
        self.machine.network_try_result = "recovery-failure"
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 1, records)
        directories = list((self.machine.root / "run").glob("nodes-network-*"))
        self.assertEqual(len(directories), 1)
        self.assertTrue((directories[0] / "pending").exists())
        self.assertEqual((directories[0] / "previous.yaml").read_bytes(), original)
        self.assertEqual(stat.S_IMODE((directories[0] / "previous.yaml").stat().st_mode), 0o600)
        self.assertTrue(any(name.startswith("nodes-network-rollback-") and value.get("ActiveState") == "active" for name, value in self.machine.units.items()))

    def test_tls_sans_stage_then_activate_once_without_identity_or_ha_change(self):
        arguments = []
        index = 0
        while index < len(self.machine.arguments):
            if self.machine.arguments[index] == "--tls-san":
                index += 2
            else:
                arguments.append(self.machine.arguments[index])
                index += 1
        self.machine.units[self.machine.k3s_unit]["ExecStart"] = self.machine.exec_property(arguments)
        self.machine.write("/proc/22/cmdline", "\0".join(arguments) + "\0")
        self.machine.certificate_sans = ["100.64.0.1"]
        token = (self.machine.root / "etc/systemd/system/k3s.service.env").read_bytes()
        ha = self.machine.root / NODE.HA_PATH.lstrip("/")
        before_ha = (ha.read_bytes(), ha.stat().st_mtime_ns)
        status, records = self.machine.reconcile()
        self.assertEqual(status, 2, records)
        policy = self.machine.root / "etc/rancher/k3s/config.yaml.d/30-node-tls.yaml"
        self.assertEqual(set(yaml.safe_load(policy.read_text())["tls-san"]), {"100.64.0.1", "fraguinha.local", "192.168.68.64"})
        self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args, _, _ in self.machine.calls))
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        self.assertEqual(sum(args[:2] == ["systemctl", "restart"] and args[-1] == "k3s.service" for args, _, _ in self.machine.calls), 1)
        self.assertEqual((self.machine.root / "etc/systemd/system/k3s.service.env").read_bytes(), token)
        self.assertEqual((ha.read_bytes(), ha.stat().st_mtime_ns), before_ha)


class SeedCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.machine = Machine(self.temporary.name)
        self.mount = self.machine.mounted()
        self.mount.__enter__()
        self.addCleanup(self.mount.__exit__, None, None, None)

    def seed(self):
        paths = (
            "/boot/firmware/user-data", "/boot/firmware/network-config",
            "/var/lib/cloud/instance/user-data.txt", "/var/lib/cloud/instance/obj.pkl",
            "/var/lib/cloud/seed/nocloud-test/user-data",
        )
        for path in paths:
            self.machine.write(path, "PRIVATE_BOOTSTRAP_SEED\n", 0o755)
        self.machine.write("/var/lib/cloud/seed/nocloud-test/network-config", "UNOWNED_SEED_NETWORK_CONFIG\n", 0o600)
        self.machine.write("/var/log/syslog", "PRESERVED_LOG_HISTORY\n")
        return [self.machine.root / path.lstrip("/") for path in paths]

    def test_completed_check_flags_seeds_and_apply_removes_only_exact_whitelist(self):
        owned = self.seed()
        self.machine.cidata = True
        self.machine.cidata_target = "/media/cidata"
        for name in ("user-data", "network-config", "meta-data"):
            self.machine.write("/media/cidata/" + name, "PRIVATE_MOUNTED_SEED\n", 0o755)
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 1, records)
        self.assertEqual(self.machine.snapshot(), before)
        status, records = self.machine.reconcile()
        self.assertEqual(status, 2, records)
        self.assertTrue(all(not path.exists() for path in owned))
        self.assertTrue((self.machine.root / "media/cidata/user-data").exists())
        self.assertTrue((self.machine.root / "media/cidata/network-config").exists())
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        self.assertFalse((self.machine.root / "media/cidata/user-data").exists())
        self.assertFalse((self.machine.root / "media/cidata/network-config").exists())
        self.assertTrue((self.machine.root / "media/cidata/meta-data").exists())
        self.assertTrue((self.machine.root / "var/lib/cloud/seed/nocloud-test/network-config").exists())
        self.assertEqual((self.machine.root / "var/log/syslog").read_text(), "PRESERVED_LOG_HISTORY\n")
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)
        self.assertFalse(any(args[0] in ("mount", "umount", "chmod") for args, _, _ in self.machine.calls))

    def test_pending_bootstrap_configuration_and_join_credentials_are_preserved(self):
        self.seed()
        self.machine.write("/etc/nodes/config", "PRIVATE_PENDING_BOOTSTRAP_CREDENTIALS\n", 0o600)
        self.machine.units["k3s.service"].update(ActiveState="activating", SubState="start", MainPID="0")
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 2, records)
        self.assertFalse(any(args[0] in ("mount", "umount", "unshare") for args, _, _ in self.machine.calls))
        self.assertEqual(self.machine.snapshot(), before)
        self.assertTrue(any(record["item"] == "bootstrap-seeds" and record["state"] == "pending" for record in records))

    def test_bootstrap_common_scope_then_canonical_post_success_cleanup(self):
        owned = self.seed()
        self.machine.write("/etc/nodes/config", "PRIVATE_BOOTSTRAP_CREDENTIALS\n", 0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = NODE.main(["--platform", "cloud", "--bootstrap", "--source-root", str(self.machine.source)])
        self.assertEqual(status, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["scope"], "bootstrap-common")
        self.assertTrue(all(path.exists() for path in owned))
        self.assertTrue((self.machine.root / "etc/nodes/config").exists())
        status, records = self.machine.reconcile(bootstrap=True, cleanup_only=True)
        self.assertEqual(status, 0, records)
        self.assertFalse((self.machine.root / "etc/nodes/config").exists())
        self.assertTrue(all(not path.exists() for path in owned))
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_unmounted_media_is_assessed_readonly_without_losing_pending_activation_config(self):
        self.seed()
        self.machine.write("/etc/nodes/config", "PRIVATE_BOOTSTRAP_CREDENTIALS\n", 0o600)
        self.machine.cidata = True
        self.machine.write("/fixture-cidata-content/user-data", "PRIVATE_UNMOUNTED_SEED\n", 0o755)
        status, records = self.machine.reconcile(bootstrap=True, cleanup_only=True)
        self.assertEqual(status, 2, records)
        self.assertTrue((self.machine.root / "etc/nodes/config").exists())
        self.assertTrue((self.machine.media_store / "user-data").exists())
        self.assertIsNone(self.machine.cidata_target)
        self.assertTrue(any(args[0] == "mount" and "ro,nodev,nosuid,noexec,umask=077" in args for args, _, _ in self.machine.calls))
        self.assertTrue(any(args[0] == "umount" for args, _, _ in self.machine.calls))

    def test_unmounted_completed_check_is_readonly_and_activation_removes_only_owned_media_files(self):
        self.machine.cidata = True
        for name in ("user-data", "network-config", "meta-data", "unrelated/keep"):
            self.machine.write("/fixture-cidata-content/" + name, "PRIVATE_MEDIA_" + name + "\n", 0o755)
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 1, records)
        self.assertEqual(self.machine.snapshot(), before)
        mounts = [args for args, _, _ in self.machine.calls if args[0] == "mount"]
        self.assertTrue(mounts)
        self.assertTrue(all("ro,nodev,nosuid,noexec,umask=077" in args for args in mounts))
        namespaces = [args for args, _, _ in self.machine.calls if args[0] == "unshare"]
        self.assertTrue(all(args[1:4] == ["--mount", "--propagation", "private"] for args in namespaces))
        self.assertIsNone(self.machine.cidata_target)
        self.assertFalse(list((self.machine.root / "run").glob("nodes-seed-media-*")))
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        self.assertFalse((self.machine.media_store / "user-data").exists())
        self.assertFalse((self.machine.media_store / "network-config").exists())
        self.assertEqual((self.machine.media_store / "meta-data").read_text(), "PRIVATE_MEDIA_meta-data\n")
        self.assertEqual((self.machine.media_store / "unrelated/keep").read_text(), "PRIVATE_MEDIA_unrelated/keep\n")
        self.assertIsNone(self.machine.cidata_target)
        self.assertFalse(list((self.machine.root / "run").glob("nodes-seed-media-*")))
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)
        self.assertNotIn("PRIVATE_", json.dumps(records))

    def test_private_media_mount_failure_preserves_seeds_and_bootstrap_credentials(self):
        owned = self.seed()
        self.machine.cidata = True
        self.machine.write("/fixture-cidata-content/user-data", "PRIVATE_MEDIA_SEED\n", 0o755)
        self.machine.write("/etc/nodes/config", "PRIVATE_BOOTSTRAP_CREDENTIALS\n", 0o600)
        self.machine.fail = lambda args: args[0] == "mount"
        status, records = self.machine.reconcile(activate=True, bootstrap=True, cleanup_only=True)
        self.assertEqual(status, 1, records)
        self.assertTrue(all(path.exists() for path in owned))
        self.assertTrue((self.machine.media_store / "user-data").exists())
        self.assertTrue((self.machine.root / "etc/nodes/config").exists())
        self.assertTrue(any(args[0] == "umount" for args, _, _ in self.machine.calls))
        self.assertIsNone(self.machine.cidata_target)
        self.assertFalse(list((self.machine.root / "run").glob("nodes-seed-media-*")))
        self.assertNotIn("PRIVATE_", json.dumps(records))

    def test_private_media_unmount_failure_never_claims_complete_or_deletes_config(self):
        self.machine.cidata = True
        self.machine.write("/fixture-cidata-content/user-data", "PRIVATE_MEDIA_SEED\n", 0o755)
        self.machine.write("/etc/nodes/config", "PRIVATE_BOOTSTRAP_CREDENTIALS\n", 0o600)
        self.machine.fail = lambda args: args[0] == "umount"
        status, records = self.machine.reconcile(activate=True, bootstrap=True, cleanup_only=True)
        self.assertEqual(status, 1, records)
        self.assertTrue((self.machine.root / "etc/nodes/config").exists())
        self.assertIsNone(self.machine.cidata_target)
        self.assertFalse(list((self.machine.root / "run").glob("nodes-seed-media-*")))
        self.assertNotIn("PRIVATE_", json.dumps(records))

    def test_multiple_distinct_cidata_devices_refuse_all_seed_and_cache_cleanup(self):
        owned = self.seed()
        self.machine.cidata = True
        self.machine.write("/dev/fixture-cidata-second", "", 0o600)
        self.machine.media_candidates.append("/dev/fixture-cidata-second")
        self.machine.media_device_numbers["/dev/fixture-cidata-second"] = os.makedev(8, 3)
        self.machine.write("/fixture-cidata-content/user-data", "PRIVATE_MEDIA_SEED\n", 0o755)
        self.machine.write("/etc/nodes/config", "PRIVATE_BOOTSTRAP_CREDENTIALS\n", 0o600)
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(activate=True, bootstrap=True, cleanup_only=True)
        self.assertEqual(status, 1, records)
        self.assertEqual(self.machine.snapshot(), before)
        self.assertTrue(all(path.exists() for path in owned))
        self.assertFalse(any(args[0] in ("unshare", "mount", "umount") for args, _, _ in self.machine.calls))
        self.assertNotIn("PRIVATE_", json.dumps(records))

    def test_cidata_aliases_of_one_block_identity_do_not_create_false_ambiguity(self):
        self.machine.cidata = True
        alias = "/dev/fixture-cidata-alias"
        (self.machine.root / alias.lstrip("/")).symlink_to("fixture-cidata")
        self.machine.media_candidates.append(alias)
        self.machine.media_device_numbers[alias] = self.machine.media_device_numbers["/dev/fixture-cidata"]
        self.machine.write("/fixture-cidata-content/user-data", "PRIVATE_MEDIA_SEED\n", 0o755)
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        self.assertFalse((self.machine.media_store / "user-data").exists())
        self.assertTrue((self.machine.root / alias.lstrip("/")).is_symlink())
        self.assertIsNone(self.machine.cidata_target)
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)

    def test_private_media_rejects_changed_singleton_identity_before_mount(self):
        owned = self.seed()
        self.machine.cidata = True
        self.machine.write("/fixture-cidata-content/user-data", "PRIVATE_MEDIA_SEED\n", 0o755)
        self.machine.write("/etc/nodes/config", "PRIVATE_BOOTSTRAP_CREDENTIALS\n", 0o600)
        def replace_device(args):
            if args[0] == "unshare":
                self.machine.media_device_numbers["/dev/fixture-cidata"] = os.makedev(8, 3)
            return False
        self.machine.fail = replace_device
        status, records = self.machine.reconcile(activate=True, bootstrap=True, cleanup_only=True)
        self.assertEqual(status, 1, records)
        self.assertTrue(all(path.exists() for path in owned))
        self.assertTrue((self.machine.media_store / "user-data").exists())
        self.assertTrue((self.machine.root / "etc/nodes/config").exists())
        self.assertFalse(any(args[0] in ("mount", "umount") for args, _, _ in self.machine.calls))
        self.assertFalse(list((self.machine.root / "run").glob("nodes-seed-media-*")))
        self.assertNotIn("PRIVATE_", json.dumps(records))

    def test_private_media_rechecks_readiness_before_mounting_or_deleting_any_seeds(self):
        owned = self.seed()
        self.machine.cidata = True
        self.machine.write("/fixture-cidata-content/user-data", "PRIVATE_MEDIA_SEED\n", 0o755)
        before = self.machine.snapshot()
        def lose_readiness(args):
            if args[0] == "unshare":
                self.machine.ready = False
            return False
        self.machine.fail = lose_readiness
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 2, records)
        self.assertTrue(all(path.exists() for path in owned))
        self.assertEqual(self.machine.snapshot(), before)
        self.assertFalse(any(args[0] in ("mount", "umount") for args, _, _ in self.machine.calls))
        self.assertFalse(list((self.machine.root / "run").glob("nodes-seed-media-*")))

    def test_failed_seed_removal_cannot_claim_complete_or_delete_config(self):
        owned = self.seed()
        self.machine.write("/etc/nodes/config", "PRIVATE_BOOTSTRAP_CREDENTIALS\n", 0o600)
        original_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == owned[0]:
                raise PermissionError()
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", unlink):
            status, records = self.machine.reconcile(bootstrap=True, cleanup_only=True)
        self.assertEqual(status, 1, records)
        self.assertTrue(owned[0].exists())
        self.assertTrue((self.machine.root / "etc/nodes/config").exists())
        self.assertNotIn("PRIVATE_", json.dumps(records))


class NativeProducerCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.machine = Machine(self.temporary.name, platform="physical", role="agent", hostname="carolion")
        previous = self.machine.root / self.machine.network_file
        self.machine.network_file = "etc/netplan/50-network.yaml"
        previous.rename(self.machine.root / self.machine.network_file)
        self.machine.write("/proc/22/cmdline", "/usr/local/bin/k3s agent\0")
        self.mount = self.machine.mounted()
        self.mount.__enter__()
        self.addCleanup(self.mount.__exit__, None, None, None)

    def test_native_producer_and_agent_title_are_read_only_compatible(self):
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(check=True)
        self.assertEqual(status, 0, records)
        self.assertEqual(self.machine.snapshot(), before)
        self.assertFalse((self.machine.root / "etc/netplan/50-cloud-init.yaml").exists())
        self.assertTrue(any(record["item"] == "bootstrap-seeds" and record["state"] == "match" for record in records))

    def test_activation_updates_existing_producer_without_duplicate_credentials(self):
        path = self.machine.root / self.machine.network_file
        document = yaml.safe_load(path.read_text())
        original_access = document["network"]["wifis"]["wlan0"]["access-points"]
        original_addresses = document["network"]["wifis"]["wlan0"]["addresses"]
        document["network"]["wifis"]["wlan0"]["routes"][0]["metric"] = 999
        path.write_text(yaml.safe_dump(document))
        self.machine.apply_network_backend(document["network"])
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 0, records)
        updated = yaml.safe_load(path.read_text())
        self.assertEqual(updated["network"]["wifis"]["wlan0"]["access-points"], original_access)
        self.assertEqual(updated["network"]["wifis"]["wlan0"]["addresses"], original_addresses)
        self.assertEqual(updated["network"]["wifis"]["wlan0"]["routes"][0]["metric"], 100)
        self.assertEqual(sorted(value.name for value in path.parent.glob("*.yaml")), ["50-network.yaml"])
        self.assertEqual(self.machine.reconcile(check=True)[0], 0)

    def test_multiple_credential_producers_are_not_guessed_or_rewritten(self):
        path = self.machine.root / self.machine.network_file
        self.machine.write("/etc/netplan/60-other.yaml", path.read_text(), 0o600)
        before = self.machine.snapshot()
        status, records = self.machine.reconcile(activate=True)
        self.assertEqual(status, 2, records)
        self.assertEqual(self.machine.snapshot(), before)
        self.assertFalse(any(args[0] in ("native-netplan-try", "systemd-run") for args, _, _ in self.machine.calls))


class AgentNodeConfigTest(unittest.TestCase):
    def test_agent_removes_only_recognized_managed_ha_and_preserves_role(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = Machine(directory, role="agent")
            machine.write(NODE.HA_PATH, (machine.source / NODE.HA_TEMPLATE.lstrip("/")).read_text())
            with machine.mounted():
                status, records = machine.reconcile()
                self.assertEqual(status, 2, records)
                self.assertFalse((machine.root / NODE.HA_PATH.lstrip("/")).exists())
                machine.write(NODE.HA_PATH, "custom: private\n")
                status, records = machine.reconcile()
                self.assertEqual(status, 1, records)
                self.assertTrue((machine.root / NODE.HA_PATH.lstrip("/")).exists())
                self.assertFalse(any(args[:2] in (["systemctl", "start"], ["systemctl", "restart"]) and "k3s" in args[-1] for args, _, _ in machine.calls))


class RenderPolicyTest(unittest.TestCase):
    def test_boot_consumers_share_policy_and_native_password_semantics(self):
        if not shutil.which("jq"):
            self.skipTest("jq is explicitly required by the renderer")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            shutil.copytree(PROJECT / "lib", project / "lib")
            shutil.copytree(PROJECT / "cloud-init", project / "cloud-init")
            shutil.copytree(PROJECT / "rootfs", project / "rootfs")
            shutil.copytree(PROJECT / "config", project / "config")
            policy_path = project / "rootfs/common" / NODE.POLICY_PATH.lstrip("/")
            policy = json.loads(policy_path.read_text())
            policy["timezone"] = "Etc/UTC"
            policy["apt"]["conf"]["Acquire::Retries"] = "7"
            policy["network"]["wifi"]["route_metric"] = 111
            policy_path.write_text(json.dumps(policy))
            user_data, network_data = Path(directory) / "user-data", Path(directory) / "network-data"
            subprocess.run(["/bin/bash", "-c", 'set -euo pipefail; source "$1/lib/render.sh"; render_user_data "$2" node-a join physical server-a TEST_AUTH ""; render_network_config "$3" "$4" "$5" node-a', "bash", str(project), str(user_data), str(network_data), 'SSID with "quotes"', "private-wifi-key"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            boot = yaml.safe_load(user_data.read_text())
            network = yaml.safe_load(network_data.read_text())
            self.assertEqual(boot["timezone"], "Etc/UTC")
            self.assertIn('Acquire::Retries "7";', boot["apt"]["conf"])
            self.assertEqual(boot["chpasswd"], {"expire": False, "users": [{"name": "root", "password": "!", "type": "hash"}]})
            self.assertEqual(boot["packages"], policy["packages"]["common"] + policy["packages"]["physical"])
            self.assertEqual(boot["users"][0]["groups"], policy["users"]["pi"]["groups"])
            keys = [line for line in (project / "config/authorized_keys").read_text().splitlines() if line]
            self.assertEqual(boot["users"][0]["ssh_authorized_keys"], keys)
            self.assertEqual(network["network"]["wifis"]["wlan0"]["dhcp4-overrides"]["route-metric"], 111)
            self.assertEqual(network["network"]["ethernets"]["ethernet"]["dhcp4-overrides"]["route-metric"], 200)
            self.assertNotIn("TEST_AUTH", str(network))
            subprocess.run(["/bin/bash", "-c", 'set -euo pipefail; source "$1/lib/render.sh"; render_network_config "$2" "$3" "$4" fraguinha', "bash", str(project), str(network_data), 'HISTORICAL "SSID"', "private-wifi-key"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            static = yaml.safe_load(network_data.read_text())["network"]
            self.assertFalse(static["wifis"]["wlan0"]["dhcp4"])
            self.assertEqual(static["wifis"]["wlan0"]["addresses"], ["192.168.68.64/22"])
            self.assertEqual(static["ethernets"]["eth0"]["dhcp4-overrides"]["route-metric"], 200)


if __name__ == "__main__":
    unittest.main()
