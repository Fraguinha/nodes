import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
ASSETS = (
    "usr/local/bin/node-health-check.sh",
    "usr/local/bin/node-wifi-recovery.sh",
    "etc/systemd/system/node-health.service",
    "etc/systemd/system/node-health.timer",
)
MODES = (0o755, 0o755, 0o644, 0o644)

FAKE_COMMAND = r'''
import json
import os
from pathlib import Path
import re
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["HEALTH_SYNC_ROOT"])
config = json.loads(Path(os.environ["HEALTH_SYNC_CONFIG"]).read_text())
state_path = Path(os.environ["HEALTH_SYNC_STATE"])
state = json.loads(state_path.read_text())
assets = (
    "usr/local/bin/node-health-check.sh",
    "usr/local/bin/node-wifi-recovery.sh",
    "etc/systemd/system/node-health.service",
    "etc/systemd/system/node-health.timer",
)

def event(*values):
    with Path(os.environ["HEALTH_SYNC_CALLS"]).open("a") as output:
        output.write(json.dumps(values) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def fail(message):
    event("rejected", name, args, message)
    raise SystemExit(message)

def relative(path):
    return str(path.relative_to(root))

def safe(value):
    if os.environ.get("HEALTH_SYNC_REMOTE") != "1" or Path.cwd() != root:
        fail("Filesystem operation outside isolated remote working directory")
    if Path(value).is_absolute():
        fail("Unexpected absolute filesystem operation")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        fail("Filesystem operation escapes isolated target")
    item = relative(path)
    if not (
        item in assets
        or any(item.startswith(asset + ".nodes-health.") for asset in assets)
        or re.match(r"^(tmp|run)/nodes-health\.[^/]+(?:/.*)?$", item)
    ):
        fail("Filesystem operation outside deployment allowlist: " + item)
    return path

if name == "timeout":
    event(name, *args)
    if len(args) < 3 or args[0] != "--kill-after=2s":
        fail("Missing bounded command termination")
    if not re.fullmatch(r"[1-9][0-9]*s", args[1]):
        fail("Unexpected command deadline")
    command = args[2:]
    if config.get("service_query_timeout") and command == [
        "systemctl", "show", "--property=ActiveState", "--value", "node-health.service"
    ]:
        raise SystemExit(124)
    if command[0] == "bash":
        os.execv(os.environ["HEALTH_SYNC_BASH"], ["bash", *command[1:]])
    name, args = command[0], command[1:]

if name == "ssh":
    import signal
    import subprocess

    while args and args[0] in ("-o", "-F"):
        args = args[2:]
    if len(args) != 2 or not re.fullmatch(r"pi@[a-z0-9-]+", args[0]):
        fail("Unexpected SSH request")
    host, command = args
    event("ssh", host)
    if not command.startswith("bash --noprofile --norc -euo pipefail -c "):
        fail("Non-health SSH operation")
    environment = os.environ.copy()
    environment["HEALTH_SYNC_REMOTE"] = "1"
    environment["BASH_ENV"] = os.environ["HEALTH_SYNC_BASH_ENV"]
    environment["HOME"] = str(root / "home/pi")
    process = subprocess.Popen(
        [os.environ["HEALTH_SYNC_BASH"], "--noprofile", "--norc", "-c", command],
        cwd=root, env=environment, start_new_session=True,
    )
    try:
        status = process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        fail("Isolated remote program exceeded 30 seconds")
    raise SystemExit(status)

if name == "tar":
    import tarfile

    portable = args[:3] == ["--format=ustar", "--no-xattrs", "--no-acls"]
    if portable:
        args = args[3:]
    if args[:1] == ["-C"]:
        if not portable:
            fail("Upload must use portable metadata-free ustar")
        if args[2:5] != ["-cf", "-", "--"] or tuple(args[5:]) != assets:
            fail("Non-allowlisted tar upload")
        event("tar-upload", args[5:])
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
            for asset in assets:
                archive.add(Path(args[1]) / asset, arcname=asset, recursive=False)
        raise SystemExit(0)
    expected = ["--extract", "--file=-"]
    if args[:2] != expected or not args[2].startswith("--directory="):
        fail("Unexpected tar extraction")
    if args[3:6] != ["--no-same-owner", "--no-same-permissions", "--"]:
        fail("Unsafe tar extraction options")
    if tuple(args[6:]) != assets:
        fail("Non-allowlisted tar extraction")
    destination = safe(args[2].split("=", 1)[1])
    event("tar-extract", relative(destination), oct(destination.stat().st_mode & 0o777))
    if config.get("upload_failure"):
        raise SystemExit(1)
    seen = []
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        for member in archive:
            if member.pax_headers:
                fail("Extended archive metadata is not allowed")
            if member.name not in assets or not member.isfile():
                fail("Unexpected uploaded archive member")
            seen.append(member.name)
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(0o600)
    if tuple(seen) != assets:
        fail("Incomplete health archive")
    if config.get("corrupt_uploaded_script"):
        (destination / assets[1]).write_text("if then\n")
    event("uploaded", seen)
    raise SystemExit(0)


if name == "sudo":
    event(name, *args)
    if args[:8] != ["-n", "bash", "--noprofile", "--norc", "-euo", "pipefail", "-s", "--"] or len(args) != 9:
        fail("Unexpected privileged operation")
    staging = safe(args[8])
    if staging.stat().st_mode & 0o777 != 0o700:
        fail("Upload staging is not private")
    os.execv(os.environ["HEALTH_SYNC_BASH"], ["bash", *args[2:]])

if name == "systemctl":
    event(name, *args)
    if args == ["show", "--property=ActiveState", "--value", "node-health.timer"]:
        print(state["timer_state"])
        raise SystemExit(0)
    if args == ["show", "--property=ActiveState", "--value", "node-health.service"]:
        states = config.get("service_states", ["inactive"])
        index = state["service_reads"]
        state["service_reads"] += 1
        state["service_state"] = states[min(index, len(states) - 1)]
        save()
        event("service-state", state["service_state"])
        print(state["service_state"])
        raise SystemExit(0)
    if args == ["stop", "node-health.timer"]:
        state["timer_state"] = "inactive"
        save()
        raise SystemExit(1 if config.get("timer_stop_failure") else 0)
    if args == ["start", "node-health.timer"]:
        state["timer_starts"] += 1
        if state["timer_starts"] == 1 and config.get("timer_start_accepted_timeout"):
            state["timer_state"] = "active"
            state["service_state"] = "active"
            save()
            event("timer-start-accepted-timeout")
            raise SystemExit(124)
        if state["timer_starts"] not in config.get("timer_start_failures", []):
            state["timer_state"] = "active"
            result = 0
        else:
            result = 1
        save()
        raise SystemExit(result)
    if args == ["daemon-reload"]:
        state["reloads"] += 1
        save()
        raise SystemExit(1 if state["reloads"] in config.get("reload_failures", []) else 0)
    fail("Forbidden service action")

if name == "sleep":
    event(name, *args)
    if args != ["1"]:
        fail("Unexpected wait")
    raise SystemExit(0)

if name == "mktemp":
    import tempfile

    event(name, *args)
    directory = args[:1] == ["-d"]
    template = safe(args[-1])
    prefix = template.name.rstrip("X")
    if directory:
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=template.parent))
    else:
        descriptor, filename = tempfile.mkstemp(prefix=prefix, dir=template.parent)
        os.close(descriptor)
        path = Path(filename)
    event("temporary", relative(path), oct(path.stat().st_mode & 0o777))
    print(relative(path))
    raise SystemExit(0)

if name == "install":
    event(name, *args)
    directory = False
    owner = None
    group = None
    mode = None
    while args and args[0] != "--":
        option = args.pop(0)
        if option == "-d":
            directory = True
        elif option in ("-o", "-g", "-m"):
            value = args.pop(0)
            if option == "-o":
                owner = value
            elif option == "-g":
                group = value
            else:
                mode = int(value, 8)
        else:
            fail("Unexpected install option")
    if not args or args.pop(0) != "--" or mode is None:
        fail("Unsafe install arguments")
    if directory:
        if mode != 0o700:
            fail("Install staging is not private")
        for value in args:
            path = safe(value)
            path.mkdir(mode=mode)
            path.chmod(mode)
        raise SystemExit(0)
    if owner != "root" or group != "root" or len(args) != 2:
        fail("Unexpected health asset ownership")
    source, target = map(safe, args)
    if config.get("candidate_install_failure") and ".nodes-health." in target.name:
        raise SystemExit(1)
    import shutil

    shutil.copyfile(source, target)
    target.chmod(mode)
    state["owners"][relative(target)] = [owner, group]
    save()
    raise SystemExit(0)

if name == "cp":
    import shutil

    event(name, *args)
    if args[:2] != ["-a", "--"] or len(args) != 4:
        fail("Unsafe backup or restore copy")
    source, target = map(safe, args[2:])
    if config.get("rollback_copy_failure") and "/backup/" in str(source):
        raise SystemExit(1)
    shutil.copy2(source, target)
    state["owners"][relative(target)] = state["owners"].get(relative(source), ["pi", "pi"])
    save()
    raise SystemExit(0)

if name == "mv":
    event(name, *args)
    if args[:2] != ["-fT", "--"] or len(args) != 4:
        fail("Non-atomic health replacement")
    source, target = map(safe, args[2:])
    target_name = relative(target)
    if target_name not in assets:
        fail("Replacement outside allowlist")
    if state["service_state"] not in ("inactive", "failed"):
        fail("Replacement while health service is running")
    state["moves"] += 1
    save()
    if state["moves"] in config.get("move_failures", []):
        raise SystemExit(1)
    os.replace(source, target)
    state["owners"][target_name] = state["owners"].pop(relative(source), ["pi", "pi"])
    save()
    event("replace", target_name)
    raise SystemExit(0)

if name == "rm":
    event(name, *args)
    if args[:2] not in (["-f", "--"], ["-rf", "--"]):
        fail("Unsafe staging cleanup")
    for value in args[2:]:
        path = safe(value)
        item = relative(path)
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        state["owners"] = {
            key: owner for key, owner in state["owners"].items()
            if key != item and not key.startswith(item + "/")
        }
    save()
    raise SystemExit(0)

if name == "systemd-analyze":
    import configparser

    event(name, *args)
    if args[:2] != ["verify", "--man=no"] or len(args) != 4:
        fail("Unexpected unit validation")
    for value, section in zip(args[2:], ("Service", "Timer")):
        path = safe(value)
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            unit.read_string(path.read_text())
            if section not in unit:
                raise ValueError("Missing unit section")
        except (configparser.Error, ValueError):
            raise SystemExit(1)
        if section == "Service":
            executable = unit[section]["ExecStart"]
            if executable != "/" + assets[0] or not os.access(root / assets[0], os.X_OK):
                raise SystemExit(1)
    event("verified-units")
    raise SystemExit(0)

fail("Unexpected isolated command")
'''


