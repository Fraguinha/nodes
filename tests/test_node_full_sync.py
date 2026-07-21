import base64
import hashlib
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
COMMON = (
    "usr/local/bin/node-health-check.sh",
    "usr/local/bin/node-wifi-recovery.sh",
    "usr/local/bin/watchdog-load.sh",
    "usr/local/bin/setup-node.sh",
    "usr/local/bin/node-configure.py",
    "usr/local/share/nodes/k3s-server-ha.yaml",
    "usr/local/share/nodes/node-policy.json",
    "etc/k3s-resolv.conf",
    "etc/chrony/conf.d/ip-fallback.conf",
    "etc/systemd/system/node-health.service",
    "etc/systemd/system/node-health.timer",
    "etc/systemd/system/watchdog-load.service",
    "etc/systemd/system/tailscaled.service.d/wait-for-time-sync.conf",
    "etc/systemd/system/tailscaled.service.d/10-oom.conf",
    "etc/systemd/system/k3s.service.d/restart-after-tailscale.conf",
    "etc/systemd/system/k3s-agent.service.d/restart-after-tailscale.conf",
    "etc/systemd/system/ssh.service.d/10-oom.conf",
    "etc/systemd/journald.conf.d/10-limits.conf",
    "etc/systemd/system.conf.d/10-watchdog.conf",
    "etc/rsyslog.d/10-k3s.conf",
    "etc/sysctl.d/99-tailscale.conf",
)
PHYSICAL = (
    "etc/systemd/network/10-wlan0.link",
    "etc/udev/rules.d/70-wifi-powersave-off.rules",
    "etc/chrony/conf.d/ip-fallback.conf",
)
KEYS = "home/pi/.ssh/authorized_keys"
SSH_DROPIN = "etc/systemd/system/ssh.service.d/10-oom.conf"
PROTECTED_SSH = (SSH_DROPIN, KEYS)
CHRONY = "etc/chrony/conf.d/ip-fallback.conf"
CHRONY_PENDING = "var/lib/nodes/chrony-pending"


def authorized_key(seed=1, options="", comment="fixture-key"):
    kind = b"ssh-ed25519"
    material = bytes([seed]) * 32
    blob = len(kind).to_bytes(4, "big") + kind + len(material).to_bytes(4, "big") + material
    encoded = base64.b64encode(blob).decode("ascii")
    prefix = options + " " if options else ""
    suffix = " " + comment if comment else ""
    return prefix + "ssh-ed25519 " + encoded + suffix + "\n"

HELPER = r'''
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
parser.add_argument("--activate", action="store_true")
parser.add_argument("--platform", choices=("physical", "cloud"), required=True)
parser.add_argument("--role", choices=("server", "agent"), required=True)
parser.add_argument("--source-root")
options = parser.parse_args()
root = Path(os.environ["FULL_SYNC_ROOT"])
config = json.loads(Path(os.environ["FULL_SYNC_CONFIG"]).read_text())
state_path = Path(os.environ["FULL_SYNC_STATE"])
state = json.loads(state_path.read_text())
source = root
if options.source_root:
    source = root / options.source_root.lstrip("/")
    if not source.resolve().is_relative_to(root / "run"):
        raise SystemExit("Desired source is not privately staged")
    if not (source / "usr/local/share/nodes/node-policy.json").is_file():
        raise SystemExit("Missing staged policy")
context = sorted((root / "var/lib/nodes/chrony-pending").glob("*.conf"))
for path in context:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or state["owners"].get(str(path.relative_to(root))) != [0, 0]
        or hashlib.sha256(path.read_bytes()).hexdigest() + ".conf" != path.name
    ):
        raise SystemExit("Unsafe durable chrony ownership context")
owned_context_present = any(
    path.read_bytes() == b"previous etc/chrony/conf.d/ip-fallback.conf\n" for path in context
)
with Path(os.environ["FULL_SYNC_CALLS"]).open("a") as output:
    output.write(json.dumps([
        "runtime", options.check, options.platform, options.role,
        options.source_root is not None, owned_context_present,
    ]) + "\n")
    output.write(json.dumps(["chrony-context-count", len(context)]) + "\n")
if options.check:
    result = config.get("runtime_check_status", 0 if state["timer_enabled"] == "enabled" else 1)
    print("match runtime policy" if result == 0 else "drift runtime policy")
else:
    state["runtime_runs"] += 1
    result = config.get("runtime_status_by_host", {}).get(
        os.environ["FULL_SYNC_HOST"], config.get("runtime_status", 0)
    )
    if result == 0:
        state["timer_enabled"] = "enabled"
        state["timer_state"] = "active"
    state_path.write_text(json.dumps(state))
raise SystemExit(result)
'''

