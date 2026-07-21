import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
HEALTH = PROJECT / "rootfs/common/usr/local/bin/node-health-check.py"
WIFI = PROJECT / "rootfs/common/usr/local/bin/node-wifi-recovery.py"
WATCHDOG_SPEC = importlib.util.spec_from_file_location(
    "watchdog_load", PROJECT / "rootfs/common/usr/local/bin/watchdog-load.py")
WATCHDOG = importlib.util.module_from_spec(WATCHDOG_SPEC)
exec(compile(Path(WATCHDOG_SPEC.origin).read_text(), WATCHDOG_SPEC.origin, "exec"), WATCHDOG.__dict__)

FAKE_COMMAND = r'''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
config = json.loads(Path(os.environ["RECOVERY_FAKE_CONFIG"]).read_text())
with Path(os.environ["RECOVERY_FAKE_CALLS"]).open("a") as trace:
    trace.write(json.dumps([name, *args]) + "\n")

if name == "timeout":
    while args and args[0].startswith("-"):
        args.pop(0)
    args.pop(0)
    os.execvp(args[0], args)
if name == "flock":
    sys.exit(config.get("lock_exit", 0))
if name == "logger":
    sys.exit(config.get("logger_exit", 0))
if name == "journalctl":
    if config.get("journal_exit", 0):
        sys.exit(config["journal_exit"])
    records = config.get("journal", [])
    after = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--after-cursor=")), None)
    if after is not None:
        matching = [index for index, record in enumerate(records) if record["__CURSOR"] == after]
        if not matching:
            sys.exit(1)
        records = records[matching[-1] + 1:][:100]
    else:
        records = records[-100:]
    for record in records:
        print(json.dumps(record))
    sys.exit(0)
if name == "date":
    print(config["wall"])
    sys.exit(0)
if name == "chronyc":
    sys.exit(config.get("chronyc_exit", 0))
if name == "sync":
    sys.exit(config.get("sync_exit", 0))
if name == "modprobe":
    code = config.get("modprobe", {}).get(args[0], 1)
    if code == 0 and args[0] == config.get("watchdog_module"):
        Path(config["watchdog_device"]).touch()
    sys.exit(code)
if name == "udevadm":
    sys.exit(config.get("udevadm_exit", 0))
if name == "bridge":
    print(json.dumps([{"dst": peer} for peer in config["peers"]]))
    sys.exit(0)
if name == "iw":
    print("Connected to 02:00:00:00:00:01" if config["associated"] else "Not connected.")
    sys.exit(0)
if name == "ping":
    sys.exit(0 if config["gateway_ping"] else 1)
if name == "curl":
    sys.exit(0 if config["http"] else 7)
if name == "networkctl":
    sys.exit(config.get(args[0] + "_exit", 0))
if name == "tailscale":
    if args == ["status", "--json"]:
        print(json.dumps({"BackendState": config["backend"]}))
        sys.exit(0)
    if args and args[0] == "ping":
        if config["ts_ping"] and config.get("ts_pong", True):
            print("pong from " + args[-1] + " via 192.0.2.2:41641 in 1ms")
        sys.exit(0 if config["ts_ping"] else 1)
    if args == ["up"]:
        sys.exit(config.get("tailscale_up_exit", 0))
if name == "systemctl":
    if args == ["daemon-reexec"]:
        sys.exit(config.get("reexec_exit", 0))
    if args == ["is-enabled", "k3s-agent"]:
        sys.exit(0 if config.get("k3s_agent", False) else 1)
    if args and args[0] == "show":
        if args[1] == "netplan-wpa-wlan0.service":
            print(config["wifi_unit"])
        else:
            print(config.get("k3s_active_enter", 1000000))
        sys.exit(0)
    if args == ["is-active", "--quiet", "tailscaled.service"]:
        sys.exit(0 if config["ts_active"] else 3)
    if args and args[0] == "is-active" and args[1] in ("k3s", "k3s-agent"):
        print(config["k3s_state"])
        sys.exit(0 if config["k3s_state"] == "active" else 3)
    if args == ["restart", "netplan-wpa-wlan0.service"]:
        sys.exit(config.get("supplicant_exit", 0))
    if args == ["--no-block", "restart", "tailscaled.service"]:
        sys.exit(config.get("ts_restart_exit", 0))
    if args[:2] == ["--no-block", "start"] or args[:2] == ["--no-block", "restart"]:
        sys.exit(0)
    if args == ["--no-block", "reboot"]:
        sys.exit(config.get("reboot_exit", 0))
if name == "ip":
    if args == ["-j", "-4", "route", "show", "default"]:
        if config.get("routes_exit", 0):
            sys.exit(config["routes_exit"])
        print(json.dumps(config["routes"]))
        sys.exit(0)
    if args[:4] == ["-j", "-4", "address", "show"]:
        addresses = [{"family": "inet", "scope": "global", "local": "192.0.2.10"}]
        print(json.dumps([{"addr_info": addresses if config["address"] else []}]))
        sys.exit(0)
    if args[:3] == ["-j", "-4", "neigh"]:
        print(json.dumps([{"state": ["REACHABLE"]}]))
        sys.exit(0)
    if args == ["link", "show", "flannel.1"]:
        sys.exit(0 if config["flannel"] else 1)
    if args[:4] == ["link", "set", "dev", "wlan0"]:
        sys.exit(config.get("link_" + args[4] + "_exit", 0))
raise SystemExit("Unexpected isolated command: " + repr([name, *args]))
'''


class NodeRecoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.config_path = self.root / "config.json"
        self.calls_path = self.root / "calls.jsonl"
        self.uptime = self.root / "uptime"
        self.health_dir = self.root / "run/health"
        self.wifi_dir = self.root / "run/wifi"
        self.persist_dir = self.root / "persist/wifi"
        self.sysfs = self.root / "sys/class/net"
        (self.sysfs / "wlan0/wireless").mkdir(parents=True)
        self.peer_cache = self.root / "persist/cluster-peer-ips"
        self.ts_state = self.health_dir / "tailscale-recovery"
        self.wifi_state = self.wifi_dir / "recovery"
        self.budget = self.persist_dir / "reboot-budget"
        dispatcher = self.bin / "fake-command"
        dispatcher.write_text("#!" + sys.executable + "\n" + FAKE_COMMAND)
        dispatcher.chmod(0o755)
        for name in (
            "timeout", "flock", "logger", "journalctl", "date", "chronyc", "sync", "bridge",
            "iw", "ping", "curl", "networkctl", "tailscale", "systemctl", "ip", "modprobe", "udevadm",
            "reboot", "shutdown", "sudo", "sleep",
        ):
            (self.bin / name).symlink_to(dispatcher)
        interpreter = self.bin / "python3"
        interpreter.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        interpreter.chmod(0o755)
        self.device = self.root / "dev/watchdog0"
        self.device.parent.mkdir()
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "LC_ALL": "C",
            "RECOVERY_FAKE_CONFIG": str(self.config_path),
            "RECOVERY_FAKE_CALLS": str(self.calls_path),
            "NODE_HEALTH_RUN_DIR": str(self.health_dir),
            "NODE_HEALTH_UPTIME_FILE": str(self.uptime),
            "NODE_HEALTH_PEER_CACHE": str(self.peer_cache),
            "NODE_HEALTH_WIFI_HELPER": str(WIFI),
            "NODE_WIFI_RUNTIME_DIR": str(self.wifi_dir),
            "NODE_WIFI_PERSIST_DIR": str(self.persist_dir),
            "NODE_WIFI_SYSFS_DIR": str(self.sysfs),
            "NODE_WIFI_UPTIME_FILE": str(self.uptime),
        }
        self.config = {
            "wall": 1800000000,
            "peers": ["100.64.0.2"],
            "associated": True,
            "gateway_ping": False,
            "http": False,
            "backend": "Running",
            "ts_ping": False,
            "ts_active": True,
            "wifi_unit": "loaded",
            "k3s_state": "active",
            "flannel": True,
            "address": True,
            "routes": [{"dev": "wlan0", "gateway": "192.0.2.1"}],
        }

    def run_entry(self, script, now, expected=0, *args):
        self.uptime.write_text(f"{now}.00 0.00\n")
        self.config_path.write_text(json.dumps(self.config))
        result = subprocess.run(
            [sys.executable, str(script), *args],
            env=self.env,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Unexpected isolated command", result.stderr)
        return result

    def watchdog(self, expected, *args):
        self.config_path.write_text(json.dumps(self.config))
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(WATCHDOG, "DEVICE", str(self.device)), contextlib.redirect_stderr(stderr):
            self.assertEqual(WATCHDOG.main(list(args)), expected)
        return stderr.getvalue()

    def wifi(self, now, expected=1, *args):
        return self.run_entry(WIFI, now, expected, *args)

    def health(self, now):
        return self.run_entry(HEALTH, now)

    def calls(self, name=None):
        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        return [call for call in calls if name is None or call[0] == name]

    def count(self, *command):
        return self.calls().count(list(command))

    def logs(self):
        return [call[-1] for call in self.calls("logger")]

    def wifi_record(self):
        version, *values = self.wifi_state.read_text().split()
        self.assertEqual(version, "v2")
        return dict(zip(
            ("stage", "started", "due", "observed", "success_started", "successes"),
            map(int, values),
        ))

    def ts_record(self):
        version, *values = self.ts_state.read_text().split()
        self.assertEqual(version, "v2")
        return dict(zip(
            ("started", "due", "observed", "attempted", "success_started", "successes", "startup_attempted", "deferred", "warned"),
            map(int, values),
        ))

    def assert_no_wifi_actions(self):
        self.assertEqual(self.count("systemctl", "restart", "netplan-wpa-wlan0.service"), 0)
        self.assertEqual(self.calls("networkctl"), [])
        self.assertEqual(self.count("ip", "link", "set", "dev", "wlan0", "down"), 0)
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)

    def exhaust_wifi_to_reboot(self):
        for now in (100, 400, 520, 700):
            self.wifi(now)

    def test_cached_reachable_neighbor_cannot_prevent_recovery(self):
        self.wifi(100)
        self.wifi(400)
        self.assertEqual(self.wifi_record()["stage"], 1)
        self.assertEqual(self.count("systemctl", "restart", "netplan-wpa-wlan0.service"), 1)
        self.assertGreater(len(self.calls("ping")), 0)
        self.assertGreater(len(self.calls("curl")), 0)

    def test_gateway_reply_is_fresh_health_without_initial_recovery(self):
        self.config["gateway_ping"] = True
        for now in (100, 115, 130, 500):
            self.wifi(now, 0)
        self.assertFalse(self.wifi_state.exists())
        self.assert_no_wifi_actions()
        self.assertEqual(len(self.logs()), 1)
        self.assertIn("probe=gateway_icmp", self.logs()[0])

    def test_external_http_reply_is_alternative_fresh_health(self):
        self.config["http"] = True
        self.wifi(100, 0)
        self.assertFalse(self.wifi_state.exists())
        self.assert_no_wifi_actions()
        self.assertIn("probe=external_http", self.logs()[0])

    def test_gateway_requires_association(self):
        self.config.update(associated=False, gateway_ping=True)
        self.wifi(100)
        self.wifi(400)
        self.assertEqual(self.wifi_record()["stage"], 1)

    def test_gateway_requires_global_address(self):
        self.config.update(address=False, gateway_ping=True)
        self.wifi(100)
        self.wifi(400)
        self.assertEqual(self.wifi_record()["stage"], 1)

    def test_gateway_requires_default_route(self):
        self.config.update(routes=[], gateway_ping=True)
        self.wifi(100)
        self.wifi(400)
        self.assertEqual(self.wifi_record()["stage"], 1)

    def test_transient_wifi_pass_retains_deadline_and_escalation(self):
        self.wifi(100)
        self.config["gateway_ping"] = True
        self.wifi(390, 0)
        self.assertEqual(self.wifi_record()["due"], 400)
        self.config["gateway_ping"] = False
        self.wifi(400)
        self.assertEqual(self.wifi_record()["stage"], 1)
        self.config["gateway_ping"] = True
        self.wifi(500, 0)
        self.config["gateway_ping"] = False
        self.wifi(520)
        record = self.wifi_record()
        self.assertEqual((record["started"], record["stage"], record["successes"]), (100, 2, 0))
        self.assertEqual(self.count("ip", "link", "set", "dev", "wlan0", "down"), 1)

    def test_wifi_clear_requires_three_consecutive_samples_and_thirty_seconds(self):
        self.wifi(100)
        self.config["gateway_ping"] = True
        for now in (120, 125, 129):
            self.wifi(now, 0)
        self.assertEqual(self.wifi_record()["successes"], 3)
        self.config["gateway_ping"] = False
        self.wifi(130)
        self.assertEqual(self.wifi_record()["successes"], 0)
        self.config["gateway_ping"] = True
        for now in (150, 165):
            self.wifi(now, 0)
            self.assertTrue(self.wifi_state.exists())
        self.wifi(180, 0)
        self.assertFalse(self.wifi_state.exists())
        self.assert_no_wifi_actions()

    def test_stable_wifi_clears_only_wifi_incident(self):
        self.health(100)
        self.config["gateway_ping"] = True
        for now in (200, 215, 230):
            self.health(now)
        self.assertFalse(self.wifi_state.exists())
        self.assertEqual((self.ts_record()["started"], self.ts_record()["due"]), (100, 400))
        self.health(400)
        self.assertEqual(self.ts_record()["attempted"], 1)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)

    def test_underlay_flaps_preserve_tailscale_deadline_and_attempt(self):
        self.health(100)
        self.config["gateway_ping"] = True
        self.health(390)
        self.config["gateway_ping"] = False
        self.health(400)
        self.assertEqual(self.ts_record()["due"], 400)
        self.assertEqual(self.ts_record()["attempted"], 0)
        self.assertEqual(self.ts_record()["deferred"], 1)
        self.config["gateway_ping"] = True
        self.health(415)
        self.config["gateway_ping"] = False
        self.health(420)
        self.config["gateway_ping"] = True
        self.health(430)
        record = self.ts_record()
        self.assertEqual((record["started"], record["due"], record["attempted"]), (100, 400, 1))
        self.assertEqual(self.wifi_record()["stage"], 1)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)

    def test_tailscale_restart_is_once_even_if_restart_request_fails(self):
        self.config.update(gateway_ping=True, ts_restart_exit=7)
        for now in (100, 399):
            self.health(now)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 0)
        for now in (400, 415, 800, 1000):
            self.health(now)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)
        self.assertEqual(self.ts_record()["attempted"], 1)
        self.assertTrue(any("action=restart_tailscaled result=failed exit_code=7" in log for log in self.logs()))

    def test_transient_peer_pong_does_not_rearm_tailscale_restart(self):
        self.config["gateway_ping"] = True
        self.health(100)
        self.health(400)
        self.config["ts_ping"] = True
        self.health(410)
        self.assertEqual(self.ts_record()["attempted"], 1)
        self.config["ts_ping"] = False
        self.health(425)
        self.assertEqual(self.ts_record()["successes"], 0)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)
        self.config["ts_ping"] = True
        for now in (430, 445):
            self.health(now)
            self.assertTrue(self.ts_state.exists())
        self.health(460)
        self.assertFalse(self.ts_state.exists())
        self.config["ts_ping"] = False
        self.health(500)
        self.health(800)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 2)

    def test_stable_peer_pongs_clear_only_tailscale_incident(self):
        self.health(100)
        self.config["ts_ping"] = True
        self.health(200)
        self.wifi(210)
        self.health(215)
        self.health(230)
        self.assertFalse(self.ts_state.exists())
        record = self.wifi_record()
        self.assertEqual((record["started"], record["successes"]), (100, 2))
        self.assert_no_wifi_actions()

    def test_working_peer_suppresses_wifi_actions_when_public_probes_are_filtered(self):
        self.config["ts_ping"] = True
        for now in (100, 400, 520, 700, 1200):
            self.health(now)
        self.assertFalse(self.wifi_state.exists())
        self.assertFalse(self.ts_state.exists())
        self.assert_no_wifi_actions()
        self.assertTrue(any("layer=wifi state=healthy probe=tsmp target=100.64.0.2" in log for log in self.logs()))

    def test_success_exit_without_tsmp_pong_is_not_peer_health(self):
        self.config.update(ts_ping=True, ts_pong=False, gateway_ping=True)
        self.health(100)
        self.health(400)
        self.assertEqual(self.ts_record()["attempted"], 1)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)

    def test_no_peer_candidates_preserve_incident_and_reset_success_streak(self):
        self.config["gateway_ping"] = True
        self.health(100)
        self.config["ts_ping"] = True
        self.health(200)
        self.config["peers"] = []
        self.peer_cache.unlink()
        self.health(215)
        self.health(800)
        record = self.ts_record()
        self.assertEqual((record["started"], record["due"], record["successes"]), (100, 400, 0))
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 0)
        self.config.update(peers=["100.64.0.2"], ts_ping=False)
        self.health(815)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)

    def test_tailnet_candidate_probes_are_bounded(self):
        self.config["peers"] = [f"100.64.0.{index}" for index in range(2, 12)]
        self.health(100)
        self.assertEqual(len([call for call in self.calls("tailscale") if call[1] == "ping"]), 5)

    def test_wifi_stages_have_bounded_delays_and_one_reboot(self):
        self.wifi(100)
        self.wifi(399)
        self.assert_no_wifi_actions()
        self.wifi(400)
        self.wifi(519)
        self.assertEqual(self.wifi_record()["stage"], 1)
        self.assertEqual(self.count("systemctl", "restart", "netplan-wpa-wlan0.service"), 1)
        self.wifi(520)
        self.wifi(699)
        self.assertEqual(self.wifi_record()["stage"], 2)
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)
        self.wifi(700)
        self.wifi(1000)
        self.wifi(100000)
        self.assertEqual(self.wifi_record()["stage"], 3)
        self.assertEqual(self.count("ip", "link", "set", "dev", "wlan0", "down"), 1)
        self.assertEqual(self.count("ip", "link", "set", "dev", "wlan0", "up"), 1)
        self.assertEqual(self.count("networkctl", "renew", "wlan0"), 2)
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 1)
        self.assertEqual(self.budget.read_text().split(), ["v1", "1800000000", "1800000000"])
        self.assertTrue(any("action=reboot result=requested exit_code=0" in log for log in self.logs()))

    def test_failed_link_restore_retries_only_up_and_retains_original_stage(self):
        self.wifi(100)
        self.wifi(400)
        self.config["link_up_exit"] = 8
        self.wifi(520)
        self.assertTrue((self.wifi_dir / "link-needs-up").exists())
        self.assertEqual(self.count("ip", "link", "set", "dev", "wlan0", "up"), 2)
        before = len(self.calls())
        self.wifi(800)
        new_calls = self.calls()[before:]
        self.assertEqual([call for call in new_calls if call[0] == "ip"], [
            ["ip", "link", "set", "dev", "wlan0", "up"],
            ["ip", "link", "set", "dev", "wlan0", "up"],
        ])
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)
        self.config["link_up_exit"] = 0
        self.wifi(815)
        self.assertFalse((self.wifi_dir / "link-needs-up").exists())
        self.assertEqual(self.count("ip", "link", "set", "dev", "wlan0", "down"), 1)
        self.assertEqual((self.wifi_record()["started"], self.wifi_record()["stage"]), (100, 2))
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)
        self.wifi(830)
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 1)

    def test_spent_native_budget_survives_state_schema_cutover(self):
        self.persist_dir.mkdir(parents=True)
        self.budget.write_text("v1 1800000000 1799999900\n")
        self.wifi_dir.mkdir(parents=True)
        self.wifi_state.write_text("v1 2 0 100 90\n")
        self.exhaust_wifi_to_reboot()
        self.wifi(1000)
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)
        self.assertEqual(self.wifi_record()["stage"], 2)
        self.assertEqual(self.budget.read_text().split()[-1], "1799999900")
        self.assertTrue(any("action=reboot result=deferred reason=budget_spent" in log for log in self.logs()))

    def test_budget_validation_and_time_sync_fail_closed(self):
        for now in (100, 400, 520):
            self.wifi(now)
        self.persist_dir.mkdir(parents=True)
        cases = (
            ("broken\n", 1800000000, 0, 0),
            ("v1 1800000000 1799999900\nextra\n", 1800000000, 0, 0),
            ("v1 1800000000 1800000001\n", 1800000000, 0, 0),
            ("v1 1800000000 100\n", 1800000000, 0, 0),
            ("v1 1800000001 0\n", 1800000000, 0, 0),
            ("v1 1800000000 0\n", 100, 0, 0),
            ("v1 1800000000 0\n", 1800000000, 1, 0),
            ("v1 1800000000 0\n", 1800000000, 0, 1),
        )
        for index, (budget, wall, chronyc_exit, sync_exit) in enumerate(cases):
            with self.subTest(index=index):
                self.budget.write_text(budget)
                self.config.update(wall=wall, chronyc_exit=chronyc_exit, sync_exit=sync_exit)
                self.wifi(700 + index * 300)
                self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)
                self.assertEqual(self.wifi_record()["stage"], 2)

    def test_symlink_budget_cannot_be_replaced_or_spent(self):
        self.persist_dir.mkdir(parents=True)
        target = self.root / "budget-target"
        target.write_text("v1 1800000000 0\n")
        self.budget.symlink_to(target)
        self.exhaust_wifi_to_reboot()
        self.assertTrue(self.budget.is_symlink())
        self.assertEqual(target.read_text(), "v1 1800000000 0\n")
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)

    def test_budget_becomes_available_only_after_twenty_four_hours(self):
        self.persist_dir.mkdir(parents=True)
        self.budget.write_text("v1 1800000000 1800000000\n")
        self.config["wall"] = 1800086399
        self.exhaust_wifi_to_reboot()
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 0)
        self.config["wall"] = 1800086400
        self.wifi(1000)
        self.assertEqual(self.count("systemctl", "--no-block", "reboot"), 1)
        self.assertEqual(self.budget.read_text().split()[-1], "1800086400")

    def test_clock_changes_do_not_move_monotonic_recovery_deadline(self):
        self.wifi(100)
        self.config["wall"] = 1
        self.wifi(399)
        self.assertEqual(self.wifi_record()["due"], 400)
        self.config["wall"] = 999999999999
        self.wifi(400)
        self.assertEqual(self.wifi_record()["stage"], 1)
        self.assertEqual(self.count("systemctl", "restart", "netplan-wpa-wlan0.service"), 1)

    def test_missing_wifi_unit_is_not_tailnet_health(self):
        self.config["wifi_unit"] = "not-found"
        self.wifi(100, 3)
        self.health(100)
        self.health(400)
        self.assertTrue(self.ts_state.exists())
        self.assertEqual(self.ts_record()["attempted"], 1)
        self.assert_no_wifi_actions()
        self.assertFalse(any("layer=wifi state=healthy" in log for log in self.logs()))

    def test_unknown_underlay_preserves_incidents_without_action(self):
        self.health(100)
        self.config["wifi_unit"] = "error"
        self.health(400)
        self.assertEqual(self.ts_record()["due"], 400)
        self.assertEqual(self.wifi_record()["started"], 100)
        self.assert_no_wifi_actions()
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 0)

    def test_no_peer_boot_can_start_managed_k3s_without_claiming_tailnet_health(self):
        self.config.update(peers=[], k3s_state="inactive", wifi_unit="not-found")
        self.health(100)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "k3s"), 1)
        self.assertFalse(self.ts_state.exists())
        self.assertTrue(any("layer=tailscale state=unknown" in log for log in self.logs()))
        self.assertFalse(any("layer=tailscale state=healthy" in log for log in self.logs()))
        self.assert_no_wifi_actions()

    def test_managed_flannel_startup_grace_is_preserved(self):
        self.config.update(peers=[], flannel=False, k3s_active_enter=100000000, k3s_agent=True)
        self.health(100)
        self.health(279)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "k3s-agent"), 0)
        self.health(280)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "k3s-agent"), 1)

    def test_authentication_required_does_not_erase_failure_or_change_auth(self):
        self.config["gateway_ping"] = True
        self.health(100)
        self.config["backend"] = "NeedsLogin"
        self.health(400)
        self.assertEqual((self.ts_record()["started"], self.ts_record()["due"]), (100, 400))
        self.assertEqual(self.count("tailscale", "up"), 0)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 0)

    def test_inactive_tailscaled_start_is_bounded_per_incident(self):
        self.config.update(gateway_ping=True, ts_active=False)
        for now in (100, 115, 130):
            self.health(now)
        self.assertEqual(self.count("systemctl", "--no-block", "start", "tailscaled.service"), 1)
        self.health(400)
        self.health(415)
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)

    def test_wifi_action_logs_report_actual_failed_command_result(self):
        self.config.update(supplicant_exit=7, renew_exit=9)
        self.wifi(100)
        self.wifi(400)
        self.wifi(415)
        self.assertEqual(self.count("systemctl", "restart", "netplan-wpa-wlan0.service"), 1)
        self.assertTrue(any("action=restart_supplicant result=failed exit_code=7" in log for log in self.logs()))
        self.assertTrue(any("action=renew result=failed exit_code=9" in log for log in self.logs()))

    def test_fatal_kernel_records_are_warned_once_without_scan_noise(self):
        self.config.update(gateway_ping=True, journal=[
            {"__CURSOR": "cursor-1", "MESSAGE": "brcmfmac: brcmf_set_channel: set chanspec 0xd022 fail, reason -52"},
            {"__CURSOR": "cursor-2", "MESSAGE": "brcmfmac: brcmf_sdio_htclk: HT Avail timeout (1000000): clkctl 0x50"},
        ])
        self.health(100)
        self.health(115)
        warnings = [log for log in self.logs() if "reason=brcmfmac_sdio_fatal" in log]
        self.assertEqual(len(warnings), 1)
        self.assertIn("records=1", warnings[0])
        self.assertNotIn("clkctl", warnings[0])
        self.assertEqual((self.health_dir / "kernel-journal-cursor").read_text().strip(), "cursor-2")
        self.config["journal"].append({
            "__CURSOR": "cursor-3",
            "MESSAGE": "brcmfmac: brcmf_sdiod_set_backplane_window: failed to set window",
        })
        self.health(130)
        self.assertEqual(len([log for log in self.logs() if "reason=brcmfmac_sdio_fatal" in log]), 2)

    def test_normal_kernel_scan_noise_is_not_a_fatal_warning(self):
        self.config.update(gateway_ping=True, journal=[
            {"__CURSOR": "cursor-1", "MESSAGE": "brcmfmac: brcmf_set_channel: set chanspec 0xd022 fail, reason -52"},
            {"__CURSOR": "cursor-2", "MESSAGE": "brcmfmac: brcmf_sdio_bus_rxctl: rxcnt_timeout=1"},
            {"__CURSOR": "cursor-3", "MESSAGE": "unrelated: backplane error"},
        ])
        self.health(100)
        self.assertEqual([log for log in self.logs() if "reason=brcmfmac_sdio_fatal" in log], [])

    def test_kernel_cursor_drains_new_records_in_bounded_batches(self):
        self.config.update(gateway_ping=True, journal=[
            {"__CURSOR": "cursor-0", "MESSAGE": "boot"},
        ])
        self.health(100)
        for index in range(1, 151):
            self.config["journal"].append({
                "__CURSOR": f"cursor-{index}",
                "MESSAGE": "brcmfmac: brcmf_sdio_readframes: RXHEADER FAILED: -110" if index in (50, 125) else "normal",
            })
        self.health(115)
        self.assertEqual((self.health_dir / "kernel-journal-cursor").read_text().strip(), "cursor-100")
        self.assertEqual(len([log for log in self.logs() if "reason=brcmfmac_sdio_fatal" in log]), 1)
        self.health(130)
        self.health(145)
        self.assertEqual((self.health_dir / "kernel-journal-cursor").read_text().strip(), "cursor-150")
        self.assertEqual(len([log for log in self.logs() if "reason=brcmfmac_sdio_fatal" in log]), 2)

    def test_journal_failure_cannot_abort_recovery_or_reset_incidents(self):
        self.config.update(gateway_ping=True, journal=[
            {"__CURSOR": "cursor-1", "MESSAGE": "boot"},
        ])
        self.health(100)
        self.config.update(journal_exit=1)
        self.config["journal"].append({
            "__CURSOR": "cursor-2",
            "MESSAGE": "brcmfmac: brcmf_sdio_htclk: HT Avail timeout",
        })
        self.health(400)
        self.assertEqual((self.ts_record()["started"], self.ts_record()["attempted"]), (100, 1))
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)
        self.assertEqual((self.health_dir / "kernel-journal-cursor").read_text().strip(), "cursor-1")
        self.config["journal_exit"] = 0
        self.health(415)
        self.assertEqual(len([log for log in self.logs() if "reason=brcmfmac_sdio_fatal" in log]), 1)

    def test_logger_failure_cannot_abort_recovery_or_repeat_kernel_warning(self):
        self.config.update(gateway_ping=True, logger_exit=7, journal=[
            {"__CURSOR": "cursor-1", "MESSAGE": "brcmfmac: brcmf_sdio_readframes: RXHEADER FAILED: -110"},
        ])
        self.health(100)
        self.health(400)
        self.health(415)
        self.assertEqual((self.ts_record()["started"], self.ts_record()["attempted"]), (100, 1))
        self.assertEqual(self.count("systemctl", "--no-block", "restart", "tailscaled.service"), 1)
        self.assertEqual(len([log for log in self.logs() if "reason=brcmfmac_sdio_fatal" in log]), 1)

    def test_prolonged_tailnet_warning_is_once_per_incident_and_stable_recovery_is_logged(self):
        self.config["gateway_ping"] = True
        for now in (100, 399):
            self.health(now)
        self.assertEqual([log for log in self.logs() if "event=diagnostic_warning layer=tailscale" in log], [])
        self.health(400)
        self.health(415)
        warnings = [log for log in self.logs() if "event=diagnostic_warning layer=tailscale" in log]
        self.assertEqual(len(warnings), 1)
        self.assertIn("elapsed_seconds=300", warnings[0])
        self.assertIn("reason=peer_unreachable", warnings[0])
        self.config["ts_ping"] = True
        self.health(430)
        self.config["ts_ping"] = False
        self.health(445)
        self.config["ts_ping"] = True
        for now in (460, 475, 490):
            self.health(now)
        recoveries = [log for log in self.logs() if "layer=tailscale state=healthy" in log and "reason=sustained_recovery" in log]
        self.assertEqual(len(recoveries), 1)
        self.assertIn("elapsed_seconds=390", recoveries[0])
        self.config["ts_ping"] = False
        self.health(500)
        self.health(800)
        self.assertEqual(len([log for log in self.logs() if "event=diagnostic_warning layer=tailscale" in log]), 2)

    def test_busy_recovery_lock_performs_no_probe_or_action(self):
        self.config["lock_exit"] = 1
        self.wifi(100, 2)
        self.health(100)
        self.assertEqual(self.calls("tailscale"), [])
        self.assertEqual(self.calls("ip"), [])
        self.assert_no_wifi_actions()

    def test_health_defaults_to_sibling_python_wifi_helper(self):
        del self.env["NODE_HEALTH_WIFI_HELPER"]
        self.config["gateway_ping"] = True
        self.health(100)
        self.assertEqual(self.count("timeout", "--kill-after=8s", "20s", str(WIFI), "--bounded"), 1)
        self.assertTrue(any("layer=wifi state=healthy probe=gateway_icmp" in log for log in self.logs()))

    def test_watchdog_loader_tries_modules_in_order_until_device_appears(self):
        self.config.update(modprobe={"hpwdt": 0, "i6300esb": 0}, watchdog_module="i6300esb",
                           watchdog_device=str(self.device))
        self.watchdog(0)
        self.assertEqual([call for call in self.calls() if call[0] != "timeout"], [
            ["modprobe", "iTCO_wdt"],
            ["modprobe", "hpwdt"],
            ["udevadm", "settle", "--timeout=5"],
            ["modprobe", "i6300esb"],
            ["udevadm", "settle", "--timeout=5"],
            ["logger", "-t", "watchdog-load", "loaded i6300esb"],
            ["systemctl", "daemon-reexec"],
        ])
        self.assertEqual(self.count("timeout", "8s", "modprobe", "iTCO_wdt"), 1)
        self.assertEqual(self.count("timeout", "6s", "udevadm", "settle", "--timeout=5"), 2)
        self.assertEqual(self.count("timeout", "15s", "systemctl", "daemon-reexec"), 1)
        before = len(self.calls())
        self.watchdog(0)
        self.assertEqual(len(self.calls()), before)

    def test_watchdog_loader_without_reexec_skips_daemon_reexec(self):
        self.config.update(modprobe={"softdog": 0}, watchdog_module="softdog", watchdog_device=str(self.device))
        self.watchdog(0, "--no-reexec")
        self.assertEqual(self.logs(), ["loaded softdog"])
        self.assertEqual(len(self.calls("modprobe")), 4)
        self.assertEqual(self.calls("systemctl"), [])

    def test_watchdog_loader_failures_are_reported(self):
        self.assertEqual(self.watchdog(1, "--bogus"), "Error: unsupported watchdog loader argument\n")
        self.assertEqual(self.watchdog(1, "--no-reexec", "extra"), "Error: too many watchdog loader arguments\n")
        self.assertFalse(self.calls_path.exists())
        self.watchdog(1)
        self.assertEqual(self.logs(), ["no watchdog device available"])
        self.assertEqual(len(self.calls("modprobe")), 4)
        self.assertEqual(self.calls("udevadm"), [])
        self.assertEqual(self.calls("systemctl"), [])
        self.config["logger_exit"] = 7
        self.watchdog(7)
        self.config.update(logger_exit=0, modprobe={"softdog": 0}, watchdog_module="softdog",
                           watchdog_device=str(self.device), reexec_exit=5)
        self.watchdog(5)


if __name__ == "__main__":
    unittest.main()
