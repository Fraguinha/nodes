import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from lib import render

POLICY = "rootfs/common/usr/local/share/nodes/node-policy.json"
NODES_CONFIG = "/etc/cloud/cloud.cfg.d/99-nodes.cfg"
HA_PATH = "/etc/rancher/k3s/config.yaml.d/20-server-ha.yaml"
NVIDIA_KEY = "/usr/local/share/nodes/nvidia-container-toolkit.asc"
NVIDIA_RULES_TEMPLATE = "/usr/local/share/nodes/nvidia-udev.rules"


def copy_project(directory):
    project = Path(directory) / "project"
    for name in ("lib", "cloud-init", "rootfs", "config"):
        shutil.copytree(PROJECT / name, project / name)
    return project


def failure(function, *args):
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        try:
            function(*args)
        except SystemExit as error:
            return error.code, errors.getvalue()
    return 0, errors.getvalue()


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def use_project(self, project):
        patch = mock.patch.object(render, "NODES_PROJECT_DIR", project)
        patch.start()
        self.addCleanup(patch.stop)

    def render_user_data(self, project, hostname, role, platform, server="server-a", mode="provision"):
        output = self.directory / (hostname + "-" + platform + "-" + mode + "-user-data")
        with mock.patch.object(render, "NODES_PROJECT_DIR", project):
            render.render_user_data(output, hostname, role, platform, server, "tskey-fixture", "", mode)
        return yaml.safe_load(output.read_text())

    def test_user_data_keeps_ssh_host_keys(self):
        output = self.directory / "user-data"
        for mode in ("provision", "resync"):
            with self.subTest(mode=mode):
                render.render_user_data(output, "node-a", "join", "cloud", "server-a", "tskey-fixture", "", mode)
                text = output.read_text()
                self.assertIn("\nssh_deletekeys: false\n", text)
                self.assertNotIn("ssh_genkeytypes", text)
                self.assertIs(yaml.safe_load(text)["ssh_deletekeys"], False)

    def test_resync_mode_skips_full_upgrade_and_keeps_apt_sources(self):
        policy = json.loads((PROJECT / POLICY).read_text())
        provision = self.render_user_data(PROJECT, "node-a", "join", "physical")
        resync = self.render_user_data(PROJECT, "node-a", "join", "physical", mode="resync")
        self.assertIs(provision["package_upgrade"], policy["apt"]["package_upgrade"])
        self.assertIs(provision["apt"]["preserve_sources_list"], False)
        self.assertIs(resync["package_update"], policy["apt"]["package_update"])
        self.assertIs(resync["package_upgrade"], False)
        self.assertIs(resync["apt"]["preserve_sources_list"], True)
        self.assertEqual(resync["packages"], provision["packages"])
        self.assertEqual(resync["swap"], {"size": 0})
        self.assertEqual(resync["apt"]["conf"], provision["apt"]["conf"])
        code, stderr = failure(render.render_user_data, self.directory / "invalid", "node-a", "join", "cloud", "server-a", "tskey-fixture", "", "upgrade")
        self.assertNotEqual(code, 0)
        self.assertIn("render mode", stderr)

    def test_boot_consumers_share_policy_and_native_password_semantics(self):
        project = copy_project(self.directory)
        self.use_project(project)
        policy_path = project / POLICY
        policy = json.loads(policy_path.read_text())
        policy["timezone"] = "Etc/UTC"
        policy["apt"]["conf"]["Acquire::Retries"] = "7"
        policy["network"]["wifi"]["route_metric"] = 111
        policy_path.write_text(json.dumps(policy))
        user_data, network_data = self.directory / "user-data", self.directory / "network-data"
        render.render_user_data(user_data, "node-a", "join", "physical", "server-a", "TEST_AUTH", "")
        render.render_network_config(network_data, 'SSID with "quotes"', "private-wifi-key", "node-a")
        boot = yaml.safe_load(user_data.read_text())
        network = yaml.safe_load(network_data.read_text())
        self.assertEqual(boot["timezone"], "Etc/UTC")
        self.assertIn('Acquire::Retries "7";', boot["apt"]["conf"])
        self.assertEqual(boot["chpasswd"], {"expire": False, "users": [{"name": "root", "password": "!", "type": "hash"}]})
        self.assertEqual(boot["packages"], policy["packages"]["common"] + policy["packages"]["physical"])
        self.assertEqual(boot["users"][0]["groups"], policy["users"]["pi"]["groups"])
        self.assertFalse(boot["ssh_deletekeys"])
        keys = [line for line in (project / "config/authorized_keys").read_text().splitlines() if line]
        self.assertEqual(boot["users"][0]["ssh_authorized_keys"], keys)
        self.assertEqual(network["network"]["wifis"]["wlan0"]["dhcp4-overrides"]["route-metric"], 111)
        self.assertEqual(network["network"]["wifis"]["wlan0"]["access-points"], {'SSID with "quotes"': {"auth": {"key-management": "psk", "password": "private-wifi-key"}}})
        self.assertEqual(network["network"]["ethernets"]["ethernet"]["dhcp4-overrides"]["route-metric"], 200)
        self.assertNotIn("TEST_AUTH", str(network))
        render.render_network_config(network_data, 'HISTORICAL "SSID"', "private-wifi-key", "fraguinha")
        static = yaml.safe_load(network_data.read_text())["network"]
        self.assertFalse(static["ethernets"]["eth0"]["dhcp4"])
        self.assertEqual(static["ethernets"]["eth0"]["addresses"], ["192.168.68.64/22"])
        self.assertEqual(static["wifis"]["wlan0"]["dhcp4-overrides"]["route-metric"], 600)

    def test_every_inventory_node_renders_cloud_init_only_configuration(self):
        inventory = json.loads((PROJECT / "config/nodes.json").read_text())
        self.assertEqual(set(inventory), {"fraguinha", "a50passos", "livinvicta", "carolion", "cloud-1", "cloud-2"})
        for hostname, node in inventory.items():
            for mode in ("provision", "resync"):
                with self.subTest(hostname=hostname, mode=mode):
                    boot = self.render_user_data(PROJECT, hostname, node["role"], node["platform"], node["server"], mode)
                    entries = {entry["path"]: entry for entry in boot["write_files"]}
                    cloud_config = yaml.safe_load(base64.b64decode(entries[NODES_CONFIG]["content"]))
                    self.assertEqual(entries[NODES_CONFIG]["permissions"], "0644")
                    self.assertEqual(cloud_config["datasource_list"], ["NoCloud", "None"])
                    if node["platform"] == "cloud":
                        self.assertEqual(cloud_config["network"], {"config": "disabled"})
                    else:
                        self.assertNotIn("network", cloud_config)
                    if node["role"] == "agent":
                        self.assertNotIn(HA_PATH, entries)
                    else:
                        self.assertEqual(base64.b64decode(entries[HA_PATH]["content"]),
                                         (PROJECT / "rootfs/common/usr/local/share/nodes/k3s-server-ha.yaml").read_bytes())
                    self.assertFalse(any("cloud-init.disabled" in path for path in entries))
                    self.assertEqual(sorted(path for path in entries if path.startswith("/usr/local/bin/")), [
                        "/usr/local/bin/node-containerd-apparmor.py", "/usr/local/bin/node-health-check.py",
                        "/usr/local/bin/node-wifi-recovery.py", "/usr/local/bin/setup-node.py", "/usr/local/bin/watchdog-load.py",
                    ])
                    self.assertEqual(boot["runcmd"], ["/usr/local/bin/setup-node.py"])
                    self.assertTrue(all(command.startswith("cloud-init-per instance ") for command in boot["bootcmd"]))
                    config = base64.b64decode(entries["/etc/nodes/config"]["content"]).decode()
                    self.assertIn("NODE_ROLE=" + node["role"], config)
                    self.assertIn("NODE_PLATFORM=" + node["platform"], config)
                    self.assertIn("SERVER_HOSTNAME=" + node["server"], config)

    def test_profile_without_wifis_needs_no_credentials(self):
        project = copy_project(self.directory)
        self.use_project(project)
        policy_path = project / POLICY
        policy = json.loads(policy_path.read_text())
        policy["network"]["profiles"]["wired"] = {"ethernets": {"eth0": {"dhcp4": True, "optional": False}}}
        policy_path.write_text(json.dumps(policy))
        output = self.directory / "network-config"
        render.render_network_config(output, "", "", "wired")
        network = yaml.safe_load(output.read_text())["network"]
        self.assertEqual(network, {"version": 2, "ethernets": {"eth0": {"dhcp4": True, "optional": False}}})
        self.assertIs(render.network_profile_has_wifis("wired"), False)
        code, stderr = failure(render.render_network_config, output, "", "", "fraguinha")
        self.assertNotEqual(code, 0)
        self.assertIn("Wi-Fi SSID", stderr)

    def test_meta_data_uses_timestamped_instance_id(self):
        output = self.directory / "meta-data"
        render.render_meta_data(output, "node-a", render.new_instance_id("node-a"))
        meta = yaml.safe_load(output.read_text())
        self.assertEqual(meta["local-hostname"], "node-a")
        self.assertRegex(meta["instance-id"], r"^node-a-[0-9]{8}T[0-9]{6}Z$")

    def test_nvidia_assets_are_inert_until_setup_detects_a_gpu(self):
        key = PROJECT / "rootfs/common" / NVIDIA_KEY.lstrip("/")
        self.assertEqual(hashlib.sha256(key.read_bytes()).hexdigest(), "c880576d6cf75a48e5027a871bac70fd0421ab07d2b55f30877b21f1c87959c9")
        for platform in ("physical", "cloud"):
            with self.subTest(platform=platform):
                boot = self.render_user_data(PROJECT, "node-a", "agent", platform)
                assets = {entry["path"]: entry for entry in boot["write_files"]}
                self.assertNotIn("/etc/apt/sources.list.d/nvidia-container-toolkit.sources", assets)
                self.assertNotIn("/etc/udev/rules.d/71-nvidia.rules", assets)
                self.assertEqual(base64.b64decode(assets[NVIDIA_KEY]["content"]), key.read_bytes())
                self.assertEqual(assets[NVIDIA_KEY]["permissions"], "0644")
                self.assertEqual(base64.b64decode(assets[NVIDIA_RULES_TEMPLATE]["content"]),
                                 (PROJECT / "rootfs/common" / NVIDIA_RULES_TEMPLATE.lstrip("/")).read_bytes())
                self.assertFalse(any(package.startswith("nvidia") or "modules-nvidia" in package for package in boot["packages"]))


if __name__ == "__main__":
    unittest.main()