DRIVER = r'''
import json
import os
from pathlib import Path
import re
import stat
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["FULL_SYNC_ROOT"])
repository = Path(os.environ["FULL_SYNC_REPOSITORY"])
config = json.loads(Path(os.environ["FULL_SYNC_CONFIG"]).read_text())
state_path = Path(os.environ["FULL_SYNC_STATE"])
state = json.loads(state_path.read_text())
common = tuple(json.loads(os.environ["FULL_SYNC_COMMON"]))
physical = tuple(json.loads(os.environ["FULL_SYNC_PHYSICAL"]))
assets = (*common, *physical, "home/pi/.ssh/authorized_keys")
protected = tuple(json.loads(os.environ["FULL_SYNC_PROTECTED"]))
protected_parents = {str(Path(asset).parent) for asset in protected}
parents = set()
for asset in assets:
    path = Path(asset).parent
    while str(path) != ".":
        parents.add(str(path))
        path = path.parent
context_dir = "var/lib/nodes/chrony-pending"
context_directories = ("var/lib/nodes", context_dir)

def event(*values):
    with Path(os.environ["FULL_SYNC_CALLS"]).open("a") as output:
        output.write(json.dumps(values) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def fail(message):
    event("rejected", name, args, message)
    raise SystemExit(message)

def relative(path):
    return str(path.relative_to(root))

def safe(value):
    if os.environ.get("FULL_SYNC_REMOTE") != "1" or Path.cwd() != root:
        fail("Filesystem operation outside isolated remote directory")
    if Path(value).is_absolute():
        fail("Unexpected absolute filesystem operation")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        fail("Filesystem operation escapes isolated target")
    item = relative(path)
    if not (
        item in assets or item in parents
        or item in ("var", "var/lib", *context_directories)
        or item.startswith(context_dir + "/")
        or any(item.startswith(asset + ".nodes-config.") for asset in assets)
        or re.fullmatch(r"(?:tmp|run)/nodes-config\.[^/]+(?:/.*)?", item)
    ):
        fail("Filesystem operation outside configuration allowlist: " + item)
    return path

def protected_write(path):
    item = relative(path)
    return item in protected or any(item.startswith(asset + ".nodes-config.") for asset in protected)

def context_temporary(path):
    return re.fullmatch(context_dir + r"/\.snapshot-[A-Za-z0-9_]+\.tmp", relative(path)) is not None

def ids(owner, group):
    return [1000 if owner == "pi" else int(owner) if owner.isdigit() else 0,
            1000 if group == "pi" else int(group) if group.isdigit() else 0]

if name == "timeout":
    if len(args) < 3 or args[0] != "--kill-after=2s" or not re.fullmatch(r"[1-9][0-9]*s", args[1]):
        fail("Missing command deadline")
    event("bounded", *args)
    command = args[2:]
    if config.get("service_query_timeout") and command == [
        "systemctl", "show", "--property=ActiveState", "--value", "node-health.service"
    ]:
        raise SystemExit(124)
    if command[0] == "bash":
        os.execv(os.environ["FULL_SYNC_BASH"], ["bash", *command[1:]])
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
        fail("Unexpected remote program")
    environment = os.environ.copy()
    environment.update({
        "FULL_SYNC_REMOTE": "1",
        "FULL_SYNC_HOST": host[3:],
        "BASH_ENV": os.environ["FULL_SYNC_BASH_ENV"],
        "HOME": str(root / "home/pi"),
    })
    process = subprocess.Popen(
        [os.environ["FULL_SYNC_BASH"], "--noprofile", "--norc", "-c", command],
        cwd=root, env=environment, start_new_session=True,
    )
    try:
        status = process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        fail("Isolated remote program exceeded 45 seconds")
    raise SystemExit(status)

if name == "tar":
    import tarfile

    portable = args[:3] == ["--format=ustar", "--no-xattrs", "--no-acls"]
    if portable:
        args = args[3:]
    if args[:2] == ["-cf", "-"]:
        if not portable:
            fail("Upload must use portable metadata-free ustar")
        args = args[2:]
        directory = None
        sources = []
        while args:
            value = args.pop(0)
            if value == "-C":
                directory = Path(args.pop(0))
                continue
            if directory == repository / "rootfs/common" and value in common:
                pass
            elif directory == repository / "rootfs/physical" and value in physical:
                pass
            elif directory == repository / "config" and value == "authorized_keys":
                pass
            else:
                fail("Non-allowlisted upload")
            source = directory / value
            if source.is_symlink() or not source.is_file():
                fail("Uploaded source is not regular")
            sources.append((source, value))
        event("upload", [value for _, value in sources])
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
            for source, value in sources:
                archive.add(source, arcname=value, recursive=False)
        raise SystemExit(0)
    if args[:2] != ["--extract", "--file=-"] or not args[2].startswith("--directory="):
        fail("Unexpected tar extraction")
    if args[3:6] != ["--no-same-owner", "--no-same-permissions", "--"]:
        fail("Unsafe extraction options")
    expected = args[6:]
    if any(value not in (*common, *physical, "authorized_keys") for value in expected):
        fail("Unsafe extraction member")
    destination = safe(args[2].split("=", 1)[1])
    if stat.S_IMODE(destination.stat().st_mode) != 0o700:
        fail("Upload staging is not private")
    if config.get("upload_failure"):
        raise SystemExit(1)
    seen = []
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        for member in archive:
            if member.pax_headers:
                fail("Extended archive metadata is not allowed")
            if member.name not in expected or not member.isfile():
                fail("Unsafe archive member")
            seen.append(member.name)
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(0o600)
    if seen != expected:
        fail("Incomplete configuration upload")
    if config.get("corrupt_uploaded_script"):
        (destination / common[0]).write_text("if then\n")
    event("extracted", seen)
    raise SystemExit(0)


if name == "sudo":
    if args[:8] != ["-n", "bash", "--noprofile", "--norc", "-euo", "pipefail", "-s", "--"] or len(args) < 13:
        fail("Unexpected privileged operation")
    stage = safe(args[8])
    if stat.S_IMODE(stage.stat().st_mode) != 0o700:
        fail("Upload staging is not private")
    event("sudo")
    os.execv(os.environ["FULL_SYNC_BASH"], ["bash", *args[2:]])

if name == "hostname":
    if args != ["-s"]:
        fail("Unsafe hostname operation")
    host = os.environ["FULL_SYNC_HOST"]
    print(config.get("hostname_by_host", {}).get(host, config.get("hostname", host)))
    raise SystemExit(0)

if name == "id":
    if args not in (["-u", "pi"], ["-g", "pi"]):
        fail("Unexpected account query")
    print(config.get("pi_uid", 1000) if args[0] == "-u" else 1000)
    raise SystemExit(0)

if name == "getent":
    if args != ["passwd", "pi"]:
        fail("Unexpected account database query")
    print("pi:x:1000:1000::" + config.get("pi_home", "/home/pi") + ":/bin/bash")
    raise SystemExit(0)

if name == "systemctl":
    event(name, *args)
    if args[:3] == ["show", "--property=LoadState", "--value"] and len(args) == 4:
        role = config.get("role", "server")
        loaded = role == "ambiguous" or (role == "server" and args[3] == "k3s.service") or (
            role == "agent" and args[3] == "k3s-agent.service")
        print("loaded" if loaded else "not-found")
        raise SystemExit(0 if loaded else config.get("missing_unit_status", 1))
    if args == ["show", "--property=ActiveState", "--value", "node-health.timer"]:
        print(state["timer_state"])
        raise SystemExit(0)
    if args == ["show", "--property=ActiveState", "--value", "node-health.service"]:
        values = config.get("service_states", ["inactive"])
        index = state["service_reads"]
        state["service_reads"] += 1
        state["service_state"] = values[min(index, len(values) - 1)]
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
        failed = state["timer_starts"] in config.get("timer_start_failures", [])
        if not failed:
            state["timer_state"] = "active"
        elif config.get("timer_start_failure_runs_service"):
            state["service_state"] = "active"
        save()
        raise SystemExit(1 if failed else 0)
    if args == ["daemon-reload"]:
        state["reloads"] += 1
        save()
        raise SystemExit(1 if state["reloads"] in config.get("reload_failures", []) else 0)
    fail("Forbidden service operation")

if name == "sleep":
    if args != ["1"]:
        fail("Unexpected wait")
    event("wait")
    raise SystemExit(0)

if name == "mktemp":
    import tempfile

    directory = args[:1] == ["-d"]
    template = safe(args[-1])
    if protected_write(template):
        fail("Protected SSH candidate creation")
    prefix = template.name.rstrip("X")
    if directory:
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=template.parent))
    else:
        descriptor, filename = tempfile.mkstemp(prefix=prefix, dir=template.parent)
        os.close(descriptor)
        path = Path(filename)
    event("temporary", relative(path), oct(stat.S_IMODE(path.stat().st_mode)))
    print(relative(path))
    raise SystemExit(0)

if name == "install":
    directory = False
    owner = group = "root"
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
            fail("Unexpected installation option")
    if not args or args.pop(0) != "--" or mode is None:
        fail("Unsafe installation arguments")
    if directory:
        for value in args:
            path = safe(value)
            item = relative(path)
            if item in protected_parents:
                fail("Protected SSH directory mutation")
            if item not in parents and item not in context_directories and not item.startswith("run/nodes-config."):
                fail("Directory creation outside allowlist")
            if item in context_directories and (path.exists() or mode != 0o700 or ids(owner, group) != [0, 0]):
                fail("Unsafe durable context directory mutation")
            if item in parents and path.exists():
                fail("Overwriting unmanaged directory metadata")
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)
            state["owners"][item] = ids(owner, group)
            event("directory", item, mode, ids(owner, group))
        save()
        raise SystemExit(0)
    if len(args) != 2:
        fail("Unexpected file installation")
    source, target = map(safe, args)
    if protected_write(target):
        fail("Protected SSH file installation")
    if relative(target).startswith(context_dir + "/"):
        if not context_temporary(target) or relative(source) != "etc/chrony/conf.d/ip-fallback.conf" or mode != 0o600 or ids(owner, group) != [0, 0]:
            fail("Immutable durable chrony context overwrite")
    if config.get("candidate_install_failure") and ".nodes-config." in target.name:
        raise SystemExit(1)
    import shutil

    shutil.copyfile(source, target)
    target.chmod(mode)
    state["owners"][relative(target)] = ids(owner, group)
    save()
    event("candidate", relative(target))
    raise SystemExit(0)

if name == "cp":
    if args[:2] != ["-a", "--"] or len(args) != 4:
        fail("Unsafe backup copy")
    source, target = map(safe, args[2:])
    if protected_write(target):
        fail("Protected SSH copy destination")
    if config.get("rollback_copy_failure") and "/backup/" in str(source):
        raise SystemExit(1)
    import shutil

    shutil.copy2(source, target)
    state["owners"][relative(target)] = state["owners"].get(relative(source), [0, 0])
    save()
    raise SystemExit(0)

if name == "mv":
    if args[:2] != ["-fT", "--"] or len(args) != 4:
        fail("Non-atomic replacement")
    source, target = map(safe, args[2:])
    if protected_write(target):
        fail("Protected SSH replacement")
    if relative(target) not in assets:
        fail("Replacement outside allowlist")
    if state["service_state"] not in ("inactive", "failed"):
        fail("Replacement while health service is running")
    state["moves"] += 1
    if state["moves"] == 1:
        policy = root / "etc/chrony/conf.d/ip-fallback.conf"
        receipt_present = policy.is_file() and any(
            path.is_file() and not path.is_symlink() and path.read_bytes() == policy.read_bytes()
            for path in (root / context_dir).glob("*.conf")
        )
        event("chrony-context-before-first-replace", receipt_present)
    save()
    if state["moves"] in config.get("move_failures", []):
        raise SystemExit(1)
    os.replace(source, target)
    state["owners"][relative(target)] = state["owners"].pop(relative(source), [0, 0])
    save()
    event("replace", relative(target))
    raise SystemExit(0)

if name == "rm":
    if args[:2] not in (["-f", "--"], ["-rf", "--"]):
        fail("Unsafe cleanup")
    for value in args[2:]:
        path = safe(value)
        item = relative(path)
        if protected_write(path):
            fail("Protected SSH removal")
        if item.startswith(context_dir + "/") and not context_temporary(path):
            fail("Immutable durable chrony context removal")
        if path.is_dir():
            if not re.fullmatch(r"(?:tmp|run)/nodes-config\.[^/]+", item):
                fail("Recursive deletion outside staging")
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        state["owners"] = {key: value for key, value in state["owners"].items()
                           if key != item and not key.startswith(item + "/")}
    save()
    raise SystemExit(0)

if name == "rmdir":
    if len(args) != 2 or args[0] != "--":
        fail("Unsafe directory rollback")
    path = safe(args[1])
    if relative(path) in protected_parents:
        fail("Protected SSH directory removal")
    if relative(path) not in parents:
        fail("Directory rollback outside allowlist")
    path.rmdir()
    state["owners"].pop(relative(path), None)
    save()
    raise SystemExit(0)

if name == "stat":
    if args[:3] != ["-c", "%a:%u:%g", "--"] or len(args) != 4:
        fail("Unexpected metadata query")
    path = safe(args[3])
    item = relative(path)
    if item.startswith(context_dir + "/") and item not in state["owners"]:
        fail("Unmodeled durable context ownership")
    uid, gid = state["owners"].get(item, [0, 0])
    print(f"{stat.S_IMODE(path.stat().st_mode):o}:{uid}:{gid}")
    raise SystemExit(0)

if name == "cmp":
    if args[:2] != ["-s", "--"] or len(args) != 4:
        fail("Unsafe content comparison")
    left, right = map(safe, args[2:])
    raise SystemExit(0 if left.read_bytes() == right.read_bytes() else 1)

if name in ("chmod", "chown"):
    fail("Protected SSH metadata mutation")

if name == "python3":
    if args[:1] == ["-"]:
        import subprocess

        context_operation = args[1] if len(args) >= 2 and args[1] in (
            "--chrony-temp", "--chrony-validate", "--chrony-publish"
        ) else None
        candidate = publication = None
        if context_operation:
            expected_count = 3 if context_operation == "--chrony-publish" else 2
            if len(args) != expected_count:
                fail("Unexpected durable context operation")
            safe(context_dir)
            if context_operation == "--chrony-publish":
                import hashlib

                candidate = safe(args[2])
                if not context_temporary(candidate):
                    fail("Invalid durable context candidate")
                candidate_owners = state["owners"][relative(candidate)]
                candidate_inode = candidate.stat().st_ino
                publication = safe(context_dir + "/" + hashlib.sha256(candidate.read_bytes()).hexdigest() + ".conf")
        else:
            if len(args) not in (2, 3):
                fail("Unexpected key comparison")
            for value in args[1:]:
                safe(value)
        result = subprocess.run([sys.executable, *args], cwd=root, timeout=10, stdout=subprocess.PIPE)
        if context_operation == "--chrony-temp" and result.returncode == 0:
            candidate = safe(result.stdout.decode().strip())
            if not context_temporary(candidate) or candidate.is_symlink() or not candidate.is_file():
                fail("Invalid durable context temporary")
            state["owners"][relative(candidate)] = [0, 0]
        elif context_operation == "--chrony-publish":
            if publication.exists() and not publication.is_symlink() and publication.stat().st_ino == candidate_inode:
                state["owners"][relative(publication)] = candidate_owners
            if not candidate.exists():
                state["owners"].pop(relative(candidate), None)
        if context_operation:
            save()
        sys.stdout.buffer.write(result.stdout)
        raise SystemExit(result.returncode)
    if args[:1] == ["-c"]:
        if len(args) != 3:
            fail("Unexpected candidate validation")
        path = safe(args[2])
        if path.suffix == ".py":
            import ast

            ast.parse(path.read_text())
        elif path.suffix == ".json":
            json.loads(path.read_text())
        elif path.suffix in (".conf", ".link", ".service", ".timer"):
            import configparser

            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read_string(path.read_text())
        else:
            fail("Unknown candidate type")
        raise SystemExit(0)
    path = safe(args[0])
    check = "--check" in args
    if check and not re.fullmatch(r"run/nodes-config\.[^/]+/desired/usr/local/bin/node-configure.py", relative(path)):
        fail("Check executed stale installed helper")
    if not check and relative(path) != "usr/local/bin/node-configure.py":
        fail("Apply did not execute committed helper")
    if not check and config.get("interrupt_before_helper"):
        import signal

        event("helper-not-started")
        os.kill(os.getppid(), signal.SIGTERM)
        raise SystemExit(143)
    import subprocess

    result = subprocess.run([sys.executable, str(path), *args[1:]], cwd=root, timeout=10)
    raise SystemExit(result.returncode)

if name == "systemd-analyze":
    import configparser

    if args[:2] != ["verify", "--man=no"] or len(args) != 5:
        fail("Unexpected unit validation")
    for value in args[2:]:
        path = safe(value)
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            unit.read_string(path.read_text())
            section = "Timer" if path.suffix == ".timer" else "Service"
            if section not in unit:
                raise ValueError("Missing unit section")
        except (configparser.Error, ValueError):
            raise SystemExit(1)
        if section == "Service":
            executable = root / unit[section]["ExecStart"].lstrip("/")
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise SystemExit(1)
    event("verified-units")
    raise SystemExit(0)

fail("Unexpected isolated command")
'''


