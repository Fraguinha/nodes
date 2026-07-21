#!/usr/bin/env python3
import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile

RUNTIME_DIR = os.environ.get("NODE_HEALTH_RUN_DIR") or "/run/node-health"
UPTIME_FILE = os.environ.get("NODE_HEALTH_UPTIME_FILE") or "/proc/uptime"
CLUSTER_PEER_CACHE = os.environ.get("NODE_HEALTH_PEER_CACHE") or "/var/lib/node-health/cluster-peer-ips"
WIFI_HELPER = os.environ.get("NODE_HEALTH_WIFI_HELPER") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "node-wifi-recovery.py")
RECOVERY_STATE = RUNTIME_DIR + "/tailscale-recovery"
HEALTH_STATE = RUNTIME_DIR + "/tailscale-health"
JOURNAL_CURSOR = RUNTIME_DIR + "/kernel-journal-cursor"
LOCK_DESCRIPTOR = 8
TAILSCALE_FAILURE_DELAY = 300
TAILSCALE_MAX_PROBES = 5
K3S_FLANNEL_STARTUP_GRACE = 180
UPTIME = re.compile(r"[0-9]{1,12}(\.[0-9]+)?")
RECOVERY_RECORD = re.compile(
    r"v2 ([0-9]{1,12}) ([0-9]{1,12}) ([0-9]{1,12}) ([01]) ([0-9]{1,12}) ([0-3]) ([01]) ([01]) ([01])")
PEER = re.compile(r"[0-9a-fA-F:.]+")
ACTIVE_ENTER = re.compile(r"[0-9]{1,18}")
COUNT = re.compile(r"[0-9]{1,3}")
KERNEL_FATAL = (
    re.compile(r"brcmfmac", re.IGNORECASE),
    re.compile(r"(brcmf_sdio[^: ]*|brcmf_sdiod[^: ]*|backplane)", re.IGNORECASE),
    re.compile(r"(^|[^\w])(failed|fail|error|timeout|halt(ed|ing)?)([^\w]|$)|timed out", re.IGNORECASE),
)
PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL


def run(arguments, stdout=None, stderr=None, pass_fds=()):
    try:
        result = subprocess.run(arguments, stdout=stdout, stderr=stderr, pass_fds=pass_fds)
    except OSError as error:
        return 126 if isinstance(error, PermissionError) else 127, ""
    output = result.stdout.decode(errors="surrogateescape") if stdout == PIPE else ""
    return 128 - result.returncode if result.returncode < 0 else result.returncode, output


def bounded(seconds, *command):
    return ["timeout", "--kill-after=1s", seconds, *command]


def log(message):
    run(bounded("1s", "logger", "-t", "node-health", message))


def action(name, result, bound, *command):
    code = run(bounded(f"{bound}s", *command), DEVNULL, DEVNULL)[0]
    if code:
        result = "failed"
    log(f"event=recovery_action layer=tailscale action={name} result={result} exit_code={code}")
    return code


def monotonic_now():
    try:
        with open(UPTIME_FILE, "rb") as stream:
            line = stream.readline().decode(errors="surrogateescape")
    except OSError:
        return None
    if not line.endswith("\n"):
        return None
    uptime = re.split(r"[ \t]+", line[:-1].strip(" \t"), maxsplit=1)[0]
    if not UPTIME.fullmatch(uptime):
        return None
    return int(uptime.split(".")[0])


def read_record(path):
    try:
        with open(path, "rb") as stream:
            text = stream.read().decode(errors="surrogateescape")
    except OSError:
        return None
    if text.count("\n") != 1 or not text.endswith("\n"):
        return None
    return text[:-1]


def readable(path):
    return os.access(path, os.R_OK)


def install_dir(path, mode):
    os.makedirs(path, exist_ok=True)
    os.chmod(path, mode)


def replace(path, content, mode=None):
    directory, name = os.path.split(path)
    try:
        descriptor, temp = tempfile.mkstemp(prefix=name + ".", dir=directory or ".")
    except OSError:
        return False
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content.encode(errors="surrogateescape"))
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
        replaced = True
    except OSError:
        pass
    finally:
        if not replaced:
            with contextlib.suppress(OSError):
                os.unlink(temp)
    return replaced


def remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def json_values(text):
    decoder = json.JSONDecoder()
    values, index = [], 0
    while True:
        while index < len(text) and text[index] in " \t\n\r":
            index += 1
        if index == len(text):
            return values, True
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            return values, False
        values.append(value)


def members(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    raise ValueError("cannot iterate")


def field(value, name):
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    raise ValueError("cannot index")


def tsv(values):
    return "\t".join(value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
                     for value in values)


def lines(text):
    return text.split("\n")[:-1]


def kernel_summary(records):
    values, complete = json_values(records)
    if not complete:
        return None
    try:
        selected = [record for record in values if isinstance(field(record, "__CURSOR"), str)]
    except ValueError:
        return None
    if not selected:
        return None
    count = 0
    for record in selected:
        message = record.get("MESSAGE")
        if isinstance(message, str) and all(pattern.search(message) for pattern in KERNEL_FATAL):
            count += 1
    return selected[-1]["__CURSOR"], count


def backend_state(text):
    output = []
    for value in json_values(text)[0]:
        state = value.get("BackendState") if isinstance(value, dict) else None
        if state is None or state is False:
            state = "unknown"
        output.append(state if isinstance(state, str) else json.dumps(state, separators=(",", ":")))
    return "\n".join(output).rstrip("\n")


def flannel_peers(text):
    output = []
    for value in json_values(text)[0]:
        peers = set()
        try:
            for entry in members(value):
                peer = entry.get("dst") if isinstance(entry, dict) else None
                if peer is None or peer is False:
                    continue
                if not isinstance(peer, str):
                    raise ValueError("cannot match")
                if ":" not in peer:
                    peers.add(peer)
        except ValueError:
            continue
        output.extend(sorted(peers))
    return "\n".join(output).rstrip("\n")


def persist_cluster_peer_ips(peers):
    content = peers + "\n"
    try:
        install_dir(CLUSTER_PEER_CACHE.rsplit("/", 1)[0], 0o755)
    except OSError:
        return False
    if readable(CLUSTER_PEER_CACHE):
        with contextlib.suppress(OSError), open(CLUSTER_PEER_CACHE, "rb") as stream:
            if stream.read() == content.encode(errors="surrogateescape"):
                return True
    return replace(CLUSTER_PEER_CACHE, content, 0o644)


def cluster_peer_ips():
    flannel_fdb = run(bounded("1s", "bridge", "-json", "fdb", "show", "dev", "flannel.1"), PIPE, DEVNULL)[1]
    live_peers = flannel_peers(flannel_fdb.rstrip("\n"))
    if live_peers:
        persist_cluster_peer_ips(live_peers)
        return live_peers + "\n"
    if readable(CLUSTER_PEER_CACHE):
        with contextlib.suppress(OSError), open(CLUSTER_PEER_CACHE, "rb") as stream:
            return stream.read().decode(errors="surrogateescape")
    return ""


class Health:
    def __init__(self, now):
        self.now = now
        self.pending = 0
        self.started = 0
        self.due = 0
        self.observed = 0
        self.attempted = 0
        self.success_started = 0
        self.successes = 0
        self.startup_attempted = 0
        self.deferred = 0
        self.warned = 0
        self.probe_state = "unknown"
        self.probe = "tsmp"
        self.target = "-"
        self.reason = "no_peer_candidates"
        self.startup_action = ""
        self.backend = "unknown"
        self.k3s_unit = "k3s"
        self.underlay = 0

    def diagnose_kernel(self):
        previous = (read_record(JOURNAL_CURSOR) if readable(JOURNAL_CURSOR) else None) or ""
        arguments = ["--dmesg", "--boot", "--no-pager", "--output=json"]
        if previous and len(previous) <= 4096:
            arguments += ["--after-cursor=" + previous, "--lines=+100"]
        else:
            arguments.append("--lines=100")
        code, records = run(bounded("2s", "journalctl", *arguments), PIPE, DEVNULL)
        records = records.rstrip("\n")
        if code or not records:
            return
        summary = kernel_summary(records)
        if summary is None:
            return
        cursor = tsv((summary[0],))
        count = str(summary[1])
        if not cursor or len(cursor) > 4096 or not COUNT.fullmatch(count):
            return
        if cursor == previous:
            return
        if not replace(JOURNAL_CURSOR, cursor + "\n"):
            return
        if summary[1] > 0:
            log("event=diagnostic_warning layer=wifi probe=kernel_journal target=- "
                f"reason=brcmfmac_sdio_fatal records={count}")

    def load_recovery(self):
        if not readable(RECOVERY_STATE):
            return
        record = read_record(RECOVERY_STATE)
        match = RECOVERY_RECORD.fullmatch(record) if record is not None else None
        if not match:
            return
        (self.started, self.due, self.observed, self.attempted, self.success_started, self.successes,
         self.startup_attempted, self.deferred, self.warned) = map(int, match.groups())
        if (self.started <= self.observed <= self.now and self.due >= self.started
                and self.success_started <= self.observed) and (
                (self.successes == 0 and self.success_started == 0)
                or (self.successes > 0 and self.success_started >= self.started)):
            self.pending = 1

    def save_recovery(self):
        return replace(RECOVERY_STATE, f"v2 {self.started} {self.due} {self.now} {self.attempted} "
                       f"{self.success_started} {self.successes} {self.startup_attempted} "
                       f"{self.deferred} {self.warned}\n")

    def transition(self, state, reason):
        previous = read_record(HEALTH_STATE) if readable(HEALTH_STATE) else None
        if previous == "v1 " + state:
            return True
        if not replace(HEALTH_STATE, f"v1 {state}\n"):
            return False
        elapsed = f" elapsed_seconds={self.now - self.started}" if reason == "sustained_recovery" else ""
        log(f"event=health_transition layer=tailscale state={state} probe={self.probe} "
            f"target={self.target} reason={reason}{elapsed}")
        return True

    def observe_tailscale(self):
        if self.probe_state == "healthy":
            if self.pending:
                if self.successes == 0:
                    self.success_started = self.now
                if self.successes < 3:
                    self.successes += 1
                if self.successes >= 3 and self.now - self.success_started >= 30:
                    if not remove(RECOVERY_STATE):
                        return False
                    self.pending = 0
                    self.transition("healthy", "sustained_recovery")
                else:
                    if not self.save_recovery():
                        return False
                    self.transition("unknown", "confirming_recovery")
            else:
                self.transition("healthy", self.reason)
        elif self.probe_state == "unhealthy":
            if not self.pending:
                self.pending = 1
                self.started = self.now
                self.due = self.now + TAILSCALE_FAILURE_DELAY
                self.attempted = 0
                self.startup_attempted = 0
                self.deferred = 0
                self.warned = 0
            self.success_started = 0
            self.successes = 0
            if not self.save_recovery():
                return False
            self.transition("unhealthy", self.reason)
        elif self.probe_state == "unknown":
            if self.pending:
                self.success_started = 0
                self.successes = 0
                if not self.save_recovery():
                    return False
            self.transition("unknown", self.reason)
        if self.pending and not self.warned and self.now >= self.due and self.probe_state != "healthy":
            self.warned = 1
            if not self.save_recovery():
                return False
            log(f"event=diagnostic_warning layer=tailscale probe={self.probe} target={self.target} "
                f"reason={self.reason} elapsed_seconds={self.now - self.started}")
        return True

    def probe_tailscale(self):
        if run(bounded("1s", "systemctl", "is-active", "--quiet", "tailscaled.service"))[0]:
            self.probe_state = "unhealthy"
            self.probe = "service"
            self.reason = "service_inactive"
            self.startup_action = "start_tailscaled"
            return
        output = run(bounded("2s", "tailscale", "status", "--json"), PIPE, DEVNULL)[1]
        self.backend = backend_state(output.rstrip("\n"))
        if self.backend == "Running":
            attempts = 0
            for peer in lines(cluster_peer_ips()):
                if not peer or not PEER.fullmatch(peer):
                    continue
                attempts += 1
                self.target = peer
                code, output = run(bounded("4s", "tailscale", "ping", "--tsmp", "--c", "1", "--timeout", "2s", peer),
                                   PIPE, DEVNULL)
                output = output.rstrip("\n")
                if code == 0 and (output.startswith("pong from ") or "\npong from " in output):
                    self.probe_state = "healthy"
                    self.reason = "peer_pong"
                    return
                if attempts >= TAILSCALE_MAX_PROBES:
                    break
            if attempts > 0:
                self.probe_state = "unhealthy"
                self.reason = "peer_unreachable"
        elif self.backend == "Stopped":
            self.probe_state = "unhealthy"
            self.probe = "backend"
            self.reason = "backend_stopped"
            self.startup_action = "tailscale_up"
        elif self.backend == "NeedsLogin":
            self.probe = "backend"
            self.reason = "authentication_required"
        else:
            self.probe_state = "unhealthy"
            self.probe = "backend"
            self.reason = "backend_unavailable"

    def recover_tailscale(self):
        if self.probe_state != "unhealthy":
            return True
        if self.underlay not in (0, 3):
            if self.now >= self.due and not self.attempted and not self.deferred:
                self.deferred = 1
                if not self.save_recovery():
                    return False
                log("event=recovery_action layer=tailscale action=restart_tailscaled result=deferred "
                    "reason=underlay_unusable")
            return True
        if self.startup_action and not self.startup_attempted:
            self.startup_attempted = 1
            self.deferred = 0
            if not self.save_recovery():
                return False
            if self.startup_action == "start_tailscaled":
                action("start_tailscaled", "requested", 2, "systemctl", "--no-block", "start", "tailscaled.service")
            elif self.startup_action == "tailscale_up":
                action("tailscale_up", "succeeded", 20, "tailscale", "up")
            return True
        if not (self.now >= self.due and not self.attempted):
            return True
        self.attempted = 1
        self.deferred = 0
        if not self.save_recovery():
            return False
        action("restart_tailscaled", "requested", 2, "systemctl", "--no-block", "restart", "tailscaled.service")
        return True

    def k3s_active_seconds(self):
        active_enter = run(bounded("1s", "systemctl", "show", self.k3s_unit,
                                   "--property=ActiveEnterTimestampMonotonic", "--value"), PIPE, DEVNULL)[1]
        active_enter = active_enter.rstrip("\n")
        if not ACTIVE_ENTER.fullmatch(active_enter):
            return None
        active_enter = int(active_enter)
        if not (active_enter > 0 and self.now * 1000000 >= active_enter):
            return None
        return self.now - active_enter // 1000000

    def manage_k3s(self):
        if self.backend != "Running" or self.probe_state == "unhealthy":
            return
        if run(bounded("1s", "systemctl", "is-enabled", "k3s-agent"), DEVNULL, DEVNULL)[0] == 0:
            self.k3s_unit = "k3s-agent"
        state = run(bounded("1s", "systemctl", "is-active", self.k3s_unit), PIPE, DEVNULL)[1].rstrip("\n")
        if state == "active":
            if run(bounded("1s", "ip", "link", "show", "flannel.1"), DEVNULL, DEVNULL)[0]:
                active_seconds = self.k3s_active_seconds()
                if active_seconds is not None and active_seconds >= K3S_FLANNEL_STARTUP_GRACE:
                    action("restart_" + self.k3s_unit, "requested", 2,
                           "systemctl", "--no-block", "restart", self.k3s_unit)
        elif state not in ("activating", "deactivating", "reloading"):
            action("restart_" + self.k3s_unit, "requested", 2, "systemctl", "--no-block", "restart", self.k3s_unit)


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.umask(0o077)
    try:
        install_dir(RUNTIME_DIR, 0o700)
        descriptor = os.open(RUNTIME_DIR + "/lock", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    except OSError:
        return 1
    if descriptor != LOCK_DESCRIPTOR:
        os.dup2(descriptor, LOCK_DESCRIPTOR)
        os.close(descriptor)
    if run(["flock", "-n", str(LOCK_DESCRIPTOR)], pass_fds=(LOCK_DESCRIPTOR,))[0]:
        return 0
    now = monotonic_now()
    if now is None:
        return 1
    health = Health(now)
    health.diagnose_kernel()
    health.load_recovery()
    health.probe_tailscale()
    if not health.observe_tailscale():
        return 1
    if health.probe_state == "healthy":
        arguments = [WIFI_HELPER, "--tailnet-peer", health.target]
    else:
        arguments = [WIFI_HELPER]
    health.underlay = run(arguments, pass_fds=(LOCK_DESCRIPTOR,))[0]
    if not health.recover_tailscale():
        return 1
    health.manage_k3s()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
