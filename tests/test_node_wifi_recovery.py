#!/usr/bin/env python3
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


FILE_COMMANDS = {"cat", "chmod", "cmp", "install", "mkdir", "mktemp", "mv", "rm", "touch"}
COMMANDS = FILE_COMMANDS | {
    "bridge", "chronyc", "curl", "date", "flock", "ip", "iw", "logger",
    "networkctl", "node-wifi-recovery.sh", "ping", "sync", "systemctl",
    "tailscale", "timeout",
}
EPOCH = 1800000000
WIFI_UNIT = "netplan-wpa-wlan0.service"
STAGE_ONE = [
    ("systemctl", "restart", WIFI_UNIT),
    ("networkctl", "reconfigure", "wlan0"),
    ("networkctl", "renew", "wlan0"),
]
STAGE_TWO = [
    ("ip", "link", "set", "dev", "wlan0", "down"),
    ("ip", "link", "set", "dev", "wlan0", "up"),
    ("networkctl", "reconfigure", "wlan0"),
    ("networkctl", "renew", "wlan0"),
]
REBOOT = ("systemctl", "reboot")


def read_optional(path):
    try:
        return Path(path).read_text()
    except (OSError, UnicodeError):
        return None


def seconds(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([smh]?)", value)
    if not match:
        raise ValueError(value)
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def mock_timeout(args, scenario):
    kill_after = None
    while args and args[0].startswith("-"):
        option = args.pop(0)
        if option.startswith("--kill-after="):
            kill_after = seconds(option.split("=", 1)[1])
        elif option in ("--kill-after", "-k"):
            kill_after = seconds(args.pop(0))
        elif option != "--":
            return 125
    duration = seconds(args.pop(0))
    invocation = [Path(args[0]).name, *args[1:]]
    injected_hang = any(invocation[:len(prefix)] == prefix for prefix in scenario.get("hangs", []))
    scale = scenario.get("timeout_scale", 1) if injected_hang else 1
    if "--bounded" in args:
        scale = scenario.get("outer_timeout_scale", 1)
    process = subprocess.Popen(args, start_new_session=True)
    try:
        result = process.wait(timeout=duration * scale)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=(kill_after or 1) * scale)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return 124
    if scenario.get("cancel_after_down") and [Path(args[0]).name, *args[1:]] == [
        "ip", "link", "set", "dev", "wlan0", "down"
    ]:
        os.kill(os.getppid(), signal.SIGTERM)
    return result if result >= 0 else 128 - result




def mock_files(command, args, root, scenario):
    options = {
        "cat": set(), "chmod": set(), "cmp": {"-s"}, "install": {"-d"},
        "mkdir": {"-p"}, "mktemp": set(), "mv": {"-f"}, "rm": {"-f"}, "touch": set(),
    }
    remaining = iter(args)
    if command == "chmod" and not re.fullmatch(r"[0-7]{3,4}", next(remaining, "")):
        return 1
    paths = []
    parsing_options = True
    for value in remaining:
        if parsing_options and value == "--":
            parsing_options = False
        elif parsing_options and value in ("-m", "--mode") and command in ("install", "mkdir"):
            if not re.fullmatch(r"[0-7]{3,4}", next(remaining, "")):
                return 1
        elif parsing_options and value.startswith("-"):
            if value not in options[command]:
                return 1
        else:
            paths.append(Path(value))
    try:
        if not paths or any(not path.resolve().is_relative_to(root.resolve()) for path in paths):
            return 1
    except (OSError, RuntimeError, ValueError):
        return 1
    if command == "mv" and scenario.get("fail_budget_write") and str(paths[-1]) == os.environ["NODE_WIFI_PERSIST_DIR"] + "/reboot-budget":
        return 1
    executable = json.loads(os.environ["NODE_WIFI_TEST_REAL_COMMANDS"])[command]
    os.execv(executable, [executable, *args])


