import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from lib import render

SETUP_PATH = PROJECT / "rootfs/common/usr/local/bin/setup-node.py"
SETUP_SPEC = importlib.util.spec_from_file_location("setup_node", SETUP_PATH)
SETUP = importlib.util.module_from_spec(SETUP_SPEC)
exec(compile(SETUP_PATH.read_text(), str(SETUP_PATH), "exec"), SETUP.__dict__)
LOAD = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('setup_node', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "exec(compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'), module.__dict__)\n"
    "print('imported', end='')\n"
)
QUOTED = (
    "plain", "", "with space", "quote'\"", "$(id)`id`$HOME", "line\nbreak", "tab\tbell\a",
    "\x01ctrl\x7f\x1b[0m", "é ünïcødé 😀", "~x", "a:~b", "semi;colon|pipe&amp<>()", "back\\slash",
    "*glob?[x]", "#hash", "{a,b}", "!bang", "trailing\n", "tskey-auth-k1=2+3/4",
)
STEPS = (
    "persist_seed", "configure_accounts", "disable_swap", "disable_multipath", "restrict_netplan",
    "configure_services", "configure_physical", "wait_for", "check", "setup_tailscale", "tailscale_ip",
    "setup_nvidia", "configure_k3s_files", "retry_output", "resolve_peer_ip", "node_token", "install_k3s",
    "register_nvidia_runtime", "bootstrap_flux", "log",
)


def bash_quote(values, locale):
    result = subprocess.run(
        ["bash", "-c", 'i=0; for value in "$@"; do printf "K%d=%q\\n" "$i" "$value"; i=$((i + 1)); done', "bash", *values],
        capture_output=True, check=True, env={**os.environ, "LC_ALL": locale},
    )
    return result.stdout


class SetupNodeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def patch(self, name, value):
        patcher = mock.patch.object(SETUP, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_importing_does_not_run_setup(self):
        result = subprocess.run([sys.executable, "-B", "-c", LOAD, str(SETUP_PATH)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "imported")

    def test_existing_k3s_is_reinstalled_without_download(self):
        k3s = self.directory / "k3s"
        self.assertEqual(SETUP.k3s_install_env(str(k3s)), ("INSTALL_K3S_CHANNEL", "stable"))
        calls = []
        self.patch("K3S_BIN", str(k3s))
        self.patch("pipe", lambda producer, consumer, env=None: calls.append((producer, consumer, env)) or 0)
        SETUP.install_k3s(["agent", "--server", "https://100.64.0.1:6443"], "K10secret::server:token")
        k3s.write_text("#!/bin/sh\n")
        k3s.chmod(0o755)
        self.assertEqual(SETUP.k3s_install_env(str(k3s)), ("INSTALL_K3S_SKIP_DOWNLOAD", "true"))
        SETUP.install_k3s(["server", "--cluster-init"], "")
        (producer, consumer, env), (_, init_consumer, init_env) = calls
        self.assertEqual(producer, ["curl", "-fsSL", "https://get.k3s.io"])
        self.assertEqual(consumer, ["sh", "-s", "-", "agent", "--server", "https://100.64.0.1:6443"])
        self.assertEqual(env["INSTALL_K3S_CHANNEL"], "stable")
        self.assertEqual(env["K3S_TOKEN"], "K10secret::server:token")
        self.assertNotIn("INSTALL_K3S_SKIP_DOWNLOAD", env)
        self.assertFalse(any("K10secret" in argument for argument in producer + consumer))
        self.assertEqual(init_consumer, ["sh", "-s", "-", "server", "--cluster-init"])
        self.assertEqual(init_env["INSTALL_K3S_SKIP_DOWNLOAD"], "true")
        self.assertNotIn("INSTALL_K3S_CHANNEL", init_env)
        text = SETUP_PATH.read_text()
        self.assertIn("k3s_install_env(K3S_BIN)", text)
        self.assertEqual(text.count("INSTALL_K3S_CHANNEL"), 1)
        self.assertNotIn("--tls-san", text)
        self.assertNotIn('"--token"', text)

    def test_k3s_arguments_keep_token_off_argv(self):
        common = ["--node-ip", "100.64.0.2", "--node-external-ip", "100.64.0.2", "--flannel-iface", "tailscale0",
                  "--resolv-conf", "/etc/k3s-resolv.conf", "--kubelet-arg", "allowed-unsafe-sysctls=net.ipv4.ip_forward"]
        server = ["--secrets-encryption", "--write-kubeconfig-mode", "0644", "--disable", "servicelb"]
        self.assertEqual(SETUP.k3s_arguments("agent", "100.64.0.2", "100.64.0.1"),
                         (["agent", "--server", "https://100.64.0.1:6443", *common], "k3s-agent.service"))
        self.assertEqual(SETUP.k3s_arguments("join", "100.64.0.2", "100.64.0.1"),
                         (["server", *common, *server, "--server", "https://100.64.0.1:6443"], "k3s.service"))
        self.assertEqual(SETUP.k3s_arguments("init", "100.64.0.2", ""),
                         (["server", *common, *server, "--cluster-init"], "k3s.service"))

    def test_tls_san_config_lists_every_address(self):
        output = self.directory / "30-node-tls.yaml"
        SETUP.write_tls_config(output, ["100.64.0.1", "192.168.68.64", "fraguinha.local"])
        self.assertEqual(yaml.safe_load(output.read_text()), {"tls-san": ["100.64.0.1", "192.168.68.64", "fraguinha.local"]})
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.assertFalse(Path(str(output) + ".tmp").exists())

    def test_agent_removes_server_k3s_files(self):
        directory = self.directory / "config.yaml.d"
        directory.mkdir(mode=0o700)
        for name in ("20-leader-election.yaml", "20-server-ha.yaml", "30-node-tls.yaml", "10-keep.yaml"):
            (directory / name).write_text("stale\n")
        self.patch("K3S_CONFIG_DIR", str(directory))
        SETUP.configure_k3s_files("agent", "physical", "node-a", "100.64.0.2")
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["10-keep.yaml"])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_physical_server_tls_lists_tailscale_lan_and_mdns(self):
        directory = self.directory / "config.yaml.d"
        directory.mkdir()
        (directory / "20-leader-election.yaml").write_text("stale\n")
        (directory / "20-server-ha.yaml").write_text("keep\n")
        self.patch("K3S_CONFIG_DIR", str(directory))
        self.patch("capture", lambda arguments, stderr=None: (0, "1.1.1.1 via 192.168.68.1 dev eth0 src 192.168.68.64 uid 0\n    cache\n"))
        SETUP.configure_k3s_files("join", "physical", "node-a", "100.64.0.2")
        self.assertEqual(yaml.safe_load((directory / "30-node-tls.yaml").read_text()),
                         {"tls-san": ["100.64.0.2", "192.168.68.64", "node-a.local"]})
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["20-server-ha.yaml", "30-node-tls.yaml"])

    def test_cloud_server_tls_falls_back_to_public_lookup(self):
        directory = self.directory / "config.yaml.d"
        calls = []
        self.patch("K3S_CONFIG_DIR", str(directory))
        self.patch("capture", lambda arguments, stderr=None: calls.append(arguments) or ((22, "") if len(calls) == 1 else (0, "203.0.113.7\n")))
        SETUP.configure_k3s_files("init", "cloud", "node-a", "100.64.0.2")
        self.assertEqual([call[-1] for call in calls], ["http://169.254.169.254/hetzner/v1/metadata/public-ipv4", "https://ifconfig.me/ip"])
        self.assertEqual(yaml.safe_load((directory / "30-node-tls.yaml").read_text()), {"tls-san": ["100.64.0.2", "203.0.113.7"]})

    def test_boot_media_seed_moves_into_nocloud_seed_directory(self):
        media = self.directory / "media"
        seed = self.directory / "seed"
        media.mkdir()
        (media / "meta-data").write_text("instance-id: node-a-20260101T000000Z\n")
        (media / "user-data").write_text("#cloud-config\n")
        (media / "network-config").write_text("network: {version: 2}\n")
        seed.mkdir()
        (seed / "network-config").write_text("stale\n")
        log = mock.Mock()
        self.patch("SEED_DIR", str(seed))
        self.patch("log", log)
        SETUP.persist_seed_from(media)
        self.assertEqual((seed / "user-data").read_text(), "#cloud-config\n")
        self.assertEqual((seed / "network-config").read_text(), "network: {version: 2}\n")
        self.assertEqual((seed / "meta-data").stat().st_mode & 0o777, 0o600)
        self.assertEqual(seed.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(path.name for path in media.iterdir()), ["meta-data"])
        log.assert_called_once_with("cloud-init seed moved from " + str(media) + " to " + str(seed))
        (media / "user-data").write_text("#cloud-config\nstale: true\n")
        SETUP.persist_seed_from(media)
        self.assertEqual((seed / "user-data").read_text(), "#cloud-config\n")
        self.assertFalse((media / "user-data").exists())
        log.assert_called_once()

    def test_tailscale_authkey_uses_private_file_removed_after_login(self):
        calls = []
        observed = {}
        authkey = "tskey-auth-k$y'\"secret"

        def check(arguments, env=None, stdout=None, stderr=None):
            calls.append(arguments)
            if arguments[:2] == ["tailscale", "up"]:
                key = Path(arguments[2].removeprefix("--auth-key=file:"))
                observed["key"] = key
                observed["content"] = key.read_text()
                observed["mode"] = key.stat().st_mode & 0o777
                observed["directory"] = key.parent.stat().st_mode & 0o777

        self.patch("PRIVATE_DIR", str(self.directory))
        self.patch("check", check)
        self.patch("wait_for", mock.Mock())
        self.patch("tailscale_status", lambda: {"BackendState": "NeedsLogin"})
        with mock.patch.object(SETUP.shutil, "which", return_value="/usr/bin/tailscale"):
            SETUP.setup_tailscale("node-a", authkey)
        self.assertEqual(calls[0], ["systemctl", "enable", "--now", "tailscaled"])
        self.assertEqual(calls[1], ["tailscale", "up", "--auth-key=file:" + str(observed["key"]), "--ssh", "--hostname=node-a"])
        self.assertEqual(observed["content"], authkey)
        self.assertEqual((observed["mode"], observed["directory"]), (0o600, 0o700))
        self.assertFalse(any(authkey in argument for call in calls for argument in call))
        self.assertEqual(list(self.directory.iterdir()), [])
        SETUP.wait_for.assert_called_once_with("Tailscale", "tailscale", "status")

    def test_running_tailscale_is_reconfigured_without_authkey(self):
        calls = []
        self.patch("check", lambda arguments, env=None, stdout=None, stderr=None: calls.append(arguments))
        self.patch("wait_for", mock.Mock())
        self.patch("tailscale_status", lambda: {"BackendState": "Running"})
        with mock.patch.object(SETUP.shutil, "which", return_value="/usr/bin/tailscale"):
            SETUP.setup_tailscale("node-a", "tskey-auth-secret")
        self.assertEqual(calls, [["systemctl", "enable", "--now", "tailscaled"], ["tailscale", "set", "--ssh=true", "--hostname=node-a"]])

    def test_peer_ip_resolves_first_ipv4_of_server(self):
        self.patch("tailscale_status", lambda: {"Peer": {
            "a": {"HostName": "other", "TailscaleIPs": ["100.64.0.9"]},
            "b": {"HostName": "node-a", "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.64.0.1", "100.64.0.3"]},
        }})
        self.assertEqual(SETUP.resolve_peer_ip("node-a"), "100.64.0.1")
        self.assertEqual(SETUP.resolve_peer_ip("missing"), "")
        self.patch("tailscale_status", lambda: None)
        self.assertEqual(SETUP.resolve_peer_ip("node-a"), "")

    def setup_host(self, role, platform):
        config = self.directory / "config"
        disabled = self.directory / "cloud-init.disabled"
        disabled.write_text("")
        values = ["node-b", role, platform, "node-a", "tskey-auth-k$y'\"\n", "ghp_token" if role == "init" else ""]
        config.write_bytes(subprocess.run(
            ["bash", "-c", "printf 'NODE_HOSTNAME=%q\\nNODE_ROLE=%q\\nNODE_PLATFORM=%q\\nSERVER_HOSTNAME=%q\\nTAILSCALE_AUTHKEY=%q\\nGITHUB_TOKEN=%q\\n' \"$@\"", "bash", *values],
            capture_output=True, check=True).stdout)
        manager = mock.Mock()
        for name in STEPS:
            self.patch(name, getattr(manager, name))
        manager.tailscale_ip.return_value = "100.64.0.2"
        manager.setup_nvidia.return_value = (False, False)
        manager.retry_output.side_effect = lambda function: function()
        manager.resolve_peer_ip.return_value = "100.64.0.1"
        manager.node_token.return_value = "K10secret::server:token"
        manager.persist_seed.side_effect = lambda: manager.cloud_init_disabled(disabled.exists())
        self.patch("CONFIG", str(config))
        self.patch("K3S_BIN", str(self.directory / "k3s"))
        self.patch("CLOUD_INIT_DISABLED", str(disabled))
        self.patch("REBOOT_MARKER", str(self.directory / "nodes-reboot"))
        self.patch("REBOOT_REQUIRED", str(self.directory / "reboot-required"))
        return manager, config, disabled

    def test_setup_never_disables_cloud_init(self):
        manager, config, disabled = self.setup_host("join", "physical")
        SETUP.setup()
        self.assertFalse(disabled.exists())
        manager.cloud_init_disabled.assert_called_once_with(False)
        text = SETUP_PATH.read_text()
        self.assertIn("Path(CLOUD_INIT_DISABLED).unlink(missing_ok=True)", text)
        self.assertEqual(text.count("CLOUD_INIT_DISABLED"), 2)

    def test_join_setup_orders_steps_and_passes_token_through_installer_env(self):
        manager, config, disabled = self.setup_host("join", "physical")
        SETUP.setup()
        names = [call[0] for call in manager.mock_calls]
        order = ["persist_seed", "configure_accounts", "disable_swap", "disable_multipath", "restrict_netplan",
                 "configure_services", "configure_physical", "wait_for", "check", "wait_for", "setup_tailscale",
                 "tailscale_ip", "setup_nvidia", "configure_k3s_files", "retry_output", "resolve_peer_ip",
                 "retry_output", "node_token", "install_k3s", "log"]
        self.assertEqual([name for name in names if name in STEPS], order)
        manager.wait_for.assert_any_call("internet access", "ping", "-c1", "-W2", "1.1.1.1")
        manager.wait_for.assert_any_call("time synchronization", "chronyc", "waitsync", "1", "0.1", "0.0", "1")
        manager.check.assert_called_once_with(["systemctl", "restart", "chrony.service"])
        manager.setup_tailscale.assert_called_once_with("node-b", "tskey-auth-k$y'\"\n")
        manager.configure_k3s_files.assert_called_once_with("join", "physical", "node-b", "100.64.0.2")
        manager.resolve_peer_ip.assert_called_once_with("node-a")
        manager.node_token.assert_called_once_with("100.64.0.1")
        arguments, token = manager.install_k3s.call_args.args
        self.assertEqual((arguments, token), (SETUP.k3s_arguments("join", "100.64.0.2", "100.64.0.1")[0], "K10secret::server:token"))
        self.assertNotIn(token, arguments)
        manager.register_nvidia_runtime.assert_not_called()
        manager.bootstrap_flux.assert_not_called()
        manager.log.assert_called_once_with("provisioning complete")
        self.assertFalse(config.exists())
        self.assertTrue((self.directory / "nodes-reboot").exists())

    def test_init_cloud_setup_bootstraps_flux_and_registers_nvidia_without_reboot(self):
        manager, config, disabled = self.setup_host("init", "cloud")
        manager.setup_nvidia.return_value = (True, False)
        SETUP.setup()
        manager.retry_output.assert_not_called()
        manager.configure_physical.assert_not_called()
        manager.install_k3s.assert_called_once_with(SETUP.k3s_arguments("init", "100.64.0.2", "")[0], "")
        manager.register_nvidia_runtime.assert_called_once_with("k3s.service")
        manager.bootstrap_flux.assert_called_once_with("ghp_token")
        self.assertFalse(config.exists())
        self.assertFalse((self.directory / "nodes-reboot").exists())

    def test_agent_setup_marks_reboot_for_unbound_gpu_and_uses_agent_unit(self):
        manager, config, disabled = self.setup_host("agent", "cloud")
        manager.setup_nvidia.return_value = (True, True)
        SETUP.setup()
        arguments, token = manager.install_k3s.call_args.args
        self.assertEqual(arguments[:3], ["agent", "--server", "https://100.64.0.1:6443"])
        self.assertEqual(token, "K10secret::server:token")
        manager.register_nvidia_runtime.assert_called_once_with("k3s-agent.service")
        self.assertTrue((self.directory / "nodes-reboot").exists())

    def test_physical_setup_with_existing_k3s_skips_reboot_unless_required(self):
        manager, config, disabled = self.setup_host("join", "physical")
        k3s = self.directory / "k3s"
        k3s.write_text("#!/bin/sh\n")
        k3s.chmod(0o755)
        SETUP.setup()
        self.assertFalse((self.directory / "nodes-reboot").exists())
        config.write_text("NODE_HOSTNAME=node-b\nNODE_ROLE=join\nNODE_PLATFORM=physical\nSERVER_HOSTNAME=node-a\n")
        (self.directory / "reboot-required").write_text("")
        SETUP.setup()
        self.assertTrue((self.directory / "nodes-reboot").exists())

    def test_setup_rejects_missing_or_invalid_config(self):
        self.patch("CONFIG", str(self.directory / "config"))
        with self.assertRaisesRegex(SETUP.Failure, "not found"):
            SETUP.setup()
        (self.directory / "config").write_text("NODE_ROLE=server\nNODE_PLATFORM=physical\n")
        with self.assertRaisesRegex(SETUP.Failure, "invalid node role"):
            SETUP.setup()
        (self.directory / "config").write_text("NODE_ROLE=join\nNODE_PLATFORM=metal\n")
        with self.assertRaisesRegex(SETUP.Failure, "invalid node platform"):
            SETUP.setup()
        (self.directory / "config").write_text("NODE_ROLE=$(reboot)\n")
        with self.assertRaisesRegex(SETUP.Failure, "invalid"):
            SETUP.setup()

    def test_config_parser_round_trips_bash_printf_quoting(self):
        expected = {"K" + str(index): value for index, value in enumerate(QUOTED)}
        for locale in ("C", "C.UTF-8", "en_US.UTF-8"):
            with self.subTest(locale=locale):
                self.assertEqual(SETUP.parse_config(bash_quote(QUOTED, locale)), expected)

    def test_config_parser_round_trips_renderer_quoting(self):
        values = (*QUOTED, "zero\u200bwidth", "nbsp\u00a0x", "a #b", "x=y", "del\x7f")
        data = "".join("K%d=%s\n" % (index, render.shell_quote(value)) for index, value in enumerate(values)).encode()
        self.assertEqual(SETUP.parse_config(data), {"K" + str(index): value for index, value in enumerate(values)})

    def test_config_parser_decodes_ansi_c_octal_and_control_escapes(self):
        data = (
            rb"A=$'\101\x41\x4g\u00e9\U0001F600\cA\c?\e\q\303\251\'\0101'" + b"\n"
            rb"B='lit $x'\ " + rb'"d\$q\z"' + b"\n"
            b"# comment\n"
            b"C=x # trailing\n"
            b"D=a\\\nb E=\n"
            b"F=1\nF=2\n"
        )
        self.assertEqual(SETUP.parse_config(data), {
            "A": "AA\x04g\u00e9\U0001F600\x01\x7f\x1b\\q\u00e9'\x081",
            "B": "lit $x d$q\\z",
            "C": "x",
            "D": "ab",
            "E": "",
            "F": "2",
        })

    def test_config_parser_rejects_shell_execution_and_malformed_input(self):
        for data in (b"A=$(id)\n", b"A=`id`\n", b"A=$HOME\n", b"echo hi\n", b"A=x;B=y\n", b"A='open\n",
                     b"A=$'open\n", b"A=\"$x\"\n", b"A=\"open\n", b"A=$'\\0'\n", b"A=x\\", b"A=a|b\n",
                     b"A=a>b\n", b"export A=1\n", b"A=(x)\n", b"1A=x\n"):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    SETUP.parse_config(data)
        config = self.directory / "config"
        config.write_bytes(b"NODE_ROLE=`id`\n")
        with self.assertRaisesRegex(SETUP.Failure, "invalid " + str(config)):
            SETUP.read_config(str(config))


if __name__ == "__main__":
    unittest.main()
