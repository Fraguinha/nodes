import ast
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

PROJECT = Path(__file__).resolve().parents[1]
POLICY = "rootfs/common/usr/local/share/nodes/node-policy.json"
SECRETS = {
    "tailscale/authkey": "tskey-sync-secret",
    "wifi/ssid": "HomeNetwork",
    "wifi/password": "wifi-secret-password",
}
INVENTORY = {
    "srv-a": {"platform": "physical", "role": "init"},
    "phys-wifi": {"platform": "physical", "role": "join", "server": "srv-a"},
    "phys-wired": {"platform": "physical", "role": "agent", "server": "srv-a"},
    "cloud-a": {"platform": "cloud", "role": "join", "server": "srv-a"},
}

FAKE_SSH = r'''
import json
import os
import sys

state_path = os.environ["SYNC_FAKE_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
index = 0
while index < len(args) and args[index].startswith("-"):
    index += 2 if args[index] in ("-o", "-F") else 1
destination = args[index]
command = " ".join(args[index + 1:])
host = destination.split("@", 1)[1]
entry = {"host": host, "argv": args}
if command == "sudo -n python3 -":
    entry["stdin"] = sys.stdin.read()
state["calls"].append(entry)

def respond(name):
    queue = state["responses"].setdefault(host, {}).setdefault(name, [])
    value = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else None)
    return value

status = 0
output = ""
if "boot_id" in command:
    value = respond("boot")
    if value is None:
        value = "boot-0" if not any(call["host"] == host and "boot_id" in " ".join(call["argv"]) for call in state["calls"][:-1]) else "boot-1"
    if value == "down":
        status = 255
    else:
        output = value + "\n"
elif command == "sudo -n python3 -":
    pass
elif "cloud-init status" in command:
    value = respond("cloud_init") or "0"
    if value == "down":
        status = 255
    else:
        output = value + "\n"
elif "cloud-init-output.log" in command:
    output = "fake cloud-init output tail\n"
elif "kubectl get node" in command:
    output = respond("ready") or "True"
else:
    status = 99
with open(state_path, "w") as handle:
    json.dump(state, handle)
sys.stdout.write(output)
sys.exit(status)
'''

FAKE_PASS = r'''
import json
import os
import sys

with open(os.environ["SYNC_FAKE_PASS_LOG"], "a") as handle:
    handle.write(sys.argv[-1] + "\n")
secrets = json.loads(os.environ["SYNC_FAKE_SECRETS"])
if sys.argv[1:2] != ["show"] or sys.argv[-1] not in secrets:
    sys.exit(1)
print(secrets[sys.argv[-1]])
'''


def seed_files(script):
    files = ast.literal_eval(ast.parse(script).body[0].value)
    return {name: base64.b64decode(data).decode() for name, data in files.items()}


class SyncTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        for name in ("lib", "cloud-init", "rootfs", "config"):
            shutil.copytree(PROJECT / name, self.project / name)
        shutil.copy2(PROJECT / "nodes", self.project / "nodes")
        (self.project / "config/nodes.json").write_text(json.dumps(INVENTORY))
        policy_path = self.project / POLICY
        policy = json.loads(policy_path.read_text())
        policy["network"]["profiles"]["phys-wired"] = {"ethernets": {"eth0": {"dhcp4": True, "optional": False}}}
        policy_path.write_text(json.dumps(policy))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, source in (("ssh", FAKE_SSH), ("pass", FAKE_PASS)):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + source)
            path.chmod(0o755)
        sleep = self.bin / "sleep"
        sleep.write_text("#!" + sys.executable + "\n")
        sleep.chmod(0o755)
        self.state = self.root / "state.json"
        self.pass_log = self.root / "pass.log"
        self.pass_log.write_text("")
        self.write_state({})

    def write_state(self, responses):
        self.state.write_text(json.dumps({"calls": [], "responses": responses}))

    def read_state(self):
        return json.loads(self.state.read_text())

    def sync(self, *args):
        environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "SYNC_FAKE_STATE": str(self.state),
            "SYNC_FAKE_PASS_LOG": str(self.pass_log),
            "SYNC_FAKE_SECRETS": json.dumps(SECRETS),
        }
        return subprocess.run([str(self.project / "nodes"), "sync", *args], env=environment,
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def installs(self):
        return {call["host"]: call["stdin"] for call in self.read_state()["calls"] if "stdin" in call}

    def test_cloud_and_wired_nodes_sync_without_wifi_credentials(self):
        self.write_state({"cloud-a": {"boot": ["boot-0", "down", "boot-0", "boot-1"], "cloud_init": ["down", "rebooting", "0"]}})
        result = self.sync("--yes", "cloud-a", "phys-wired")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pass_log.read_text().split(), ["tailscale/authkey"])
        state = self.read_state()
        hosts = [call["host"] for call in state["calls"]]
        first_wired = hosts.index("phys-wired")
        self.assertNotIn("phys-wired", hosts[:first_wired])
        self.assertNotIn("cloud-a", hosts[first_wired:])
        self.assertEqual(hosts[-1], "srv-a")
        self.assertIn("kubectl get node cloud-a", " ".join(next(call["argv"] for call in state["calls"] if call["host"] == "cloud-a" and "kubectl" in " ".join(call["argv"]))))
        self.assertIn("kubectl get node phys-wired", " ".join(state["calls"][-1]["argv"]))

        installs = self.installs()
        cloud = seed_files(installs["cloud-a"])
        wired = seed_files(installs["phys-wired"])
        self.assertEqual(set(cloud), {"meta-data", "user-data"})
        self.assertEqual(set(wired), {"meta-data", "user-data", "network-config"})
        for hostname, files in (("cloud-a", cloud), ("phys-wired", wired)):
            meta = yaml.safe_load(files["meta-data"])
            self.assertEqual(meta["local-hostname"], hostname)
            self.assertRegex(meta["instance-id"], "^" + hostname + r"-[0-9]{8}T[0-9]{6}Z$")
            user_data = yaml.safe_load(files["user-data"])
            self.assertIs(user_data["ssh_deletekeys"], False)
            self.assertIs(user_data["package_upgrade"], False)
            self.assertIs(user_data["apt"]["preserve_sources_list"], True)
            entries = {entry["path"]: entry for entry in user_data["write_files"]}
            config = yaml.safe_load(base64.b64decode(entries["/etc/cloud/cloud.cfg.d/99-nodes.cfg"]["content"]))
            self.assertEqual(config.get("network"), {"config": "disabled"} if hostname == "cloud-a" else None)
            self.assertIn("TAILSCALE_AUTHKEY=tskey-sync-secret", base64.b64decode(entries["/etc/nodes/config"]["content"]).decode())
        self.assertEqual(yaml.safe_load(wired["network-config"])["network"], {"version": 2, "ethernets": {"eth0": {"dhcp4": True, "optional": False}}})

        for script in installs.values():
            self.assert_installer_behavior(script)
            self.assertNotIn("--seed", script)
            self.assertNotIn("tskey-sync-secret", script)
            self.assertIsNotNone(compile(script, "<seed>", "exec"))
        for call in state["calls"]:
            self.assertFalse(any("tskey" in arg or "secret" in arg for arg in call["argv"]))
        self.assertNotIn("tskey-sync-secret", result.stdout + result.stderr)
        self.assertEqual(list(self.root.glob("nodes-sync.*")), [])

    def assert_installer_behavior(self, script):
        namespace = {"__name__": "seed_test"}
        exec(compile(script, "<seed>", "exec"), namespace)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace["ROOT"] = root
            seed = root / "var/lib/cloud/seed/nocloud"
            seed.mkdir(parents=True)
            seed.chmod(0o755)
            (seed / "network-config").write_text("stale")
            boot = root / "boot/firmware"
            boot.mkdir(parents=True)
            for name in ("meta-data", "user-data", "network-config"):
                (boot / name).write_text("boot " + name)
            disabled = root / "etc/cloud/cloud-init.disabled"
            disabled.parent.mkdir(parents=True)
            disabled.touch()
            calls, unmounted = [], []
            def command(arguments, **kwargs):
                calls.append(arguments)
                if arguments[0] == "blkid":
                    return SimpleNamespace(returncode=0, stdout="/dev/fake-cidata\n")
                if arguments[0] == "mount":
                    media = Path(arguments[-1])
                    for name in ("meta-data", "user-data", "network-config"):
                        (media / name).write_text("cidata " + name)
                elif arguments[0] == "umount":
                    media = Path(arguments[-1])
                    unmounted.append({path.name: path.read_text() for path in media.iterdir()})
                    for path in media.iterdir():
                        path.unlink()
                elif arguments[0] == "cloud-init":
                    self.assertFalse(disabled.exists())
                return SimpleNamespace(returncode=0)
            namespace["command"] = command
            original_umask = os.umask(0o022)
            try:
                with mock.patch("os.chown") as chown:
                    namespace["install_seed"](namespace["FILES"])
                    chown.assert_called_once_with(seed, 0, 0)
                applied_umask = os.umask(original_umask)
                self.assertEqual(applied_umask, 0o077)
            finally:
                os.umask(original_umask)
            files = seed_files(script)
            self.assertEqual({path.name: path.read_text() for path in seed.iterdir()}, files)
            self.assertEqual(seed.stat().st_mode & 0o777, 0o700)
            for name in files:
                self.assertEqual((seed / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(path.name for path in boot.iterdir()), ["meta-data"])
            self.assertEqual(unmounted, [{"meta-data": "cidata meta-data"}])
            self.assertFalse(disabled.exists())
            self.assertEqual(calls[0], ["blkid", "-t", "LABEL=CIDATA", "-o", "device"])
            self.assertEqual(calls[1][:5], ["mount", "-t", "vfat", "-o", "nodev,nosuid,noexec"])
            self.assertEqual(calls[-2:], [
                ["cloud-init", "clean", "--logs"],
                ["systemd-run", "--quiet", "--on-active=5s", "/usr/bin/systemctl", "reboot"],
            ])

    def test_physical_wifi_node_gets_credentials_in_network_config(self):
        result = self.sync("--yes", "phys-wifi")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pass_log.read_text().split(), ["tailscale/authkey", "wifi/ssid", "wifi/password"])
        network = yaml.safe_load(seed_files(self.installs()["phys-wifi"])["network-config"])["network"]
        self.assertEqual(network["wifis"]["wlan0"]["access-points"], {"HomeNetwork": {"auth": {"key-management": "psk", "password": "wifi-secret-password"}}})
        self.assertNotIn("wifi-secret-password", result.stdout + result.stderr)
        ready = [call for call in self.read_state()["calls"] if "kubectl" in " ".join(call["argv"])]
        self.assertEqual({call["host"] for call in ready}, {"phys-wifi"})

    def test_cloud_init_failure_shows_log_and_stops_before_next_node(self):
        self.write_state({"cloud-a": {"cloud_init": ["1"]}})
        result = self.sync("--yes", "cloud-a", "phys-wired")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fake cloud-init output tail", result.stderr)
        self.assertIn("cloud-init failed on cloud-a", result.stderr)
        self.assertNotIn("phys-wired", [call["host"] for call in self.read_state()["calls"]])

    def test_recoverable_cloud_init_errors_warn_and_continue(self):
        self.write_state({"cloud-a": {"cloud_init": ["2"]}})
        result = self.sync("--yes", "cloud-a", "phys-wired")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Warning: cloud-init on cloud-a finished with recoverable errors", result.stderr)
        self.assertIn("phys-wired is Ready.", result.stdout)

    def test_unknown_or_duplicate_hosts_fail_before_contacting_nodes(self):
        for args in (("--yes", "ghost"), ("--yes", "cloud-a", "cloud-a"), ("--platform", "cloud", "cloud-a"), ("--health-only", "cloud-a")):
            with self.subTest(args=args):
                result = self.sync(*args)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_state()["calls"], [])

    def test_confirmation_lists_hosts_and_can_abort(self):
        result = self.sync("cloud-a", "phys-wired")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rebooted, one at a time", result.stdout)
        self.assertIn("cloud-a (cloud, join)", result.stdout)
        self.assertIn("phys-wired (physical, agent)", result.stdout)
        self.assertEqual(self.read_state()["calls"], [])


if __name__ == "__main__":
    unittest.main()