def mock_command(command, args):
    root = Path(os.environ["NODE_WIFI_TEST_ROOT"])
    scenario = json.loads((root / "scenario.json").read_text())
    runtime = Path(os.environ["NODE_WIFI_RUNTIME_DIR"]) / "recovery"
    budget = Path(os.environ["NODE_WIFI_PERSIST_DIR"]) / "reboot-budget"
    event = {
        "command": command,
        "args": args,
        "runtime": read_optional(runtime),
        "budget": read_optional(budget),
        "admin_state": read_optional(root / "admin-state"),
        "link_cleanup": read_optional(Path(os.environ["NODE_WIFI_RUNTIME_DIR"]) / "link-needs-up"),
    }
    if command == "sync" and args:
        event["target_data"] = read_optional(args[-1])
    with (root / "journal.jsonl").open("a") as journal:
        journal.write(json.dumps(event) + "\n")
    invocation = [command, *args]
    if invocation == ["ip", "link", "set", "dev", "wlan0", "down"] and (Path(os.environ["NODE_WIFI_SYSFS_DIR"]) / "wlan0").is_dir():
        (root / "admin-state").write_text("down")
    for prefix in scenario.get("hangs", []):
        if invocation[:len(prefix)] == prefix:
            time.sleep(60)
    for prefix in scenario.get("failures", []):
        if invocation[:len(prefix)] == prefix:
            return 1
    if command == "timeout":
        return mock_timeout(args.copy(), scenario)
    if command in FILE_COMMANDS:
        return mock_files(command, args, root, scenario)
    if command == "sync":
        target = Path(args[-1])
        failure = scenario.get("fail_sync")
        return int(failure == "all" or failure == ("directory" if target.is_dir() else "file"))
    if command == "flock":
        try:
            fcntl.flock(int(args[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 0
        except OSError:
            return 1
    if command == "logger":
        return 0
    if command == "node-wifi-recovery.sh":
        return scenario.get("helper_status", 0)
    if command == "date":
        print(scenario["epoch"])
        return 0
    if command == "chronyc":
        if not scenario.get("clock_trusted", True) or not args or args[0] != "waitsync":
            return 1
        for index, setting in ((2, "clock_correction"), (3, "clock_skew")):
            limit = float(args[index]) if len(args) > index else 0
            observed = abs(float(scenario.get(setting, 0)))
            if not math.isfinite(limit) or limit < 0:
                return 1
            if limit > 0 and (not math.isfinite(observed) or observed > limit):
                return 1
        return 0
    if command == "systemctl":
        if args == ["show", WIFI_UNIT, "--property=LoadState", "--value"]:
            print(scenario.get("unit_state", "loaded" if scenario.get("configured", True) else "not-found"))
            return scenario.get("gate_status", 0)
        elif args[:1] == ["is-enabled"]:
            return int(not scenario.get("k3s_agent_enabled", False))
        elif args[:1] == ["is-active"]:
            if args[-1] == "tailscaled.service":
                return int(not scenario.get("ts_active", True))
            print(scenario.get("k3s_state", "inactive"))
            return int(scenario.get("k3s_state", "inactive") != "active")
        elif "--property=ActiveEnterTimestampMonotonic" in args:
            print("1")
        return 0
    if command == "networkctl":
        return 0
    if command == "iw":
        if scenario.get("associated", False):
            print("Connected to 00:11:22:33:44:55 (on wlan0)")
            return 0
        print("Not connected.")
        return 1
    if command == "ip":
        if args == ["-j", "-4", "route", "show", "default"]:
            print(json.dumps(scenario["routes"]))
        elif args[:5] == ["-j", "-4", "address", "show", "dev"]:
            interface = args[5]
            addresses = scenario["addresses"].get(interface, [])
            print(json.dumps([{
                "ifname": interface,
                "addr_info": [{"family": "inet", "scope": "global", "local": address} for address in addresses],
            }]))
        elif args[:5] == ["-j", "-4", "neigh", "show", "to"]:
            gateway = args[5]
            interface = args[args.index("dev") + 1]
            states = scenario.get("neighbors", {}).get(interface + ":" + gateway, [])
            print(json.dumps([{"dst": gateway, "dev": interface, "state": states}] if states else []))
        elif args[:3] == ["link", "set", "dev"]:
            if not (Path(os.environ["NODE_WIFI_SYSFS_DIR"]) / args[3]).is_dir():
                return 1
            if args[-1] == "up":
                (root / "admin-state").write_text("up")
            return 0
        elif args[:2] == ["link", "show"]:
            return int(not scenario.get("flannel", False))
        else:
            return 2
        return 0
    if command == "ping":
        interface = args[args.index("-I") + 1] if "-I" in args else ""
        return int(not scenario.get("ping", {}).get(interface + ":" + args[-1], False))
    if command == "curl":
        interface = args[args.index("--interface") + 1] if "--interface" in args else ""
        return int(not scenario.get("http", {}).get(interface, False))
    if command == "tailscale":
        if args[:1] == ["status"]:
            print(json.dumps({"BackendState": scenario.get("ts_backend", "Running")}))
            return 0
        if args[:1] == ["ping"]:
            return int(not scenario.get("ts_ping", False))
        return 0
    if command == "bridge":
        print(json.dumps([{"dst": peer} for peer in scenario.get("peers", ["100.64.0.2"])]))
        return 0
    return 127


def mutations(events):
    result = []
    for event in events:
        command, args = event["command"], event["args"]
        if command == "systemctl":
            args = [arg for arg in args if arg not in ("--no-block", "--quiet")]
            if args and args[0] not in ("show", "is-enabled", "is-active"):
                result.append((command, *args))
        elif command == "networkctl":
            result.append((command, *args))
        elif command == "ip" and args[:2] == ["link", "set"]:
            result.append((command, *args))
        elif command == "tailscale" and args and args[0] in ("up", "down"):
            result.append((command, *args))
    return result


if __name__ == "__main__" and Path(sys.argv[0]).name in COMMANDS:
    sys.exit(mock_command(Path(sys.argv[0]).name, sys.argv[1:]))

import unittest


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="node-wifi-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.persist = self.root / "persist"
        self.sysfs = self.root / "sysfs"
        self.health_run = self.root / "health"
        self.uptime = self.root / "uptime"
        self.bin = self.root / "bin"
        for directory in (self.runtime, self.persist, self.sysfs, self.health_run, self.bin):
            directory.mkdir()
        self.interface("wlan0", wireless=True)
        self.admin_state = self.root / "admin-state"
        self.admin_state.write_text("up")
        real_commands = {command: shutil.which(command) for command in FILE_COMMANDS | {"jq"}}
        for command, path in real_commands.items():
            self.assertIsNotNone(path, f"required executable not found: {command}")
        executable = Path(__file__).resolve()
        for command in COMMANDS:
            (self.bin / command).symlink_to(executable)
        (self.bin / "jq").symlink_to(real_commands["jq"])
        (self.bin / "python3").symlink_to(sys.executable)
        self.bash = shutil.which("bash")
        self.assertIsNotNone(self.bash)
        (self.bin / "bash").symlink_to(self.bash)
        scripts = executable.parents[1] / "rootfs/common/usr/local/bin"
        self.helper = scripts / "node-wifi-recovery.sh"
        self.health = scripts / "node-health-check.sh"
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin) + ":/usr/bin:/bin",
            "NODE_WIFI_RUNTIME_DIR": str(self.runtime),
            "NODE_WIFI_PERSIST_DIR": str(self.persist),
            "NODE_WIFI_SYSFS_DIR": str(self.sysfs),
            "NODE_WIFI_UPTIME_FILE": str(self.uptime),
            "NODE_HEALTH_RUN_DIR": str(self.health_run),
            "NODE_WIFI_TEST_ROOT": str(self.root),
            "NODE_WIFI_TEST_REAL_COMMANDS": json.dumps(real_commands),
            "LC_ALL": "C",
        })
        self.scenario = {
            "configured": True,
            "associated": False,
            "addresses": {"wlan0": ["192.0.2.10"]},
            "routes": [{"dst": "default", "dev": "wlan0", "gateway": "192.0.2.1"}],
            "epoch": EPOCH,
        }
        self.now = 0

    @property
    def recovery(self):
        return self.runtime / "recovery"

    @property
    def budget(self):
        return self.persist / "reboot-budget"

    @property
    def deadline(self):
        return self.health_run / "node-health.tailscale-failure-deadline"

    @property
    def attempted(self):
        return self.health_run / "node-health.tailscale-recovery-attempted"

    def interface(self, name, wireless=False):
        path = self.sysfs / name
        path.mkdir(exist_ok=True)
        if wireless:
            (path / "wireless").mkdir(exist_ok=True)

    def events(self):
        journal = self.root / "journal.jsonl"
        return [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []

    def actions(self):
        return mutations(self.events())

    def invoke(self, script, now=None, epoch=None, expected=1):
        if now is not None:
            self.now = now
        if epoch is not None:
            self.scenario["epoch"] = epoch
        self.uptime.write_text(str(self.now) + ".00 0.00\n")
        (self.root / "scenario.json").write_text(json.dumps(self.scenario))
        result = subprocess.run(
            [self.bash, str(script)], env=self.env, capture_output=True, text=True, timeout=35,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def wifi(self, now=None, epoch=None, expected=1):
        return self.invoke(self.helper, now, epoch, expected)

    def health_check(self, epoch=None):
        return self.invoke(self.health, epoch=epoch, expected=0)

    def healthy_wifi(self):
        self.scenario["associated"] = True
        self.scenario["ping"] = {"wlan0:192.0.2.1": True}

    def exhaust(self, start=0, epoch=None):
        for offset in (0, 300, 420, 600):
            self.wifi(start + offset, epoch=epoch)

    def stage_two_ready(self):
        self.wifi(0)
        self.wifi(300)

    def seed_budget(self, record):
        self.budget.write_text(record)


class WifiRecoveryTests(RecoveryFixture):
    def tearDown(self):
        events = self.events()
        allowed = set(STAGE_ONE + STAGE_TWO + [REBOOT])
        for action in mutations(events):
            self.assertIn(action, allowed)
        super().tearDown()

    def test_unconfigured_wifi_skips_and_clears_runtime(self):
        self.wifi(0)
        self.assertTrue(self.recovery.exists())
        self.scenario["configured"] = False
        shutil.rmtree(self.sysfs / "wlan0")
        self.wifi(300, expected=0)
        self.assertFalse(self.recovery.exists())
        self.assertEqual(self.actions(), [])

    def test_not_found_gate_skips_even_when_systemctl_reports_failure(self):
        self.scenario.update({"unit_state": "not-found", "gate_status": 1})
        self.wifi(0, expected=0)
        self.assertFalse(self.recovery.exists())
        self.assertEqual(self.actions(), [])

    def test_unknown_or_empty_wifi_gate_fails_closed(self):
        for state in ("", "error"):
            with self.subTest(state=state):
                self.scenario["unit_state"] = state
                self.wifi(0)
                self.assertFalse(self.recovery.exists())
                self.assertEqual(self.actions(), [])

    def test_healthy_wifi_clears_runtime_without_internet_or_tailnet(self):
        self.wifi(0)
        self.healthy_wifi()
        self.scenario.update({"http": {}, "ts_active": False, "ts_ping": False})
        self.wifi(300, expected=0)
        self.assertFalse(self.recovery.exists())
        self.assertEqual(self.actions(), [])
        self.assertFalse(any(event["command"] == "tailscale" for event in self.events()))

    def test_reachable_neighbor_accepts_icmp_filtered_gateway(self):
        self.scenario.update({"associated": True, "neighbors": {"wlan0:192.0.2.1": ["REACHABLE"]}})
        self.wifi(0, expected=0)
        self.assertEqual(self.actions(), [])

    def test_filtered_gateway_with_usable_bound_http_keeps_wifi_healthy(self):
        self.scenario.update({
            "associated": True,
            "neighbors": {"wlan0:192.0.2.1": ["STALE"]},
            "http": {"wlan0": True},
        })
        self.wifi(0, expected=0)
        self.assertEqual(self.actions(), [])

    def test_association_ipv4_and_default_route_are_required(self):
        for missing in ("association", "ipv4", "route", "usable_ipv4"):
            with self.subTest(missing=missing):
                self.healthy_wifi()
                self.scenario["addresses"]["wlan0"] = ["192.0.2.10"]
                self.scenario["routes"] = [{"dst": "default", "dev": "wlan0", "gateway": "192.0.2.1"}]
                if missing == "association":
                    self.scenario["associated"] = False
                elif missing == "ipv4":
                    self.scenario["addresses"]["wlan0"] = []
                elif missing == "route":
                    self.scenario["routes"] = []
                else:
                    self.scenario["addresses"]["wlan0"] = ["169.254.1.2", "127.0.0.1"]
                self.recovery.unlink(missing_ok=True)
                self.wifi(0)
                self.assertTrue(self.recovery.exists())
                self.assertEqual(self.actions(), [])

    def test_transient_failure_resets_the_full_initial_delay(self):
        self.wifi(0)
        self.wifi(299)
        self.assertEqual(self.actions(), [])
        self.healthy_wifi()
        self.wifi(299, expected=0)
        self.assertFalse(self.recovery.exists())
        self.scenario["associated"] = False
        self.wifi(300)
        self.wifi(599)
        self.assertEqual(self.actions(), [])
        self.wifi(600)
        self.assertEqual(self.actions(), STAGE_ONE)

    def test_stage_boundaries_are_monotonic_and_execute_once(self):
        self.wifi(100)
        initial = self.recovery.read_text()
        self.wifi(399, epoch=EPOCH + 1000000)
        self.assertEqual(self.actions(), [])
        self.wifi(400, epoch=EPOCH)
        first = self.recovery.read_text()
        self.assertNotEqual(first, initial)
        first_event = next(event for event in self.events() if mutations([event]))
        self.assertEqual(first_event["runtime"], first)
        self.wifi(400)
        self.wifi(519)
        self.assertEqual(self.actions(), STAGE_ONE)
        before_second = len(self.events())
        self.wifi(520)
        second = self.recovery.read_text()
        self.assertNotEqual(second, first)
        second_event = next(event for event in self.events()[before_second:] if mutations([event]))
        self.assertEqual(second_event["runtime"], second)
        self.wifi(520)
        self.wifi(699)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO)
        self.wifi(700)
        self.wifi(700)
        self.wifi(1000)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO + [REBOOT])
        self.assertEqual(self.budget.read_text(), f"v1 {EPOCH} {EPOCH}\n")
        reboots = [event for event in self.events() if "reboot" in event["args"] and event["command"] == "systemctl"]
        self.assertEqual(reboots[0]["args"], ["--no-block", "reboot"])
        self.assertEqual(reboots[0]["budget"], f"v1 {EPOCH} {EPOCH}\n")

    def test_budget_reservation_is_synced_and_renamed_before_reboot(self):
        self.exhaust()
        events = self.events()
        file_sync = next(index for index, event in enumerate(events) if event["command"] == "sync" and event.get("target_data"))
        rename = next(index for index, event in enumerate(events) if event["command"] == "mv" and event["args"][-1] == str(self.budget))
        dir_sync = next(index for index, event in enumerate(events) if event["command"] == "sync" and event["args"][-1] == str(self.persist))
        reboot = next(index for index, event in enumerate(events) if event["command"] == "systemctl" and "reboot" in event["args"])
        self.assertLess(file_sync, rename)
        self.assertLess(rename, dir_sync)
        self.assertLess(dir_sync, reboot)

    def test_working_wired_default_guards_missing_disconnected_or_stuck_wifi(self):
        for wireless in ("missing", "disconnected", "stuck"):
            with self.subTest(wireless=wireless):
                self.scenario.pop("hangs", None)
                self.scenario.pop("timeout_scale", None)
                self.scenario["routes"] = [{"dst": "default", "dev": "wlan0", "gateway": "192.0.2.1"}]
                self.scenario["ping"] = {}
                self.interface("wlan0", wireless=True)
                self.wifi(0)
                self.interface("enp1s0")
                self.scenario["routes"].append({"dst": "default", "dev": "enp1s0", "gateway": "198.51.100.1"})
                self.scenario["addresses"]["enp1s0"] = ["198.51.100.10"]
                self.scenario["ping"] = {"enp1s0:198.51.100.1": True}
                if wireless == "missing":
                    shutil.rmtree(self.sysfs / "wlan0")
                elif wireless == "stuck":
                    self.scenario.update({"hangs": [["iw", "dev", "wlan0", "link"]], "timeout_scale": 0.2})
                self.wifi(300, expected=0)
                self.assertFalse(self.recovery.exists())
                self.assertEqual(self.actions(), [])

    def test_wireless_e_interface_is_not_treated_as_wired_guard(self):
        self.interface("eth0", wireless=True)
        self.scenario["addresses"]["eth0"] = ["198.51.100.10"]
        self.scenario["routes"].append({"dst": "default", "dev": "eth0", "gateway": "198.51.100.1"})
        self.scenario["ping"] = {"eth0:198.51.100.1": True}
        self.wifi(0)
        self.wifi(300)
        self.assertEqual(self.actions(), STAGE_ONE)

    def test_missing_configured_wlan_can_recover_through_final_reboot(self):
        shutil.rmtree(self.sysfs / "wlan0")
        self.exhaust()
        actions = self.actions()
        self.assertEqual(actions.count(("systemctl", "restart", WIFI_UNIT)), 1)
        self.assertNotIn(STAGE_TWO[0], actions)
        self.assertEqual(actions.count(REBOOT), 1)

    def test_recovery_after_link_stage_clears_only_runtime(self):
        self.seed_budget(f"v1 {EPOCH} {EPOCH - 100}\n")
        self.stage_two_ready()
        self.wifi(420)
        previous = self.budget.read_text()
        self.healthy_wifi()
        self.wifi(600, expected=0)
        self.assertFalse(self.recovery.exists())
        self.assertEqual(self.budget.read_text(), previous)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO)

    def test_reboot_cap_survives_runtime_deletion_and_uptime_reset(self):
        self.exhaust()
        shutil.rmtree(self.runtime)
        self.exhaust(epoch=EPOCH + 600)
        self.assertEqual(self.actions().count(REBOOT), 1)
        self.assertEqual(self.budget.read_text(), f"v1 {EPOCH + 600} {EPOCH}\n")
        actions = self.actions()
        clock_calls = sum(event["command"] == "chronyc" for event in self.events())
        self.wifi(899, epoch=EPOCH + 86400)
        self.assertEqual(self.actions(), actions)
        self.assertEqual(sum(event["command"] == "chronyc" for event in self.events()), clock_calls)
        self.wifi(900, epoch=EPOCH + 86400)
        self.assertEqual(self.actions(), actions + [REBOOT])
        self.assertEqual(self.budget.read_text(), f"v1 {EPOCH + 86400} {EPOCH + 86400}\n")

    def test_reboot_is_denied_until_exact_24_hour_boundary(self):
        self.seed_budget(f"v1 {EPOCH} {EPOCH}\n")
        self.exhaust(epoch=EPOCH + 86399)
        self.assertNotIn(REBOOT, self.actions())
        actions = self.actions()
        self.wifi(899, epoch=EPOCH + 86400)
        self.assertEqual(self.actions(), actions)
        self.wifi(900, epoch=EPOCH + 86400)
        self.assertEqual(self.actions(), actions + [REBOOT])

    def test_fleeting_healthy_recovery_does_not_replenish_budget(self):
        self.exhaust()
        reserved = self.budget.read_text()
        self.healthy_wifi()
        self.wifi(601, epoch=EPOCH + 1, expected=0)
        self.assertFalse(self.recovery.exists())
        self.assertEqual(self.budget.read_text(), reserved)
        self.scenario["associated"] = False
        self.exhaust(start=602, epoch=EPOCH + 602)
        self.assertEqual(self.actions().count(REBOOT), 1)
        self.assertEqual(self.budget.read_text(), f"v1 {EPOCH + 602} {EPOCH}\n")

    def test_clock_rollback_denies_reboot_and_retries_only_final_decision(self):
        previous = f"v1 {EPOCH + 100} {EPOCH - 90000}\n"
        self.seed_budget(previous)
        self.exhaust(epoch=EPOCH)
        self.assertNotIn(REBOOT, self.actions())
        self.assertEqual(self.budget.read_text(), previous)
        before = self.actions()
        clocks = sum(event["command"] == "chronyc" for event in self.events())
        self.wifi(899, epoch=EPOCH + 200)
        self.assertEqual(self.actions(), before)
        self.assertEqual(sum(event["command"] == "chronyc" for event in self.events()), clocks)
        self.wifi(900, epoch=EPOCH + 200)
        self.assertEqual(self.actions(), before + [REBOOT])

    def test_corrupt_and_multiline_budget_records_deny_reboot(self):
        records = (
            "garbage\n",
            f"v1 {EPOCH} 0",
            f"v1 {EPOCH} 0 extra\n",
            f"v1 {EPOCH} 0\nv1 {EPOCH} 0\n",
            f"v1 {EPOCH} 0\n\n",
            "v1 0 0\n",
            f"v1 {EPOCH} {EPOCH + 1}\n",
            "v1 999999999999999999999 0\n",
        )
        for record in records:
            with self.subTest(record=record):
                self.recovery.unlink(missing_ok=True)
                self.seed_budget(record)
                self.exhaust()
                self.assertNotIn(REBOOT, self.actions())
                self.assertEqual(self.budget.read_text(), record)

    def test_nonregular_and_symlink_budget_files_deny_reboot(self):
        target = self.persist / "actual-budget"
        target.write_text(f"v1 {EPOCH} 0\n")
        for kind in ("directory", "symlink", "dangling_symlink"):
            with self.subTest(kind=kind):
                self.recovery.unlink(missing_ok=True)
                if self.budget.is_dir() and not self.budget.is_symlink():
                    self.budget.rmdir()
                else:
                    self.budget.unlink(missing_ok=True)
                if kind == "directory":
                    self.budget.mkdir()
                else:
                    self.budget.symlink_to(target if kind == "symlink" else self.persist / "missing")
                self.exhaust()
                self.assertNotIn(REBOOT, self.actions())
                self.assertTrue(self.budget.is_dir() if kind == "directory" else self.budget.is_symlink())

    def test_untrusted_clock_denies_reboot(self):
        self.scenario["clock_trusted"] = False
        self.exhaust()
        self.assertNotIn(REBOOT, self.actions())
        self.assertFalse(self.budget.exists())
        self.assertFalse(any(event["command"] == "date" for event in self.events()))

    def test_synchronized_clock_with_large_remaining_correction_denies_reboot(self):
        self.scenario.update({"clock_trusted": True, "clock_correction": 172800})
        self.exhaust()
        self.assertNotIn(REBOOT, self.actions())
        self.assertFalse(self.budget.exists())
        self.scenario["clock_correction"] = 0
        self.wifi(900)
        self.assertEqual(self.actions().count(REBOOT), 1)

    def test_invalid_wall_clocks_deny_reboot(self):
        for epoch in (0, 1704067199, -1, "invalid", 999999999999999999999):
            with self.subTest(epoch=epoch):
                self.recovery.unlink(missing_ok=True)
                self.exhaust(epoch=epoch)
                self.assertNotIn(REBOOT, self.actions())
                self.assertFalse(self.budget.exists())

    def test_persistent_directory_failure_denies_reboot(self):
        self.stage_two_ready()
        self.wifi(420)
        self.persist.rmdir()
        self.persist.write_text("not a directory")
        self.wifi(600)
        self.wifi(900)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO)

    def test_atomic_budget_rename_failure_denies_reboot(self):
        self.scenario["fail_budget_write"] = True
        self.exhaust()
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO)
        self.assertFalse(self.budget.exists())
        self.assertEqual(list(self.persist.iterdir()), [])

    def test_file_or_directory_sync_failure_denies_reboot(self):
        for target in ("file", "directory"):
            with self.subTest(target=target):
                self.recovery.unlink(missing_ok=True)
                self.budget.unlink(missing_ok=True)
                self.scenario["fail_sync"] = target
                self.exhaust()
                self.assertNotIn(REBOOT, self.actions())
                if target == "file":
                    self.assertFalse(self.budget.exists())
                else:
                    self.assertEqual(self.budget.read_text(), f"v1 {EPOCH} {EPOCH}\n")

    def test_failed_directory_sync_does_not_reuse_the_reserved_budget(self):
        self.scenario["fail_sync"] = "directory"
        self.exhaust()
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO)
        self.scenario.pop("fail_sync")
        self.wifi(900, epoch=EPOCH + 300)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO)
        self.assertEqual(self.budget.read_text(), f"v1 {EPOCH + 300} {EPOCH}\n")

    def test_failed_or_hung_first_stage_actions_do_not_repeat(self):
        for mode in ("failures", "hangs"):
            for action in STAGE_ONE:
                with self.subTest(mode=mode, action=action):
                    self.scenario.pop("failures", None)
                    self.scenario.pop("hangs", None)
                    self.recovery.unlink(missing_ok=True)
                    before = len(self.actions())
                    self.scenario[mode] = [list(action)]
                    self.scenario["timeout_scale"] = 0.2
                    self.wifi(0)
                    initial = self.recovery.read_text()
                    self.wifi(300)
                    self.assertNotEqual(self.recovery.read_text(), initial)
                    self.wifi(301)
                    self.wifi(419)
                    self.assertEqual(self.actions()[before:], STAGE_ONE)

    def test_failed_or_hung_link_down_always_attempts_up_without_repeating(self):
        for mode in ("failures", "hangs"):
            with self.subTest(mode=mode):
                self.scenario.pop("failures", None)
                self.scenario.pop("hangs", None)
                self.recovery.unlink(missing_ok=True)
                self.stage_two_ready()
                before = len(self.actions())
                previous = self.recovery.read_text()
                self.scenario[mode] = [["ip", "link", "set", "dev", "wlan0", "down"]]
                self.scenario["timeout_scale"] = 0.2
                self.wifi(420)
                self.assertNotEqual(self.recovery.read_text(), previous)
                self.wifi(421)
                self.assertEqual(self.actions()[before:], STAGE_TWO)

    def test_failed_or_hung_link_up_is_retried_on_later_invocations_without_redown(self):
        for mode in ("failures", "hangs"):
            with self.subTest(mode=mode):
                self.scenario.pop("failures", None)
                self.scenario.pop("hangs", None)
                self.recovery.unlink(missing_ok=True)
                self.stage_two_ready()
                self.scenario[mode] = [["ip", "link", "set", "dev", "wlan0", "up"]]
                self.scenario["timeout_scale"] = 0.2
                self.wifi(420)
                self.assertEqual(self.admin_state.read_text(), "down")
                self.assertTrue((self.runtime / "link-needs-up").exists())
                actions = self.actions()
                down_count = actions.count(STAGE_TWO[0])
                down = next(event for event in self.events() if event["command"] == "ip" and event["args"][-1] == "down")
                self.assertIsNotNone(down["link_cleanup"])
                self.scenario.pop(mode)
                self.wifi(421)
                self.assertEqual(self.admin_state.read_text(), "up")
                self.assertFalse((self.runtime / "link-needs-up").exists())
                self.assertGreater(len(self.actions()), len(actions))
                self.assertTrue(all(action == STAGE_TWO[1] for action in self.actions()[len(actions):]))
                self.assertEqual(self.actions().count(STAGE_TWO[0]), down_count)

    def test_permanently_failed_up_cleanup_does_not_prevent_one_budgeted_reboot(self):
        self.scenario["failures"] = [["ip", "link", "set", "dev", "wlan0", "up"]]
        self.exhaust()
        self.assertEqual(self.admin_state.read_text(), "down")
        self.assertTrue((self.runtime / "link-needs-up").exists())
        self.assertEqual(self.actions().count(STAGE_TWO[0]), 1)
        self.assertEqual(self.actions().count(REBOOT), 1)
        up_count = self.actions().count(STAGE_TWO[1])
        self.wifi(601)
        self.wifi(900)
        self.assertGreater(self.actions().count(STAGE_TWO[1]), up_count)
        self.assertEqual(self.actions().count(STAGE_TWO[0]), 1)
        self.assertEqual(self.actions().count(REBOOT), 1)

    def test_spent_reboot_budget_keeps_retrying_only_up_cleanup(self):
        self.seed_budget(f"v1 {EPOCH} {EPOCH}\n")
        self.scenario["failures"] = [["ip", "link", "set", "dev", "wlan0", "up"]]
        self.exhaust()
        self.assertEqual(self.admin_state.read_text(), "down")
        self.assertEqual(self.actions().count(STAGE_TWO[0]), 1)
        self.assertNotIn(REBOOT, self.actions())
        up_count = self.actions().count(STAGE_TWO[1])
        self.wifi(601)
        self.wifi(900)
        self.assertGreater(self.actions().count(STAGE_TWO[1]), up_count)
        self.assertEqual(self.actions().count(STAGE_TWO[0]), 1)
        self.assertNotIn(REBOOT, self.actions())
        self.scenario.pop("failures")
        self.wifi(901)
        self.assertEqual(self.admin_state.read_text(), "up")
        self.assertFalse((self.runtime / "link-needs-up").exists())
        self.assertEqual(self.actions().count(STAGE_TWO[0]), 1)
        self.assertNotIn(REBOOT, self.actions())

    def test_failed_or_hung_reboot_request_cannot_repeat(self):
        for mode in ("failures", "hangs"):
            with self.subTest(mode=mode):
                self.scenario.pop("failures", None)
                self.scenario.pop("hangs", None)
                self.recovery.unlink(missing_ok=True)
                self.budget.unlink(missing_ok=True)
                before = len(self.actions())
                self.scenario[mode] = [["systemctl", "--no-block", "reboot"]]
                self.scenario["timeout_scale"] = 0.2
                self.exhaust()
                self.wifi(601)
                self.wifi(900)
                self.assertEqual(self.actions()[before:], STAGE_ONE + STAGE_TWO + [REBOOT])
                self.assertEqual(self.budget.read_text(), f"v1 {EPOCH} {EPOCH}\n")

    def test_cancellation_after_link_down_restores_link_and_preserves_stage(self):
        self.stage_two_ready()
        previous = self.recovery.read_text()
        self.scenario["cancel_after_down"] = True
        self.wifi(420)
        self.assertNotEqual(self.recovery.read_text(), previous)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO[:2])
        self.assertEqual(self.admin_state.read_text(), "up")
        self.assertFalse((self.runtime / "link-needs-up").exists())
        self.scenario["cancel_after_down"] = False
        self.wifi(421)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_TWO[:2])

    def test_whole_invocation_watchdog_terminates_an_unbounded_local_hang(self):
        self.scenario.update({"hangs": [["install"]], "outer_timeout_scale": 0.1})
        self.wifi(0, expected=124)
        self.assertEqual(self.actions(), [])
        self.assertFalse(self.recovery.exists())
        self.scenario.pop("hangs")
        self.scenario.pop("outer_timeout_scale")
        self.wifi(0)
        self.assertTrue(self.recovery.exists())

    def test_corrupt_runtime_and_uptime_rollback_restart_the_delay(self):
        self.recovery.write_text("corrupt\n")
        self.wifi(100)
        self.wifi(399)
        self.assertEqual(self.actions(), [])
        self.wifi(400)
        self.assertEqual(self.actions(), STAGE_ONE)
        self.wifi(0)
        self.wifi(299)
        self.assertEqual(self.actions(), STAGE_ONE)
        self.wifi(300)
        self.assertEqual(self.actions(), STAGE_ONE + STAGE_ONE)