class FullSyncFixture:
    def __init__(self, directory, platform="cloud", matching=False, absent=(), **config):
        self.base = Path(directory)
        self.repository = self.base / "repository"
        self.root = self.base / "target"
        self.bin = self.base / "bin"
        self.config_path = self.base / "config.json"
        self.state_path = self.base / "state.json"
        self.calls_path = self.base / "calls.jsonl"
        self.platform = platform
        self.bin.mkdir()
        self.repository.mkdir()
        for relative in ("nodes", "lib/common.sh", "lib/commands.sh", "lib/render.sh"):
            target = self.repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / relative, target)
        self.sources = {}
        for subtree, relatives in (("common", COMMON), ("physical", PHYSICAL)):
            for relative in relatives:
                source = self.repository / "rootfs" / subtree / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                if relative == "usr/local/bin/node-configure.py":
                    source.write_text(HELPER)
                elif relative == "usr/local/share/nodes/node-policy.json":
                    source.write_text('{"policy": "desired"}\n')
                else:
                    shutil.copyfile(PROJECT / "rootfs" / subtree / relative, source)
                if subtree == "common" or platform == "physical" or relative not in COMMON:
                    self.sources[relative] = source.read_bytes()
        keys = self.repository / "config/authorized_keys"
        keys.parent.mkdir()
        keys.write_text(authorized_key())
        self.sources[KEYS] = keys.read_bytes()
        for relative in (
            "rootfs/common/etc/unmanaged-spill",
            "rootfs/physical/etc/unmanaged-physical",
            "config/id_ed25519",
            "config/user-data",
        ):
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must never upload\n")
        self.assets = tuple(dict.fromkeys((*COMMON, *(PHYSICAL if platform == "physical" else ()), KEYS)))
        owners = {}
        self.originals = {}
        for asset in self.assets:
            path = self.root / asset
            path.parent.mkdir(parents=True, exist_ok=True)
            if asset not in absent:
                asset_matches = matching or asset in PROTECTED_SSH
                path.write_bytes(self.sources[asset] if asset_matches else ("previous " + asset + "\n").encode())
                path.chmod(self.mode(asset) if asset_matches else 0o640)
                os.utime(path, ns=(1_600_000_000_123_456_789,) * 2)
                owners[asset] = self.owner(asset) if asset_matches or asset == CHRONY else [1000, 1000]
                self.originals[asset] = self.file_state(path)
            else:
                self.originals[asset] = None
        self.original_owners = dict(owners)
        self.sentinels = {}
        for relative in (
            "home/pi/.ssh/id_ed25519",
            "etc/systemd/system/k3s.service",
            "etc/systemd/system/unrelated.service",
            "etc/rancher/k3s/config.yaml.d/10-server-ha.yaml",
            "etc/rancher/k3s/k3s.yaml",
            "etc/rancher/node/password",
            "var/lib/rancher/k3s/server/token",
            "var/lib/tailscale/tailscaled.state",
            "var/lib/node-health/reboots",
            "var/lib/node-health/wifi-link-reset-window",
            "var/log/syslog",
            "var/log/syslog.1",
            "var/log/journal/retained.log",
            "etc/netplan/50-cloud-init.yaml",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE-RETAIN " + relative + "\n")
            path.chmod(0o640)
            self.sentinels[relative] = self.file_state(path)
        (self.root / "etc/os-release").write_text("ID=" + config.pop("os_id", "ubuntu") + "\n")
        for relative in ("tmp", "run"):
            (self.root / relative).mkdir()
        ssh_directory = self.root / "home/pi/.ssh"
        ssh_directory.chmod(0o700)
        owners["home/pi/.ssh"] = [1000, 1000]
        self.ssh_original = (stat.S_IMODE(ssh_directory.stat().st_mode), owners["home/pi/.ssh"])
        self.config_path.write_text(json.dumps(config))
        self.state_path.write_text(json.dumps({
            "owners": owners,
            "timer_enabled": config.get("timer_enabled", "enabled" if matching else "disabled"),
            "timer_state": config.get("timer_state", "active"),
            "service_state": "inactive",
            "service_reads": 0,
            "timer_starts": 0,
            "reloads": 0,
            "moves": 0,
            "runtime_runs": 0,
        }))
        self.calls_path.write_text("")
        driver = self.bin / "driver"
        driver.write_text(f"#!{sys.executable}\n" + DRIVER)
        driver.chmod(0o755)
        for command in (
            "ssh", "tar", "sudo", "timeout", "hostname", "id", "getent",
            "systemctl", "sleep", "mktemp", "install", "cp", "mv", "rm",
            "rmdir", "stat", "cmp", "chmod", "chown", "python3", "systemd-analyze",
            "sysctl", "journalctl", "truncate", "networkctl", "tailscale", "k3s",
            "find", "reboot", "netplan",
        ):
            (self.bin / command).symlink_to(driver)
        bash_environment = self.base / "bash-env"
        bash_environment.write_text(
            'cd() { if [[ "$#" == 1 && "$1" == / ]]; then '
            'builtin cd "$FULL_SYNC_ROOT"; else builtin cd "$@"; fi; }\ncd /\n'
        )
        self.environment = os.environ.copy()
        self.environment.pop("NODES_SSH_CONFIG", None)
        self.environment.pop("BASH_ENV", None)
        self.environment.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FULL_SYNC_ROOT": str(self.root),
            "FULL_SYNC_REPOSITORY": str(self.repository),
            "FULL_SYNC_CONFIG": str(self.config_path),
            "FULL_SYNC_STATE": str(self.state_path),
            "FULL_SYNC_CALLS": str(self.calls_path),
            "FULL_SYNC_BASH_ENV": str(bash_environment),
            "FULL_SYNC_BASH": shutil.which("bash"),
            "FULL_SYNC_COMMON": json.dumps(COMMON),
            "FULL_SYNC_PHYSICAL": json.dumps(PHYSICAL),
            "FULL_SYNC_PROTECTED": json.dumps(PROTECTED_SSH),
        })

    @staticmethod
    def mode(asset):
        return 0o600 if asset == KEYS else 0o755 if asset.startswith("usr/local/bin/") else 0o644

    @staticmethod
    def owner(asset):
        return [1000, 1000] if asset == KEYS else [0, 0]

    @staticmethod
    def file_state(path):
        info = path.stat()
        return path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_mtime_ns

    def run(self, *arguments):
        return subprocess.run(
            [self.environment["FULL_SYNC_BASH"], str(self.repository / "nodes"), "sync", *arguments],
            cwd=self.repository, env=self.environment, text=True, capture_output=True, timeout=60,
        )

    def apply(self, *hostnames):
        return self.run("--platform", self.platform, *(hostnames or ("cloud-1",)))

    def check(self):
        return self.run("--platform", self.platform, "--check", "cloud-1")

    def desired_keys(self, text):
        (self.repository / "config/authorized_keys").write_text(text)
        self.sources[KEYS] = text.encode()

    def configure(self, **updates):
        config = json.loads(self.config_path.read_text())
        config.update(updates)
        self.config_path.write_text(json.dumps(config))

    def seed_chrony_context(self, *policies):
        state = self.state()
        for relative in ("var/lib/nodes", CHRONY_PENDING):
            directory = self.root / relative
            directory.mkdir(exist_ok=True)
            directory.chmod(0o700)
            state["owners"][relative] = [0, 0]
        for policy in policies:
            relative = CHRONY_PENDING + "/" + hashlib.sha256(policy).hexdigest() + ".conf"
            path = self.root / relative
            path.write_bytes(policy)
            path.chmod(0o600)
            state["owners"][relative] = [0, 0]
        self.state_path.write_text(json.dumps(state))

    def events(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def state(self):
        return json.loads(self.state_path.read_text())

    def snapshot(self):
        files = {str(path.relative_to(self.root)): self.file_state(path)
                 for path in self.root.rglob("*") if path.is_file()}
        owners = {key: value for key, value in self.state()["owners"].items()
                  if not key.startswith(("run/", "tmp/"))}
        directories = {str(path.relative_to(self.root)): stat.S_IMODE(path.stat().st_mode)
                       for path in self.root.rglob("*") if path.is_dir()}
        return files, owners, directories


class NodeFullSyncTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def fixture(self, **config):
        directory = Path(self.temporary.name) / str(len(list(Path(self.temporary.name).iterdir())))
        directory.mkdir()
        return FullSyncFixture(directory, **config)

    def assert_chrony_context(self, fixture, policies):
        directory = fixture.root / CHRONY_PENDING
        self.assertTrue(directory.is_dir())
        self.assertFalse(directory.is_symlink())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(fixture.state()["owners"][CHRONY_PENDING], [0, 0])
        expected = {hashlib.sha256(policy).hexdigest() + ".conf": policy for policy in policies}
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, expected)
        for path in directory.iterdir():
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(fixture.state()["owners"][str(path.relative_to(fixture.root))], [0, 0])

    def assert_unchanged_except_chrony_context(self, fixture, before, policies):
        self.assert_chrony_context(fixture, policies)
        expected_paths = {
            CHRONY_PENDING + "/" + hashlib.sha256(policy).hexdigest() + ".conf" for policy in policies
        } | {"var/lib/nodes", CHRONY_PENDING}
        for actual, previous in zip(fixture.snapshot(), before):
            filtered = {path: value for path, value in actual.items()
                        if path in previous or path not in expected_paths}
            self.assertEqual(filtered, previous)

    def assert_clean_and_preserved(self, fixture):
        for relative, expected in fixture.sentinels.items():
            self.assertEqual(fixture.file_state(fixture.root / relative), expected, relative)
        self.assertFalse(list(fixture.root.rglob("*.nodes-config.*")))
        self.assertEqual(list((fixture.root / "tmp").iterdir()), [])
        self.assertEqual(list((fixture.root / "run").iterdir()), [])
        self.assertFalse([event for event in fixture.events() if event[0] == "rejected"])

    def assert_desired(self, fixture):
        for asset in fixture.assets:
            path = fixture.root / asset
            self.assertEqual(path.read_bytes(), fixture.sources[asset], asset)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), fixture.mode(asset), asset)
            self.assertEqual(fixture.state()["owners"][asset], fixture.owner(asset), asset)
        self.assertEqual(stat.S_IMODE((fixture.root / "home/pi/.ssh").stat().st_mode), 0o700)
        self.assertEqual(fixture.state()["owners"]["home/pi/.ssh"], [1000, 1000])

    def assert_originals(self, fixture):
        for asset, original in fixture.originals.items():
            path = fixture.root / asset
            if original is None:
                self.assertFalse(path.exists(), asset)
            else:
                self.assertEqual(fixture.file_state(path), original, asset)
                self.assertEqual(fixture.state()["owners"][asset], fixture.original_owners[asset], asset)
        self.assertEqual(stat.S_IMODE((fixture.root / "home/pi/.ssh").stat().st_mode), fixture.ssh_original[0])
        self.assertEqual(fixture.state()["owners"]["home/pi/.ssh"], fixture.ssh_original[1])

    def test_cloud_and_physical_apply_only_their_owned_files_with_exact_metadata(self):
        for platform in ("cloud", "physical"):
            with self.subTest(platform=platform):
                fixture = self.fixture(platform=platform, service_states=["active", "activating", "inactive"])
                result = fixture.apply()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_desired(fixture)
                self.assert_clean_and_preserved(fixture)
                self.assertEqual(fixture.state()["timer_enabled"], "enabled")
                uploaded = next(event[1] for event in fixture.events() if event[0] == "upload")
                self.assertEqual(set(uploaded), set(COMMON) | (set(PHYSICAL) if platform == "physical" else set()) | {"authorized_keys"})
                for relative in set(PHYSICAL) - set(COMMON):
                    self.assertEqual((fixture.root / relative).exists(), platform == "physical")
                self.assertIn(["runtime", False, platform, "server", False, True], fixture.events())
                self.assertNotIn("PRIVATE-RETAIN", result.stdout + result.stderr)

    def test_effective_overlay_uses_physical_override_once_and_cloud_keeps_common(self):
        common = b"server common.example iburst nts\n"
        physical = b"server physical.example iburst nts ipv4\n"
        for platform in ("cloud", "physical"):
            with self.subTest(platform=platform):
                fixture = self.fixture(platform=platform)
                (fixture.repository / "rootfs/common" / CHRONY).write_bytes(common)
                (fixture.repository / "rootfs/physical" / CHRONY).write_bytes(physical)
                expected = physical if platform == "physical" else common
                fixture.sources[CHRONY] = expected
                result = fixture.apply()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((fixture.root / CHRONY).read_bytes(), expected)
                self.assert_desired(fixture)
                uploaded = next(event[1] for event in fixture.events() if event[0] == "upload")
                self.assertEqual(uploaded.count(CHRONY), 1)
                self.assertEqual(len(uploaded), len(fixture.assets))
                result = fixture.check()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("match /" + CHRONY + "\n"), 1)
                self.assert_clean_and_preserved(fixture)

    def test_selected_physical_override_rejects_symlink_before_transport(self):
        fixture = self.fixture(platform="physical")
        source = fixture.repository / "rootfs/physical" / CHRONY
        moved = fixture.base / "physical-policy"
        source.rename(moved)
        source.symlink_to(moved)
        before = fixture.snapshot()
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.events(), [])
        self.assertEqual(fixture.snapshot(), before)

    def test_chrony_context_survives_pending_and_matching_file_resync(self):
        fixture = self.fixture(runtime_status=2)
        previous = (fixture.root / CHRONY).read_bytes()
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assert_desired(fixture)
        self.assert_chrony_context(fixture, (previous,))
        self.assertIn(["chrony-context-before-first-replace", True], fixture.events())
        receipt = next((fixture.root / CHRONY_PENDING).iterdir())
        identity = (receipt.name, receipt.stat().st_ino, fixture.file_state(receipt))
        moves = fixture.state()["moves"]
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(fixture.state()["moves"], moves)
        self.assertEqual((receipt.name, receipt.stat().st_ino, fixture.file_state(receipt)), identity)
        self.assert_chrony_context(fixture, (previous,))
        self.assertEqual([event for event in fixture.events() if event[0] == "chrony-context-count"],
                         [["chrony-context-count", 1], ["chrony-context-count", 1]])
        self.assertNotIn(hashlib.sha256(previous).hexdigest(), result.stdout + result.stderr)
        self.assert_clean_and_preserved(fixture)

    def test_chrony_context_survives_interruption_after_commit_before_helper_and_retry(self):
        fixture = self.fixture(interrupt_before_helper=True, runtime_status=2)
        previous = (fixture.root / CHRONY).read_bytes()
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assert_desired(fixture)
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assertIn(["helper-not-started"], fixture.events())
        self.assert_chrony_context(fixture, (previous,))
        fixture.configure(interrupt_before_helper=False)
        moves = fixture.state()["moves"]
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(fixture.state()["moves"], moves)
        self.assertIn(["runtime", False, "cloud", "server", False, True], fixture.events())
        self.assert_chrony_context(fixture, (previous,))
        self.assert_clean_and_preserved(fixture)

    def test_chrony_context_precedes_first_replacement_and_survives_rollback(self):
        fixture = self.fixture(move_failures=[1])
        previous = (fixture.root / CHRONY).read_bytes()
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(["chrony-context-before-first-replace", True], fixture.events())
        self.assert_originals(fixture)
        self.assert_chrony_context(fixture, (previous,))
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assert_clean_and_preserved(fixture)

    def test_chrony_context_keeps_distinct_versions_and_never_rewrites_existing_receipt(self):
        fixture = self.fixture(matching=True, runtime_status=2)
        first = b"server old-one.example iburst\n"
        second = b"server old-two.example iburst\n"
        (fixture.root / CHRONY).write_bytes(first)
        fixture.seed_chrony_context(first)
        receipt = next((fixture.root / CHRONY_PENDING).iterdir())
        identity = (receipt.stat().st_ino, fixture.file_state(receipt))
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((receipt.stat().st_ino, fixture.file_state(receipt)), identity)
        (fixture.root / CHRONY).write_bytes(second)
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assert_chrony_context(fixture, (first, second))
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assert_chrony_context(fixture, (first, second))
        self.assertEqual((receipt.stat().st_ino, fixture.file_state(receipt)), identity)
        self.assert_clean_and_preserved(fixture)

    def test_safe_unpublished_chrony_temporary_is_retained_but_not_consumed(self):
        fixture = self.fixture(matching=True, runtime_status=2)
        fixture.seed_chrony_context(b"server retired.example iburst nts\n")
        relative = CHRONY_PENDING + "/.snapshot-orphan.tmp"
        temporary = fixture.root / relative
        temporary.write_bytes(b"uncommitted policy bytes\n")
        temporary.chmod(0o600)
        state = fixture.state()
        state["owners"][relative] = [0, 0]
        fixture.state_path.write_text(json.dumps(state))
        before = fixture.snapshot()
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertIn(["chrony-context-count", 1], fixture.events())
        self.assertEqual(fixture.state()["moves"], 0)
        self.assert_clean_and_preserved(fixture)

    def test_oversized_old_chrony_policy_cannot_commit_an_unreadable_snapshot(self):
        fixture = self.fixture()
        (fixture.root / CHRONY).write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        before = fixture.snapshot()
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assert_unchanged_except_chrony_context(fixture, before, ())
        self.assertEqual(fixture.state()["moves"], 0)
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assert_clean_and_preserved(fixture)

    def test_check_never_creates_or_updates_chrony_context(self):
        fixture = self.fixture()
        before = fixture.snapshot()
        result = fixture.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.snapshot(), before)
        self.assertFalse((fixture.root / CHRONY_PENDING).exists())
        fixture.seed_chrony_context((fixture.root / CHRONY).read_bytes())
        before = fixture.snapshot()
        result = fixture.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.snapshot(), before)
        self.assert_clean_and_preserved(fixture)

    def test_unsafe_chrony_context_is_rejected_before_any_managed_replacement(self):
        for kind in (
            "parent-link", "parent-mode", "parent-owner",
            "directory-link", "directory-mode", "directory-owner",
            "record-link", "record-mode", "record-owner", "record-bytes",
            "temporary-link", "temporary-mode", "temporary-owner",
            "unknown-entry", "source-owner", "source-mode",
        ):
            with self.subTest(kind=kind):
                fixture = self.fixture()
                previous = (fixture.root / CHRONY).read_bytes()
                fixture.seed_chrony_context(previous)
                directory = fixture.root / CHRONY_PENDING
                receipt = next(directory.iterdir())
                state = fixture.state()
                if kind == "parent-link":
                    parent = directory.parent
                    moved = fixture.base / "moved-parent"
                    parent.rename(moved)
                    parent.symlink_to(moved, target_is_directory=True)
                elif kind == "parent-mode":
                    directory.parent.chmod(0o770)
                elif kind == "parent-owner":
                    state["owners"]["var/lib/nodes"] = [1000, 1000]
                elif kind == "directory-link":
                    moved = fixture.base / "moved-context"
                    directory.rename(moved)
                    directory.symlink_to(moved, target_is_directory=True)
                elif kind == "directory-mode":
                    directory.chmod(0o755)
                elif kind == "directory-owner":
                    state["owners"][CHRONY_PENDING] = [1000, 1000]
                elif kind == "record-link":
                    moved = fixture.base / "moved-receipt"
                    receipt.rename(moved)
                    receipt.symlink_to(moved)
                elif kind == "record-mode":
                    receipt.chmod(0o644)
                elif kind == "record-owner":
                    state["owners"][str(receipt.relative_to(fixture.root))] = [1000, 1000]
                elif kind == "record-bytes":
                    receipt.write_bytes(b"not the named policy\n")
                elif kind.startswith("temporary-"):
                    relative = CHRONY_PENDING + "/.snapshot-unsafe.tmp"
                    temporary = fixture.root / relative
                    if kind == "temporary-link":
                        moved = fixture.base / "moved-temporary"
                        moved.write_bytes(b"retain unrelated bytes\n")
                        temporary.symlink_to(moved)
                    else:
                        temporary.write_bytes(b"uncommitted policy bytes\n")
                        temporary.chmod(0o644 if kind == "temporary-mode" else 0o600)
                        state["owners"][relative] = [1000, 1000] if kind == "temporary-owner" else [0, 0]
                elif kind == "unknown-entry":
                    (directory / "unexpected").write_bytes(b"retain unknown state\n")
                elif kind == "source-mode":
                    (fixture.root / CHRONY).chmod(0o664)
                else:
                    state["owners"][CHRONY] = [1000, 1000]
                fixture.state_path.write_text(json.dumps(state))
                before = fixture.snapshot()
                result = fixture.apply()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.snapshot(), before)
                self.assertEqual(fixture.state()["moves"], 0)
                self.assertEqual(fixture.state()["runtime_runs"], 0)
                self.assertNotIn(hashlib.sha256(previous).hexdigest(), result.stdout + result.stderr)
                self.assert_clean_and_preserved(fixture)

    def test_matching_files_preserve_mtime_and_avoid_file_activation(self):
        fixture = self.fixture(matching=True)
        before = fixture.snapshot()
        result = fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual(fixture.state()["moves"], 0)
        self.assertEqual(fixture.state()["reloads"], 0)
        self.assertEqual(fixture.state()["timer_starts"], 0)
        self.assertEqual(fixture.state()["runtime_runs"], 1)
        self.assert_clean_and_preserved(fixture)

    def test_single_changed_file_does_not_replace_matching_assets(self):
        fixture = self.fixture(matching=True)
        asset = "etc/chrony/conf.d/ip-fallback.conf"
        (fixture.root / asset).write_text("old managed chrony source\n")
        before = {name: fixture.file_state(fixture.root / name) for name in fixture.assets if name != asset}
        result = fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([event[1] for event in fixture.events() if event[0] == "replace"], [asset])
        for name, expected in before.items():
            self.assertEqual(fixture.file_state(fixture.root / name), expected)
        self.assert_clean_and_preserved(fixture)

    def test_equivalent_authorized_keys_preserve_comments_order_options_and_mtime(self):
        fixture = self.fixture(matching=True)
        first = r'command="echo \"hi there\"",restrict'
        second = 'from="host-a,host-b",no-agent-forwarding'
        fixture.desired_keys(
            authorized_key(1, first, "source first") + authorized_key(2, second, "source second")
        )
        presentation = (
            "# cloud-init presentation\n\n"
            + "  " + authorized_key(2, 'FROM="host-a,host-b",no-agent-forwarding', 'cloud "comment')
            + "\t" + authorized_key(1, r'COMMAND="echo \"hi there\"",restrict', "different comment")
        ).replace(" ssh-ed25519 ", "\tssh-ed25519\t")
        target = fixture.root / KEYS
        target.write_text(presentation)
        os.utime(target, ns=(1_600_000_000_123_456_789,) * 2)
        before = fixture.snapshot()
        result = fixture.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        result = fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual(fixture.state()["moves"], 0)
        self.assertEqual(fixture.state()["reloads"], 0)
        self.assert_clean_and_preserved(fixture)

    def test_changed_key_material_options_and_duplicate_counts_remain_pending_without_writes(self):
        desired = authorized_key(1, "no-agent-forwarding") + authorized_key(2)
        variants = (
            authorized_key(1, "no-agent-forwarding"),
            desired + authorized_key(3),
            desired + authorized_key(1, "no-agent-forwarding"),
            authorized_key(1, "no-port-forwarding") + authorized_key(2),
            authorized_key(3, "no-agent-forwarding") + authorized_key(2),
        )
        for actual in variants:
            with self.subTest(actual=actual):
                fixture = self.fixture(matching=True)
                fixture.desired_keys(desired)
                (fixture.root / KEYS).write_text(actual)
                before = fixture.snapshot()
                result = fixture.check()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(fixture.snapshot(), before)
                result = fixture.apply()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(fixture.snapshot(), before)
                self.assertEqual((fixture.root / KEYS).read_text(), actual)
                self.assertFalse([event for event in fixture.events() if event[0] == "replace"])
                for asset in fixture.assets:
                    if asset != KEYS:
                        self.assertEqual(fixture.file_state(fixture.root / asset), before[0][asset])
                self.assert_clean_and_preserved(fixture)

    def test_same_key_restriction_precedence_drift_is_detected_without_rewriting_authorization(self):
        fixture = self.fixture(matching=True)
        restricted = authorized_key(1, 'command="/usr/bin/true",restrict', "restricted first")
        unrestricted = authorized_key(1, comment="unrestricted later")
        other = authorized_key(2, comment="independent key")
        desired = restricted + other + unrestricted
        fixture.desired_keys(desired)
        target = fixture.root / KEYS
        target.write_text(unrestricted + other + restricted)
        before = fixture.snapshot()
        result = fixture.check()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(fixture.snapshot(), before)
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual(target.read_text(), unrestricted + other + restricted)
        self.assertFalse([event for event in fixture.events() if event[0] == "replace"])
        for asset in fixture.assets:
            if asset != KEYS:
                self.assertEqual(fixture.file_state(fixture.root / asset), before[0][asset])
        self.assert_clean_and_preserved(fixture)

    def test_security_significant_option_order_is_not_mistaken_for_presentation(self):
        fixture = self.fixture(matching=True)
        desired = authorized_key(1, "restrict,port-forwarding")
        fixture.desired_keys(desired)
        (fixture.root / KEYS).write_text(authorized_key(1, "port-forwarding,restrict"))
        before = fixture.snapshot()
        result = fixture.check()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(fixture.snapshot(), before)
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual((fixture.root / KEYS).read_text(), authorized_key(1, "port-forwarding,restrict"))
        self.assert_clean_and_preserved(fixture)

    def test_equivalent_key_metadata_drift_is_pending_without_presentation_or_metadata_changes(self):
        fixture = self.fixture(matching=True)
        target = fixture.root / KEYS
        presentation = "# retain cloud-init comment\n" + authorized_key(1, comment="native cloud key")
        target.write_text(presentation)
        target.chmod(0o644)
        os.utime(target, ns=(1_600_000_000_123_456_789,) * 2)
        original_mtime = target.stat().st_mtime_ns
        state = fixture.state()
        state["owners"][KEYS] = [0, 0]
        fixture.state_path.write_text(json.dumps(state))
        before = fixture.snapshot()
        result = fixture.check()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(fixture.snapshot(), before)
        result = fixture.apply()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual(target.read_text(), presentation)
        self.assertEqual(target.stat().st_mtime_ns, original_mtime)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(fixture.state()["owners"][KEYS], [0, 0])
        self.assert_clean_and_preserved(fixture)

    def test_protected_key_metadata_stays_untouched_during_non_ssh_file_rollback(self):
        fixture = self.fixture(matching=True, reload_failures=[1])
        target = fixture.root / KEYS
        target.write_text("# retain native formatting\n" + authorized_key(1, comment="original native key"))
        target.chmod(0o644)
        state = fixture.state()
        state["owners"][KEYS] = [0, 0]
        fixture.state_path.write_text(json.dumps(state))
        (fixture.root / "etc/chrony/conf.d/ip-fallback.conf").write_text("retain previous managed time source\n")
        before = fixture.snapshot()
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assert_unchanged_except_chrony_context(
            fixture, before, (b"retain previous managed time source\n",)
        )
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assert_clean_and_preserved(fixture)

    def test_protected_ssh_targets_are_verify_only_even_with_disruptive_activation(self):
        for asset in (*PROTECTED_SSH, "home/pi/.ssh"):
            kinds = ("mode", "owner") if asset == "home/pi/.ssh" else ("content", "mode", "owner", "missing", "directory")
            for kind in kinds:
                with self.subTest(asset=asset, kind=kind):
                    fixture = self.fixture(matching=True)
                    target = fixture.root / asset
                    if kind == "content":
                        content = authorized_key(3).encode() if asset == KEYS else fixture.sources[asset] + b"\n# retain native SSH configuration\n"
                        target.write_bytes(content)
                    elif kind == "mode":
                        target.chmod(0o755 if asset == "home/pi/.ssh" else 0o640)
                    elif kind == "owner":
                        state = fixture.state()
                        state["owners"][asset] = [1000, 1000] if asset == SSH_DROPIN else [0, 0]
                        fixture.state_path.write_text(json.dumps(state))
                    elif kind == "missing":
                        target.unlink()
                    else:
                        target.unlink()
                        target.mkdir()
                    before = fixture.snapshot()
                    result = fixture.check()
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(fixture.snapshot(), before)
                    result = fixture.run("--platform", "cloud", "--activate", "cloud-1")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(fixture.snapshot(), before)
                    self.assertEqual(fixture.state()["moves"], 0)
                    self.assertEqual(fixture.state()["reloads"], 0)
                    self.assert_clean_and_preserved(fixture)

    def test_invalid_desired_keys_cannot_replace_working_authorization(self):
        for desired in (
            "", "# private-marker comment only\n", "ssh-ed25519 invalid private-marker\n",
            'command="unterminated private-marker\n',
            authorized_key().replace("ssh-ed25519 ", "ssh-rsa ", 1),
        ):
            with self.subTest(desired=desired):
                fixture = self.fixture(matching=True)
                fixture.desired_keys(desired)
                before = fixture.snapshot()
                result = fixture.apply()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(fixture.snapshot(), before)
                self.assertEqual(fixture.state()["moves"], 0)
                self.assertNotIn("private-marker", result.stdout + result.stderr)
                self.assert_clean_and_preserved(fixture)

    def test_check_matches_without_any_managed_state_changes_or_stale_helper_execution(self):
        fixture = self.fixture(matching=True)
        before = fixture.snapshot()
        result = fixture.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.snapshot(), before)
        self.assertIn(["runtime", True, "cloud", "server", True, False], fixture.events())
        self.assertEqual(fixture.state()["timer_enabled"], "enabled")
        self.assertFalse([event for event in fixture.events() if event[0] == "systemctl" and event[1] in ("stop", "start", "daemon-reload")])
        self.assert_clean_and_preserved(fixture)

    def test_check_detects_required_timer_drift_without_enabling_it(self):
        fixture = self.fixture(matching=True, timer_enabled="disabled")
        before = fixture.snapshot()
        result = fixture.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual(fixture.state()["timer_enabled"], "disabled")
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assert_clean_and_preserved(fixture)

    def test_apply_delegates_required_timer_enablement_to_committed_runtime_policy(self):
        fixture = self.fixture(matching=True, timer_enabled="disabled", timer_state="inactive")
        result = fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.state()["timer_enabled"], "enabled")
        self.assertEqual(fixture.state()["timer_state"], "active")
        self.assertEqual(fixture.state()["timer_starts"], 0)
        self.assertEqual(fixture.state()["moves"], 0)
        self.assert_clean_and_preserved(fixture)

    def test_check_reports_content_mode_owner_key_and_runtime_drift_without_mutation(self):
        for kind in ("content", "mode", "owner", "keys", "runtime", "pending", "ssh-directory"):
            with self.subTest(kind=kind):
                fixture = self.fixture(matching=True, runtime_check_status=2 if kind == "pending" else 1 if kind == "runtime" else 0)
                asset = COMMON[0]
                if kind == "content":
                    (fixture.root / asset).write_text("changed content\n")
                elif kind == "mode":
                    (fixture.root / asset).chmod(0o700)
                elif kind == "owner":
                    state = fixture.state()
                    state["owners"][asset] = [1000, 1000]
                    fixture.state_path.write_text(json.dumps(state))
                elif kind == "keys":
                    (fixture.root / KEYS).write_text("ssh-ed25519 different-key private-marker\n")
                elif kind == "ssh-directory":
                    (fixture.root / "home/pi/.ssh").chmod(0o755)
                before = fixture.snapshot()
                result = fixture.check()
                expected_status = 2 if kind in ("keys", "ssh-directory", "pending") else 1
                self.assertEqual(result.returncode, expected_status)
                self.assertEqual(fixture.snapshot(), before)
                self.assertNotIn("private-marker", result.stdout + result.stderr)
                self.assertEqual(fixture.state()["runtime_runs"], 0)
                self.assert_clean_and_preserved(fixture)

    def test_required_platform_and_all_hostnames_fail_before_transport(self):
        for arguments in (
            ("cloud-1",), ("--check", "cloud-1"),
            ("--platform", "other", "cloud-1"),
            ("--platform", "cloud", "cloud-1", "bad;name"),
            ("--health-only", "--check", "cloud-1"),
            ("--health-only", "--platform", "cloud", "cloud-1"),
            ("--health-only", "--activate", "cloud-1"),
            ("--platform", "cloud", "--check", "--activate", "cloud-1"),
        ):
            with self.subTest(arguments=arguments):
                fixture = self.fixture()
                before = fixture.snapshot()
                result = fixture.run(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.events(), [])
                self.assertEqual(fixture.snapshot(), before)

    def test_os_hostname_account_and_role_must_match_before_managed_changes(self):
        for config in (
            {"os_id": "debian"}, {"hostname": "wrong-host"},
            {"role": "ambiguous"}, {"role": "none"},
            {"pi_uid": 0}, {"pi_home": "/unexpected-home"},
        ):
            with self.subTest(config=config):
                fixture = self.fixture(**config)
                before = fixture.snapshot()
                result = fixture.apply()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.snapshot(), before)
                self.assertEqual(fixture.state()["runtime_runs"], 0)
                self.assertEqual(fixture.state()["reloads"], 0)
                self.assert_clean_and_preserved(fixture)

    def test_agent_role_is_preserved_in_apply_and_check(self):
        fixture = self.fixture(role="agent")
        result = fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["runtime", False, "cloud", "agent", False, True], fixture.events())
        result = fixture.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_clean_and_preserved(fixture)

    def test_runtime_pending_and_failure_keep_committed_files_and_return_failure(self):
        for status in (1, 2):
            with self.subTest(status=status):
                fixture = self.fixture(runtime_status=status)
                result = fixture.apply()
                self.assertEqual(result.returncode, status, result.stderr)
                self.assert_desired(fixture)
                self.assertEqual(fixture.state()["runtime_runs"], 1)
                self.assertEqual(fixture.state()["timer_state"], "active")
                self.assertEqual(len([event for event in fixture.events() if event[0] == "replace"]), len(fixture.assets) - len(PROTECTED_SSH))
                self.assert_clean_and_preserved(fixture)

    def test_fleet_keeps_processing_and_reports_partial_failure(self):
        fixture = self.fixture(runtime_status_by_host={"cloud-1": 1, "cloud-2": 0})
        result = fixture.apply("cloud-1", "cloud-2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([event for event in fixture.events() if event[0] == "ssh"], [["ssh", "pi@cloud-1"], ["ssh", "pi@cloud-2"]])
        self.assertEqual(fixture.state()["runtime_runs"], 2)
        self.assert_desired(fixture)
        self.assert_clean_and_preserved(fixture)

    def test_file_failures_restore_bytes_metadata_mtime_and_timer_without_runtime_apply(self):
        for config in (
            {"candidate_install_failure": True}, {"move_failures": [2]},
            {"move_failures": [12]}, {"reload_failures": [1]},
            {"timer_stop_failure": True}, {"timer_start_failures": [1]},
            {"service_query_timeout": True},
        ):
            with self.subTest(config=config):
                fixture = self.fixture(**config)
                result = fixture.apply()
                self.assertNotEqual(result.returncode, 0)
                self.assert_originals(fixture)
                self.assertEqual(fixture.state()["runtime_runs"], 0)
                self.assertEqual(fixture.state()["timer_state"], "active")
                self.assert_clean_and_preserved(fixture)

    def test_failed_timer_restore_waits_for_newly_running_health_before_rollback(self):
        fixture = self.fixture(
            timer_start_failures=[1],
            timer_start_failure_runs_service=True,
            service_states=["inactive", "active", "inactive"],
        )
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assert_originals(fixture)
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assertEqual(fixture.state()["service_reads"], 3)
        self.assertEqual(fixture.state()["timer_state"], "active")
        self.assert_clean_and_preserved(fixture)

    def test_running_health_service_finishes_naturally_before_any_replacement(self):
        fixture = self.fixture(service_states=["activating", "active", "deactivating", "inactive"])
        result = fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = fixture.events()
        finished = events.index(["service-state", "inactive"])
        replacements = [index for index, event in enumerate(events) if event[0] == "replace"]
        self.assertLess(finished, min(replacements))
        self.assertEqual(len([event for event in events if event[0] == "wait"]), 3)
        self.assert_desired(fixture)
        self.assert_clean_and_preserved(fixture)

    def test_health_wait_timeout_does_not_replace_files_or_force_service_stop(self):
        fixture = self.fixture(service_states=["active"])
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fixture.state()["service_reads"], 65)
        self.assertEqual(fixture.state()["moves"], 0)
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assertEqual(fixture.state()["timer_state"], "active")
        self.assert_originals(fixture)
        self.assert_clean_and_preserved(fixture)

    def test_missing_asset_parents_are_created_without_changing_existing_directories(self):
        absent = tuple(asset for asset in (*COMMON, *PHYSICAL) if asset not in PROTECTED_SSH)
        fixture = self.fixture(platform="physical", absent=absent)
        parent = fixture.root / "usr/local/share/nodes"
        parent.rmdir()
        parent.parent.rmdir()
        protected = fixture.root / "etc/systemd"
        protected.chmod(0o711)
        result = fixture.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_desired(fixture)
        self.assertEqual(stat.S_IMODE(protected.stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
        self.assert_clean_and_preserved(fixture)

    def test_rollback_removes_new_files_and_empty_created_parents_only(self):
        fixture = self.fixture(absent=tuple(asset for asset in COMMON if asset not in PROTECTED_SSH), reload_failures=[1])
        parent = fixture.root / "usr/local/share/nodes"
        parent.rmdir()
        parent.parent.rmdir()
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assert_originals(fixture)
        self.assertFalse(parent.exists())
        self.assertFalse(parent.parent.exists())
        self.assert_clean_and_preserved(fixture)

    def test_local_symlink_sources_and_directories_are_rejected_before_ssh(self):
        for kind in ("file", "directory", "keys"):
            with self.subTest(kind=kind):
                fixture = self.fixture()
                relative = Path("config/authorized_keys") if kind == "keys" else Path("rootfs/common") / COMMON[0]
                path = fixture.repository / relative
                if kind == "directory":
                    path = path.parent
                    replacement = fixture.base / "source-bin"
                    path.rename(replacement)
                    path.symlink_to(replacement, target_is_directory=True)
                else:
                    replacement = fixture.base / "source-file"
                    path.rename(replacement)
                    path.symlink_to(replacement)
                before = fixture.snapshot()
                result = fixture.apply()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(fixture.events(), [])
                self.assertEqual(fixture.snapshot(), before)

    def test_remote_symlink_destination_fails_before_any_managed_changes(self):
        fixture = self.fixture()
        target = fixture.root / COMMON[0]
        outside = fixture.base / "outside"
        outside.write_text("retain outside sandbox target\n")
        target.unlink()
        target.symlink_to(outside)
        result = fixture.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_text(), "retain outside sandbox target\n")
        self.assertTrue(target.is_symlink())
        self.assertEqual(fixture.state()["moves"], 0)
        self.assertEqual(fixture.state()["runtime_runs"], 0)
        self.assertFalse([event for event in fixture.events() if event[0] == "rejected"])
        self.assertEqual(list((fixture.root / "tmp").iterdir()), [])
        self.assertEqual(list((fixture.root / "run").iterdir()), [])

    def test_invalid_script_policy_and_units_never_reach_runtime_apply(self):
        for kind in ("script", "python", "json", "unit", "dropin", "link", "upload"):
            with self.subTest(kind=kind):
                fixture = self.fixture(
                    platform="physical" if kind == "link" else "cloud",
                    corrupt_uploaded_script=kind == "script", upload_failure=kind == "upload",
                )
                if kind == "python":
                    (fixture.repository / "rootfs/common/usr/local/bin/node-configure.py").write_text("if then\n")
                elif kind == "json":
                    (fixture.repository / "rootfs/common/usr/local/share/nodes/node-policy.json").write_text("{broken\n")
                elif kind == "unit":
                    (fixture.repository / "rootfs/common/etc/systemd/system/node-health.timer").write_text("[Timer\nbroken\n")
                elif kind == "dropin":
                    (fixture.repository / "rootfs/common/etc/systemd/system/k3s.service.d/restart-after-tailscale.conf").write_text("[Unit\nbroken\n")
                elif kind == "link":
                    (fixture.repository / "rootfs/physical/etc/systemd/network/10-wlan0.link").write_text("[Link\nbroken\n")
                result = fixture.apply()
                self.assertNotEqual(result.returncode, 0)
                self.assert_originals(fixture)
                self.assertEqual(fixture.state()["runtime_runs"], 0)
                self.assert_clean_and_preserved(fixture)


if __name__ == "__main__":
    unittest.main()
