import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

from test_node_config import Machine, NODE, PROJECT


class NvidiaMachine(Machine):
    running = "7.0.0-34-generic"
    newest = "7.0.0-44-generic"
    address = "0000:01:00.0"
    runtime_config = "/var/lib/rancher/k3s/agent/etc/containerd/config.toml"
    runtime_content = "[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.'nvidia']\n  runtime_type = 'io.containerd.runc.v2'\n"

    def __init__(self, directory, installed=True, driver="nvidia", registered=True, role="agent"):
        super().__init__(directory, platform="physical", role=role, hostname="carolion")
        self.gpu = self.root / "sys/bus/pci/devices" / self.address
        self.write("/sys/bus/pci/devices/" + self.address + "/vendor", "0x10de\n")
        self.write("/sys/bus/pci/devices/" + self.address + "/class", "0x030000\n")
        self.write("/sys/bus/pci/devices/" + self.address + "/driver_override", "(null)\n")
        for name in ("nouveau", "nvidia"):
            self.write("/sys/bus/pci/drivers/" + name + "/unbind", "")
        if driver:
            (self.gpu / "driver").symlink_to(self.root / "sys/bus/pci/drivers" / driver)
        if driver == "nouveau":
            (self.root / "sys/module/nouveau").mkdir(parents=True)
        self.write("/proc/sys/kernel/osrelease", self.running + "\n")
        for version in (self.running, "7.0.0-43-generic", self.newest):
            self.write("/boot/vmlinuz-" + version, "kernel-image\n")
        self.default_routes_override = [{"dev": "wlan0", "metric": 100, "protocol": "static", "gateway": "192.168.68.1"}]
        module = self.root / "sys/class/net/wlan0/device/driver/module"
        module.parent.mkdir(parents=True)
        module.symlink_to("../../../../../../module/rtw89_8851bu")
        self.nvidia_packages = self.policy["packages"]["nvidia"] + ["linux-modules-nvidia-580-server-" + self.running]
        self.module_missing = False
        self.register_on_restart = True
        self.gpu_events = []
        if installed:
            self.installed.update(self.nvidia_packages)
            self.write("/usr/bin/nvidia-container-runtime", "runtime-binary\n", 0o755)
        if registered:
            self.write(self.runtime_config, self.runtime_content)
        self.write(NODE.NVIDIA_SOURCE, NODE.NVIDIA_SOURCE_CONTENT)
        self.write(NODE.NVIDIA_RULES, (self.source / NODE.NVIDIA_RULES_TEMPLATE.lstrip("/")).read_text())
        for path in self.root.rglob("*"):
            if not path.is_symlink():
                os.utime(path, (100, 100))

    def command(self, arguments, timeout=20, env=None, umask=-1):
        args = list(arguments)
        if args[0] in ("rmmod", "modprobe", "modinfo", "update-initramfs") or args[:3] == ["udevadm", "control", "--reload-rules"] or args[:2] == ["systemctl", "restart"] and args[-1].startswith("nvidia-"):
            self.calls.append((args, timeout, env))
            self.gpu_events.append(tuple(args))
            if self.fail and self.fail(args):
                return NODE.Result(1)
            if args[0] == "rmmod":
                shutil.rmtree(self.root / "sys/module/nouveau")
            if args == ["modprobe", "nvidia"]:
                driver = self.gpu / "driver"
                if driver.is_symlink():
                    driver.unlink()
                driver.symlink_to(self.root / "sys/bus/pci/drivers/nvidia")
            return NODE.Result(1 if args[0] == "modinfo" and self.module_missing else 0)
        restarting = args[:2] == ["systemctl", "restart"] and args[-1] == self.k3s_unit
        if restarting:
            process = (self.root / "proc/22/stat").read_text()
            ticks = int(process[process.rfind(")") + 2:].split()[19]) + 1000
        result = super().command(args, timeout, env, umask)
        if args[0] == "apt-get" and "install" in args and "nvidia-container-toolkit" in self.installed:
            self.write("/usr/bin/nvidia-container-runtime", "runtime-binary\n", 0o755)
        if restarting:
            self.write("/proc/22/stat", "22 (k3s) S " + "0 " * 18 + str(ticks) + " 0\n")
            if self.register_on_restart:
                self.write(self.runtime_config, self.runtime_content)
        return result


class NvidiaNodeConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def machine(self, **kwargs):
        return NvidiaMachine(self.temporary.name, **kwargs)

    def test_detection_uses_injected_sysfs_and_only_nvidia_display_classes(self):
        root = Path(self.temporary.name)
        pci = root / "sys/bus/pci/devices"
        helper = NODE.Reconciler("physical", check=True)
        with mock.patch.object(NODE, "ROOT", root):
            self.assertEqual(helper.nvidia_devices(), [])
            for address, vendor, device_class in (
                ("0000:01:00.0", "0x10de", "0x030000"),
                ("0000:01:00.1", "0x10de", "0x040300"),
                ("0000:02:00.0", "0x1002", "0x030000"),
                ("0000:03:00.0", "0x10de", "0x030200"),
            ):
                device = pci / address
                device.mkdir(parents=True)
                (device / "vendor").write_text(vendor + "\n")
                (device / "class").write_text(device_class + "\n")
            self.assertEqual([device.name for device in helper.nvidia_devices()], ["0000:01:00.0", "0000:03:00.0"])

    def test_no_gpu_preserves_existing_records_files_and_commands(self):
        for platform in ("physical", "cloud"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                machine = Machine(directory, platform=platform)
                with machine.mounted():
                    before = machine.snapshot()
                    status, records = machine.reconcile(check=True)
                    self.assertEqual(status, 0, records)
                    self.assertFalse(any(record["item"].startswith("nvidia") for record in records))
                    check_calls = list(machine.calls)
                    machine.calls.clear()
                    with mock.patch.object(NODE.Reconciler, "nvidia"):
                        old_status, old_records = machine.reconcile(check=True)
                    self.assertEqual((old_status, old_records), (status, records))
                    self.assertEqual(machine.calls, check_calls)
                    status, records = machine.reconcile()
                    self.assertEqual(status, 0, records)
                    self.assertEqual(machine.snapshot(), before)
                    self.assertFalse((machine.root / NODE.NVIDIA_SOURCE.lstrip("/")).exists())
                    self.assertFalse((machine.root / NODE.NVIDIA_RULES.lstrip("/")).exists())
                    self.assertFalse(any(args[0] in ("apt-get", "modprobe", "rmmod", "modinfo", "update-initramfs") for args, _, _ in machine.calls))

    def test_carolion_state_checks_without_drift_or_mutation(self):
        machine = self.machine()
        with machine.mounted():
            before = machine.snapshot()
            status, records = machine.reconcile(check=True)
            self.assertEqual(status, 0, records)
            self.assertEqual(machine.snapshot(), before)
            self.assertEqual({record["item"] for record in records if record["item"].startswith("nvidia")}, {"nvidia-repository", "nvidia-udev", "nvidia-packages", "nvidia-kernel-network", "nvidia-driver"})
            self.assertFalse(any(args[0] in ("apt-get", "modprobe", "rmmod", "nvidia-modprobe", "update-initramfs") or args[:2] == ["systemctl", "restart"] for args, _, _ in machine.calls))
            self.assertEqual([args for args, _, _ in machine.calls if args[0] == "modinfo"], [["modinfo", "-k", machine.newest, "rtw89_8851bu"]])

    def test_first_install_check_is_readonly_and_apply_uses_exact_packages(self):
        machine = self.machine(installed=False, driver="nouveau", registered=False)
        for path in (NODE.NVIDIA_SOURCE, NODE.NVIDIA_RULES):
            (machine.root / path.lstrip("/")).unlink()
        with machine.mounted():
            before = machine.snapshot()
            status, records = machine.reconcile(check=True)
            self.assertEqual(status, 1, records)
            self.assertEqual(machine.snapshot(), before)
            self.assertFalse(any(args[0] in ("apt-get", "modprobe", "rmmod", "nvidia-modprobe", "update-initramfs") or args[:2] == ["systemctl", "restart"] for args, _, _ in machine.calls))
            status, records = machine.reconcile(activate=True)
            self.assertEqual(status, 0, records)
            installs = [(args, timeout, env) for args, timeout, env in machine.calls if args[0] == "apt-get" and "install" in args]
            self.assertEqual(len(installs), 1)
            args, timeout, env = installs[0]
            self.assertEqual(args[args.index("--no-remove") + 1:], machine.nvidia_packages)
            self.assertIn("--no-upgrade", args)
            self.assertIn("Dpkg::Options::=--force-confold", args)
            self.assertIn("DPkg::Lock::Timeout=30", args)
            self.assertIn("Acquire::http::Timeout=15", args)
            self.assertIn("Acquire::https::Timeout=15", args)
            self.assertEqual(timeout, 900)
            self.assertEqual(env, {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"})
            self.assertEqual(sum(args[0] == "apt-get" and "update" in args for args, _, _ in machine.calls), 1)
            self.assertEqual(sum(args == ["update-initramfs", "-u", "-k", "all"] for args, _, _ in machine.calls), 1)
            self.assertEqual((machine.root / NODE.NVIDIA_SOURCE.lstrip("/")).read_bytes(), b"Types: deb\nURIs: https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH)\nSuites: /\nSigned-By: /usr/local/share/nodes/nvidia-container-toolkit.asc\n")
            self.assertFalse((machine.root / "etc/modprobe.d/nvidia.conf").exists())
            before = machine.snapshot()
            self.assertEqual(machine.reconcile(check=True)[0], 0)
            self.assertEqual(machine.snapshot(), before)

    def test_new_source_refreshes_apt_even_with_all_packages_installed(self):
        machine = self.machine()
        (machine.root / NODE.NVIDIA_SOURCE.lstrip("/")).unlink()
        with machine.mounted():
            status, records = machine.reconcile()
            self.assertEqual(status, 0, records)
            self.assertEqual(sum(args[0] == "apt-get" and "update" in args for args, _, _ in machine.calls), 1)
            self.assertFalse(any(args[0] == "apt-get" and "install" in args or args[0] == "update-initramfs" for args, _, _ in machine.calls))

    def test_binding_overrides_unbinds_rmmods_then_loads_both_modules(self):
        machine = self.machine(driver="nouveau")
        original_write = Path.write_text
        def write(path, content, *args, **kwargs):
            if path == machine.gpu / "driver_override":
                machine.gpu_events.append(("driver_override", content))
            if path == machine.gpu / "driver/unbind":
                machine.gpu_events.append(("unbind", content))
                result = original_write(path, content, *args, **kwargs)
                (machine.gpu / "driver").unlink()
                return result
            return original_write(path, content, *args, **kwargs)
        with machine.mounted(), mock.patch.object(Path, "write_text", write):
            status, records = machine.reconcile()
            self.assertEqual(status, 0, records)
            events = machine.gpu_events
            expected = [("driver_override", "nvidia\n"), ("unbind", machine.address + "\n"), ("rmmod", "nouveau"), ("modprobe", "nvidia"), ("modprobe", "nvidia_uvm")]
            relevant = [event for event in events if event[0] in ("driver_override", "unbind", "rmmod", "modprobe")]
            self.assertEqual(relevant, expected)
            self.assertLess(events.index(("udevadm", "control", "--reload-rules")), events.index(expected[0]))
            self.assertFalse(any(args[:2] == ["modprobe", "-r"] for args, _, _ in machine.calls))
            self.assertEqual(Path(os.readlink(machine.gpu / "driver")).name, "nvidia")

    def test_unbound_device_does_not_require_nouveau_unload(self):
        machine = self.machine(driver=None)
        with machine.mounted():
            status, records = machine.reconcile()
            self.assertEqual(status, 0, records)
            self.assertFalse(any(args[0] == "rmmod" for args, _, _ in machine.calls))
            self.assertIn((["modprobe", "nvidia_uvm"], 20, None), machine.calls)

    def test_udev_override_is_gpu_only_preserves_vendor_rules_and_reloads_before_binding(self):
        machine = self.machine()
        path = machine.root / NODE.NVIDIA_RULES.lstrip("/")
        path.unlink()
        expected = (machine.source / NODE.NVIDIA_RULES_TEMPLATE.lstrip("/")).read_bytes()
        self.assertFalse(any(line.startswith(b"#") for line in expected.splitlines()))
        removal = [line for line in expected.decode().splitlines() if 'ACTION=="remove"' in line]
        self.assertEqual([line.split('RUN+="')[1].rstrip('"') for line in removal], ["/sbin/rmmod nvidia_modeset", "/sbin/rmmod nvidia_drm", "/sbin/rmmod nvidia_uvm"])
        self.assertIn(b'ATTR{class}=="0x03[0-9]*"', expected)
        self.assertIn(b'ENV{SYSTEMD_WANTS}="nvidia-persistenced.service"', expected)
        with machine.mounted():
            status, records = machine.reconcile()
            self.assertEqual(status, 0, records)
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            events = machine.gpu_events
            self.assertLess(events.index(("udevadm", "control", "--reload-rules")), events.index(("modprobe", "nvidia")))
            self.assertFalse(any(args[:2] == ["udevadm", "settle"] for args, _, _ in machine.calls))
            self.assertIn(("systemctl", "restart", "nvidia-cdi-refresh.service"), events)

    def test_runtime_registration_uses_existing_guarded_restart_for_agent_and_server(self):
        for role in ("agent", "server"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                machine = NvidiaMachine(directory, role=role, registered=False)
                with machine.mounted():
                    status, records = machine.reconcile(check=True)
                    self.assertEqual(status, 2, records)
                    self.assertTrue(any(record["item"] == "k3s-runtime" and "NVIDIA" in record.get("reason", "") for record in records))
                    self.assertEqual(machine.reconcile()[0], 2)
                    self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args, _, _ in machine.calls))
                    status, records = machine.reconcile(activate=True)
                    self.assertEqual(status, 0, records)
                    self.assertEqual([args for args, _, _ in machine.calls if args[:2] == ["systemctl", "restart"]], [["systemctl", "restart", machine.k3s_unit]])
                    self.assertEqual(machine.reconcile(check=True)[0], 0)
                    self.assertEqual(machine.reconcile(activate=True)[0], 0)
                    self.assertEqual(sum(args[:2] == ["systemctl", "restart"] for args, _, _ in machine.calls), 1)

    def test_runtime_registration_never_restarts_when_node_is_not_ready(self):
        machine = self.machine(registered=False)
        machine.ready = False
        with machine.mounted():
            status, records = machine.reconcile(activate=True)
            self.assertEqual(status, 2, records)
            self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args, _, _ in machine.calls))

    def test_missing_default_route_module_fails_loudly_and_blocks_k3s_restart(self):
        machine = self.machine(registered=False)
        machine.module_missing = True
        with machine.mounted():
            for options in ({"check": True}, {"activate": True}):
                with self.subTest(options=options):
                    before = machine.snapshot()
                    status, records = machine.reconcile(**options)
                    self.assertEqual(status, 1, records)
                    self.assertTrue(any(record["item"] == "nvidia-kernel-network" and record["state"] == "drift" and "rtw89_8851bu" in record.get("reason", "") and machine.newest in record.get("reason", "") and "reboot" in record.get("reason", "") for record in records))
                    self.assertEqual(machine.snapshot(), before)
                    self.assertFalse(any(args[:2] == ["systemctl", "restart"] for args, _, _ in machine.calls))

    def test_running_extra_modules_require_extras_for_every_newer_kernel(self):
        machine = self.machine()
        machine.installed.add("linux-modules-extra-" + machine.running)
        with machine.mounted():
            before = machine.snapshot()
            status, records = machine.reconcile(check=True)
            self.assertEqual(status, 1, records)
            self.assertEqual(machine.snapshot(), before)
            status, records = machine.reconcile()
            self.assertEqual(status, 0, records)
            installs = [args for args, _, _ in machine.calls if args[0] == "apt-get" and "install" in args]
            self.assertEqual(len(installs), 1)
            self.assertEqual(set(installs[0][installs[0].index("--no-remove") + 1:]), {"linux-modules-extra-7.0.0-43-generic", "linux-modules-extra-" + machine.newest})
            self.assertEqual(machine.reconcile(check=True)[0], 0)

    def test_policy_requires_valid_nonempty_nvidia_group_and_closed_driver_meta(self):
        path = Path(self.temporary.name) / "policy.json"
        original = json.loads((PROJECT / "rootfs/common" / NODE.POLICY_PATH.lstrip("/")).read_text())
        path.write_text(json.dumps(original))
        self.assertEqual(NODE.load_policy(path), original)
        for packages in (None, [], ["nvidia-container-toolkit"], ["linux-modules-nvidia-580-server-open-generic"], ["linux-modules-nvidia-580-server-generic", "nvidia-driver-580-open"], ["linux-modules-nvidia-580-server-generic", "bad;package"], "nvidia-container-toolkit"):
            with self.subTest(packages=packages):
                policy = json.loads(json.dumps(original))
                if packages is None:
                    del policy["packages"]["nvidia"]
                else:
                    policy["packages"]["nvidia"] = packages
                path.write_text(json.dumps(policy))
                with self.assertRaises(NODE.PolicyError):
                    NODE.load_policy(path)

    def test_inert_assets_render_exact_key_and_rules_without_enabling_repository(self):
        key = PROJECT / "rootfs/common" / NODE.NVIDIA_KEY.lstrip("/")
        self.assertEqual(hashlib.sha256(key.read_bytes()).hexdigest(), "c880576d6cf75a48e5027a871bac70fd0421ab07d2b55f30877b21f1c87959c9")
        if not shutil.which("jq"):
            self.skipTest("jq is explicitly required by the renderer")
        for platform in ("physical", "cloud"):
            with self.subTest(platform=platform):
                output = Path(self.temporary.name) / (platform + "-user-data")
                subprocess.run(["/bin/bash", "-c", 'set -euo pipefail; source "$1/lib/render.sh"; render_user_data "$2" node-a agent "$3" server-a TEST_AUTH ""', "bash", str(PROJECT), str(output), platform], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                boot = yaml.safe_load(output.read_text())
                assets = {entry["path"]: entry for entry in boot["write_files"]}
                self.assertNotIn(NODE.NVIDIA_SOURCE, assets)
                self.assertNotIn(NODE.NVIDIA_RULES, assets)
                self.assertEqual(base64.b64decode(assets[NODE.NVIDIA_KEY]["content"]), key.read_bytes())
                self.assertEqual(assets[NODE.NVIDIA_KEY]["permissions"], "0644")
                self.assertEqual(base64.b64decode(assets[NODE.NVIDIA_RULES_TEMPLATE]["content"]), (PROJECT / "rootfs/common" / NODE.NVIDIA_RULES_TEMPLATE.lstrip("/")).read_bytes())
                self.assertFalse(any(package in boot["packages"] for package in boot["packages"] if package.startswith("nvidia") or "modules-nvidia" in package))


if __name__ == "__main__":
    unittest.main()