class HealthSyncFixture:
    def __init__(self, directory, **config):
        self.base = Path(directory)
        self.repository = self.base / "repository"
        self.root = self.base / "target"
        self.bin = self.base / "bin"
        self.config_path = self.base / "config.json"
        self.state_path = self.base / "state.json"
        self.calls_path = self.base / "calls.jsonl"
        self.bin.mkdir()
        self.repository.mkdir()
        for relative in ("nodes", "lib/common.sh", "lib/commands.sh", "lib/render.sh"):
            destination = self.repository / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / relative, destination)
        self.sources = {}
        self.originals = {}
        self.owners = {}
        absent = config.pop("absent", [])
        for asset in ASSETS:
            source = self.repository / "rootfs/common" / asset
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / "rootfs/common" / asset, source)
            self.sources[asset] = source.read_bytes()
            target = self.root / asset
            target.parent.mkdir(parents=True, exist_ok=True)
            if asset not in absent:
                target.write_text("previous " + asset + "\n")
                target.chmod(0o640)
                self.originals[asset] = (target.read_bytes(), 0o640)
                self.owners[asset] = ["pi", "pi"]
            else:
                self.originals[asset] = None
        self.sentinels = {}
        for relative in (
            "home/pi/.ssh/authorized_keys",
            "etc/chrony/conf.d/ip-fallback.conf",
            "etc/sysctl.d/99-node.conf",
            "etc/systemd/system/watchdog-load.service",
            "etc/systemd/system/unrelated.service",
            "var/lib/node-health/reboots",
            "var/lib/node-health/wifi-link-reset-window",
            "var/log/syslog",
            "var/log/syslog.1",
            "var/log/journal/retained.log",
            "etc/netplan/50-cloud-init.yaml",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("retain " + relative + "\n")
            path.chmod(0o640)
            self.sentinels[relative] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for relative in ("tmp", "run"):
            (self.root / relative).mkdir()
        for relative in (
            "rootfs/common/etc/forbidden-spill",
            "rootfs/physical/etc/forbidden-physical",
            "config/authorized_keys",
        ):
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must never upload\n")
        enabled = config.pop("timer_enabled", "enabled")
        self.enabled = enabled
        self.timer_state = config.pop("timer_state", "active")
        if enabled == "enabled":
            self.enablement_link = self.root / "etc/systemd/system/timers.target.wants/node-health.timer"
            self.enablement_link.parent.mkdir()
            self.enablement_link.symlink_to("../node-health.timer")
        else:
            self.enablement_link = None
        self.config_path.write_text(json.dumps(config))
        self.state_path.write_text(json.dumps({
            "timer_state": self.timer_state,
            "timer_enabled": enabled,
            "service_state": "inactive",
            "service_reads": 0,
            "timer_starts": 0,
            "reloads": 0,
            "moves": 0,
            "owners": self.owners,
        }))
        self.calls_path.write_text("")
        driver = self.bin / "driver"
        driver.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        driver.chmod(0o755)
        for command in (
            "ssh", "tar", "sudo", "timeout", "systemctl", "systemd-analyze",
            "mktemp", "install", "cp", "mv", "rm", "sleep", "sysctl",
            "journalctl", "truncate", "networkctl", "tailscale", "k3s", "find",
        ):
            (self.bin / command).symlink_to(driver)
        bash_environment = self.base / "bash-env"
        bash_environment.write_text(
            'cd() { if [[ "$#" == 1 && "$1" == / ]]; then '
            'builtin cd "$HEALTH_SYNC_ROOT"; else builtin cd "$@"; fi; }\n'
            'cd /\n'
        )
        self.environment = os.environ.copy()
        self.environment.pop("NODES_SSH_CONFIG", None)
        self.environment.pop("BASH_ENV", None)
        self.environment.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HEALTH_SYNC_ROOT": str(self.root),
            "HEALTH_SYNC_CONFIG": str(self.config_path),
            "HEALTH_SYNC_STATE": str(self.state_path),
            "HEALTH_SYNC_CALLS": str(self.calls_path),
            "HEALTH_SYNC_BASH_ENV": str(bash_environment),
            "HEALTH_SYNC_BASH": shutil.which("bash"),
        })

    def run(self, *hostnames):
        return subprocess.run(
            [self.environment["HEALTH_SYNC_BASH"], str(self.repository / "nodes"),
             "sync", "--health-only", *(hostnames or ("cloud-1",))],
            cwd=self.repository,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=45,
        )

    def events(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def state(self):
        return json.loads(self.state_path.read_text())


class NodeHealthSyncTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def fixture(self, **config):
        directory = Path(self.temporary.name) / str(len(list(Path(self.temporary.name).iterdir())))
        directory.mkdir()
        return HealthSyncFixture(directory, **config)

    def assert_preserved(self, fixture):
        for relative, expected in fixture.sentinels.items():
            path = fixture.root / relative
            self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), expected)
        self.assertEqual(fixture.state()["timer_enabled"], fixture.enabled)
        if fixture.enablement_link is not None:
            self.assertEqual(os.readlink(fixture.enablement_link), "../node-health.timer")
        else:
            self.assertFalse((fixture.root / "etc/systemd/system/timers.target.wants/node-health.timer").exists())
        self.assertFalse(list(fixture.root.rglob("*.nodes-health.*")))
        self.assertEqual(list((fixture.root / "tmp").iterdir()), [])
        self.assertEqual(list((fixture.root / "run").iterdir()), [])
        forbidden = {
            "sysctl", "journalctl", "truncate", "networkctl", "tailscale", "k3s", "find"
        }
        self.assertFalse([event for event in fixture.events() if event[0] in forbidden])

    def assert_originals(self, fixture):
        for asset, original in fixture.originals.items():
            path = fixture.root / asset
            if original is None:
                self.assertFalse(path.exists())
                self.assertNotIn(asset, fixture.state()["owners"])
            else:
                self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), original)
                self.assertEqual(fixture.state()["owners"][asset], ["pi", "pi"])

    def assert_deployed(self, fixture):
        for asset, mode in zip(ASSETS, MODES):
            path = fixture.root / asset
            self.assertEqual(path.read_bytes(), fixture.sources[asset])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            self.assertEqual(fixture.state()["owners"][asset], ["root", "root"])

    def test_allowlisted_deployment_waits_and_restores_active_timer(self):
        fixture = self.fixture(service_states=["active", "activating", "reloading", "deactivating", "inactive"])
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_deployed(fixture)
        self.assert_preserved(fixture)
        events = fixture.events()
        self.assertEqual([event for event in events if event[0] == "uploaded"], [["uploaded", list(ASSETS)]])
        self.assertEqual([event for event in events if event[0] == "tar-upload"], [["tar-upload", list(ASSETS)]])
        self.assertEqual([event for event in events if event[0] == "ssh"], [["ssh", "pi@cloud-1"]])
        self.assertEqual(fixture.state()["timer_state"], "active")
        stop = events.index(["systemctl", "stop", "node-health.timer"])
        first_wait = events.index(["service-state", "active"])
        finished = events.index(["service-state", "inactive"])
        replacements = [index for index, event in enumerate(events) if event[0] == "replace"]
        verified = events.index(["verified-units"])
        reload = events.index(["systemctl", "daemon-reload"])
        restore = events.index(["systemctl", "start", "node-health.timer"])
        self.assertLess(stop, first_wait)
        self.assertLess(finished, min(replacements))
        self.assertLess(replacements[1], verified)
        self.assertLess(verified, replacements[2])
        self.assertLess(max(replacements), reload)
        self.assertLess(reload, restore)
        self.assertEqual([event[1] for event in events if event[0] == "replace"], list(ASSETS))
        self.assertEqual(fixture.state()["service_reads"], 5)
        self.assertEqual(fixture.state()["timer_starts"], 1)
        for event in events:
            if event[0] == "temporary":
                expected = "0o700" if event[1].startswith(("tmp/", "run/")) else "0o600"
                self.assertEqual(event[2], expected)
        self.assertFalse([event for event in events if event[0] == "rejected"])

    def test_inactive_disabled_and_missing_timer_are_not_started_or_enabled(self):
        for enabled in ("disabled", "not-found", "enabled"):
            with self.subTest(enabled=enabled):
                fixture = self.fixture(timer_state="inactive", timer_enabled=enabled)
                result = fixture.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_deployed(fixture)
                self.assert_preserved(fixture)
                self.assertEqual(fixture.state()["timer_state"], "inactive")
                self.assertEqual(fixture.state()["timer_starts"], 0)
                self.assertNotIn(["systemctl", "stop", "node-health.timer"], fixture.events())

    def test_long_running_service_times_out_without_stopping_or_replacing_it(self):
        fixture = self.fixture(service_states=["activating", "reloading", "deactivating"])
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.state()["service_reads"], 65)
        self.assertEqual(fixture.state()["timer_state"], "active")
        self.assertEqual(fixture.state()["timer_starts"], 1)
        self.assertEqual(fixture.state()["reloads"], 0)
        self.assertFalse([event for event in fixture.events() if event[0] == "replace"])
        self.assert_originals(fixture)
        self.assert_preserved(fixture)
        self.assertFalse([event for event in fixture.events() if event[0] == "rejected"])

    def test_failed_service_is_safe_to_replace_without_forcing_a_run(self):
        fixture = self.fixture(service_states=["failed"])
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_deployed(fixture)
        self.assert_preserved(fixture)
        self.assertEqual(fixture.state()["service_reads"], 1)

    def test_install_and_reload_failures_roll_back_and_restore_timer(self):
        for config in (
            {"candidate_install_failure": True},
            {"move_failures": [2]},
            {"move_failures": [4]},
            {"reload_failures": [1]},
            {"timer_start_failures": [1]},
            {"timer_stop_failure": True},
            {"service_query_timeout": True},
        ):
            with self.subTest(config=config):
                fixture = self.fixture(**config)
                result = fixture.run()
                self.assertNotEqual(result.returncode, 0)
                self.assert_originals(fixture)
                self.assert_preserved(fixture)
                self.assertEqual(fixture.state()["timer_state"], "active")
                self.assertEqual(len([event for event in fixture.events() if event[0] == "ssh"]), 1)
                self.assertEqual(len([event for event in fixture.events() if event[0] == "tar-upload"]), 1)
                self.assertFalse([event for event in fixture.events() if event[0] == "rejected"])

    def test_accepted_timer_start_timeout_requiesces_and_waits_before_rollback(self):
        fixture = self.fixture(
            timer_start_accepted_timeout=True,
            service_states=["inactive", "activating", "active", "deactivating", "inactive"],
        )
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assert_originals(fixture)
        self.assert_preserved(fixture)
        events = fixture.events()
        accepted = events.index(["timer-start-accepted-timeout"])
        stops = [index for index, event in enumerate(events)
                 if event == ["systemctl", "stop", "node-health.timer"]]
        self.assertEqual(len(stops), 2)
        self.assertGreater(stops[1], accepted)
        finished = max(index for index, event in enumerate(events)
                       if event == ["service-state", "inactive"])
        rollback_replacements = [index for index, event in enumerate(events)
                                 if event[0] == "replace" and index > accepted]
        self.assertEqual(len(rollback_replacements), len(ASSETS))
        self.assertLess(stops[1], finished)
        self.assertLess(finished, min(rollback_replacements))
        self.assertEqual(fixture.state()["timer_state"], "active")
        self.assertEqual(fixture.state()["timer_starts"], 2)

    def test_accepted_timer_start_timeout_retains_installed_files_if_health_never_quiesces(self):
        fixture = self.fixture(
            timer_start_accepted_timeout=True,
            service_states=["inactive", "active"],
        )
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assert_deployed(fixture)
        self.assert_preserved(fixture)
        events = fixture.events()
        accepted = events.index(["timer-start-accepted-timeout"])
        self.assertFalse([event for event in events[accepted + 1:] if event[0] == "replace"])
        self.assertEqual(len([event for event in events
                              if event == ["systemctl", "stop", "node-health.timer"]]), 2)
        self.assertEqual(fixture.state()["service_reads"], 66)
        self.assertEqual(fixture.state()["reloads"], 1)
        self.assertEqual(fixture.state()["timer_state"], "active")
        self.assertIn("installed files retained", result.stderr)

    def test_rollback_removes_only_new_assets_when_originals_were_absent(self):
        fixture = self.fixture(absent=list(ASSETS), timer_state="inactive", timer_enabled="not-found", reload_failures=[1])
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assert_originals(fixture)
        self.assert_preserved(fixture)
        self.assertEqual(fixture.state()["timer_state"], "inactive")
        self.assertEqual(fixture.state()["timer_starts"], 0)

    def test_missing_original_assets_can_be_installed_without_enabling_timer(self):
        fixture = self.fixture(absent=list(ASSETS), timer_state="inactive", timer_enabled="not-found")
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_deployed(fixture)
        self.assert_preserved(fixture)
        self.assertEqual(fixture.state()["timer_state"], "inactive")

    def test_invalid_candidates_fail_before_activation_and_preserve_originals(self):
        for kind in ("uploaded-script", "unit", "upload"):
            with self.subTest(kind=kind):
                fixture = self.fixture(
                    corrupt_uploaded_script=kind == "uploaded-script",
                    upload_failure=kind == "upload",
                )
                if kind == "unit":
                    (fixture.repository / "rootfs/common" / ASSETS[3]).write_text("[Timer\nbroken\n")
                result = fixture.run()
                self.assertNotEqual(result.returncode, 0)
                self.assert_originals(fixture)
                self.assert_preserved(fixture)
                self.assertEqual(fixture.state()["timer_state"], "active")
                if kind != "unit":
                    self.assertEqual(fixture.state()["reloads"], 0)
                    self.assertEqual(fixture.state()["timer_starts"], 0)
                else:
                    self.assertEqual(fixture.state()["reloads"], 1)
                    self.assertEqual(fixture.state()["timer_starts"], 1)

    def test_all_hostnames_and_local_files_are_checked_before_upload(self):
        fixture = self.fixture()
        result = fixture.run("cloud-1", "bad;name")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.events(), [])
        self.assert_originals(fixture)
        self.assert_preserved(fixture)
        for asset in ASSETS:
            with self.subTest(asset=asset):
                fixture = self.fixture()
                (fixture.repository / "rootfs/common" / asset).unlink()
                result = fixture.run()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.events(), [])
                self.assert_originals(fixture)
                self.assert_preserved(fixture)

    def test_symlink_and_each_invalid_local_script_are_rejected_before_ssh(self):
        for kind in ("symlink", "directory", "first-script", "second-script"):
            with self.subTest(kind=kind):
                fixture = self.fixture()
                index = 1 if kind == "second-script" else 0
                source = fixture.repository / "rootfs/common" / ASSETS[index]
                if kind == "symlink":
                    source.unlink()
                    source.symlink_to(PROJECT / "rootfs/common" / ASSETS[index])
                elif kind == "directory":
                    source.unlink()
                    source.mkdir()
                else:
                    source.write_text("if then\n")
                result = fixture.run()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.events(), [])
                self.assert_originals(fixture)
                self.assert_preserved(fixture)

    def test_rollback_failure_is_reported_as_failure_not_success(self):
        fixture = self.fixture(reload_failures=[1, 2], rollback_copy_failure=True)
        result = fixture.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Rollback failed restoring", result.stderr)
        self.assertIn("Rollback daemon-reload failed", result.stderr)
        self.assertEqual(fixture.state()["timer_state"], "active")
        self.assert_preserved(fixture)
        self.assert_deployed(fixture)


if __name__ == "__main__":
    unittest.main()