class HealthIntegrationTests(RecoveryFixture):
    def test_nonzero_wifi_helper_defers_tailnet_and_k3s_and_clears_stale_state(self):
        for status in (1, 124, 127):
            with self.subTest(status=status):
                self.deadline.write_text(str(EPOCH - 1) + "\n")
                self.attempted.touch()
                self.scenario.update({"helper_status": status, "ts_active": False, "k3s_state": "inactive"})
                before = len(self.events())
                self.health_check()
                events = self.events()[before:]
                self.assertFalse(self.deadline.exists())
                self.assertFalse(self.attempted.exists())
                self.assertEqual(mutations(events), [])

    def test_wifi_recovery_leaves_a_fresh_tailnet_outage_delay(self):
        self.deadline.write_text(str(EPOCH - 1) + "\n")
        self.attempted.touch()
        self.scenario["helper_status"] = 1
        self.health_check()
        self.scenario["helper_status"] = 0
        self.health_check()
        self.assertEqual(self.deadline.read_text(), f"{EPOCH + 300}\n")
        self.assertFalse(self.attempted.exists())
        self.assertEqual(self.actions(), [])

    def test_healthy_underlay_allows_one_genuine_tailnet_only_restart(self):
        self.scenario.update({"helper_status": 0, "ts_backend": "Running", "ts_ping": False})
        self.health_check(epoch=EPOCH)
        self.assertEqual(self.deadline.read_text(), f"{EPOCH + 300}\n")
        self.health_check(epoch=EPOCH + 299)
        self.assertEqual(self.actions(), [])
        self.health_check(epoch=EPOCH + 300)
        self.health_check(epoch=EPOCH + 301)
        self.assertEqual(self.actions(), [("systemctl", "restart", "tailscaled.service")])
        self.assertTrue(self.attempted.exists())

    def test_healthy_underlay_and_tailnet_allow_existing_k3s_recovery(self):
        self.scenario.update({"helper_status": 0, "ts_ping": True, "k3s_state": "inactive"})
        self.deadline.write_text(str(EPOCH - 1) + "\n")
        self.attempted.touch()
        self.health_check()
        self.assertFalse(self.deadline.exists())
        self.assertFalse(self.attempted.exists())
        self.assertEqual(self.actions(), [("systemctl", "restart", "k3s")])


if __name__ == "__main__":
    unittest.main()
