#!/usr/bin/env python3
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile

RUNTIME_DIR = os.environ.get("NODE_WIFI_RUNTIME_DIR") or "/run/node-health/wifi-recovery"
PERSIST_DIR = os.environ.get("NODE_WIFI_PERSIST_DIR") or "/var/lib/node-health/wifi-recovery"
SYSFS_DIR = os.environ.get("NODE_WIFI_SYSFS_DIR") or "/sys/class/net"
UPTIME_FILE = os.environ.get("NODE_WIFI_UPTIME_FILE") or "/proc/uptime"
PENDING_STATE = RUNTIME_DIR + "/recovery"
HEALTH_STATE = RUNTIME_DIR + "/health"
LINK_UP_STATE = RUNTIME_DIR + "/link-needs-up"
REBOOT_BUDGET = PERSIST_DIR + "/reboot-budget"
WIFI_UNIT = "netplan-wpa-wlan0.service"
LOCK_DESCRIPTOR = 9
BUDGET_EPOCH = 1704067200
UPTIME = re.compile(r"[0-9]{1,12}(\.[0-9]+)?")
PENDING_RECORD = re.compile(r"v2 ([0-3]) ([0-9]{1,12}) ([0-9]{1,12}) ([0-9]{1,12}) ([0-9]{1,12}) ([0-3])")
BUDGET_RECORD = re.compile(r"v1 ([0-9]{1,12}) ([0-9]{1,12})")
WALL_CLOCK = re.compile(r"[0-9]{1,12}")
PEER = re.compile(r"[0-9a-fA-F:.]+")
GATEWAY = re.compile(r"([0-9]{1,3}\.){3}[0-9]{1,3}")
NON_GLOBAL = re.compile(r"(169\.254\.|127\.)")
PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL
WAITING = False
TERMINATING = False


def terminate(signum, frame):
    global TERMINATING
    if WAITING:
        TERMINATING = True
    else:
        raise SystemExit(1)


def run(arguments, stdout=None, stderr=None, pass_fds=()):
    global WAITING, TERMINATING
    WAITING = True
    try:
        result = subprocess.run(arguments, stdout=stdout, stderr=stderr, pass_fds=pass_fds)
    except OSError as error:
        result = error
    finally:
        WAITING = False
    if TERMINATING:
        TERMINATING = False
        raise SystemExit(1)
    if isinstance(result, OSError):
        return 126 if isinstance(result, PermissionError) else 127, ""
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
    log(f"event=recovery_action layer=wifi action={name} result={result} exit_code={code}")
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


def remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def replace(path, content, synced_directory=None):
    directory, name = os.path.split(path)
    try:
        descriptor, temp = tempfile.mkstemp(prefix=name + ".", dir=directory or ".")
    except OSError:
        return False
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content.encode())
        if synced_directory is not None and run(bounded("2s", "sync", "-f", temp))[0]:
            return False
        os.replace(temp, path)
        replaced = True
    except OSError:
        return False
    finally:
        if not replaced:
            with contextlib.suppress(OSError):
                os.unlink(temp)
    return synced_directory is None or run(bounded("2s", "sync", "-f", synced_directory))[0] == 0


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


def routes_valid(values, complete):
    return complete and bool(values) and isinstance(values[-1], list) and all(
        isinstance(route, dict) for route in values[-1])


def default_routes(values):
    routes = []
    for value in values:
        for route in members(value):
            device, gateway = field(route, "dev"), field(route, "gateway")
            if isinstance(device, str) and isinstance(gateway, str):
                fields = tsv((device, gateway)).strip("\t").split("\t", 1)
                routes.append((fields[0], fields[1] if len(fields) > 1 else ""))
    return routes


def has_global_address(value):
    for interface in members(value):
        addresses = field(interface, "addr_info")
        if not isinstance(addresses, (list, dict)):
            continue
        for address in members(addresses):
            if field(address, "family") != "inet" or field(address, "scope") != "global":
                continue
            local = field(address, "local")
            if not isinstance(local, str):
                raise ValueError("cannot match")
            if not NON_GLOBAL.match(local):
                return True
    return False


def global_address(text):
    values, complete = json_values(text)
    if not complete or not values:
        return False
    try:
        return [has_global_address(value) for value in values][-1]
    except ValueError:
        return False


class WifiRecovery:
    def __init__(self, tailnet_peer):
        self.tailnet_peer = tailnet_peer
        self.now = 0
        self.link_needs_up = 0
        self.pending = 0
        self.stage = 0
        self.started = 0
        self.due = 0
        self.observed = 0
        self.success_started = 0
        self.successes = 0
        self.probe = "none"
        self.target = "-"
        self.reason = "underlay_unreachable"

    def restore_link(self):
        if not self.link_needs_up:
            return True
        if action("link_up", "succeeded", 2, "ip", "link", "set", "dev", "wlan0", "up"):
            return False
        if not remove(LINK_UP_STATE):
            return False
        self.link_needs_up = 0
        return True

    def load_pending(self):
        if not readable(PENDING_STATE):
            return
        record = read_record(PENDING_STATE)
        match = PENDING_RECORD.fullmatch(record) if record is not None else None
        if not match:
            return
        (self.stage, self.started, self.due, self.observed, self.success_started,
         self.successes) = map(int, match.groups())
        if (self.started <= self.observed <= self.now and self.due >= self.started
                and self.success_started <= self.observed) and (
                (self.successes == 0 and self.success_started == 0)
                or (self.successes > 0 and self.success_started >= self.started)):
            self.pending = 1

    def save_pending(self):
        return replace(PENDING_STATE, f"v2 {self.stage} {self.started} {self.due} {self.now} "
                       f"{self.success_started} {self.successes}\n")

    def transition(self, state, reason):
        previous = read_record(HEALTH_STATE) if readable(HEALTH_STATE) else None
        if previous == "v1 " + state:
            return True
        if not replace(HEALTH_STATE, f"v1 {state}\n"):
            return False
        log(f"event=health_transition layer=wifi state={state} probe={self.probe} target={self.target} "
            f"reason={reason}")
        return True

    def observe_positive(self):
        if self.pending:
            if self.successes == 0:
                self.success_started = self.now
            if self.successes < 3:
                self.successes += 1
            if self.successes >= 3 and self.now - self.success_started >= 30:
                if not remove(PENDING_STATE):
                    return 1
                self.pending = 0
                return 0 if self.transition("healthy", "sustained_recovery") else 1
            if not self.save_pending():
                return 1
            return 0 if self.transition("unknown", "confirming_recovery") else 1
        return 0 if self.transition("healthy", self.reason) else 1

    def observe_unknown(self, reason):
        if self.pending:
            self.success_started = 0
            self.successes = 0
            if not self.save_pending():
                return False
        return self.transition("unknown", reason)

    def interface_works(self, iface, gateway):
        self.probe = "default_route"
        self.target = "-"
        self.reason = "default_route_unusable"
        if not GATEWAY.fullmatch(gateway):
            return False
        self.probe = "address"
        self.reason = "no_global_address"
        code, addresses = run(bounded("1s", "ip", "-j", "-4", "address", "show", "dev", iface, "scope", "global"),
                              PIPE, DEVNULL)
        if code or not global_address(addresses.rstrip("\n")):
            return False
        if self.tailnet_peer:
            self.probe = "tsmp"
            self.target = self.tailnet_peer
            self.reason = "peer_pong"
            return True
        self.target = gateway
        if run(bounded("2s", "ping", "-n", "-I", iface, "-c1", "-W1", gateway), DEVNULL, DEVNULL)[0] == 0:
            self.probe = "gateway_icmp"
            self.reason = "gateway_reply"
            return True
        self.target = "1.1.1.1"
        if run(bounded("2s", "curl", "--silent", "--output", "/dev/null", "--noproxy", "*", "--interface", iface,
                       "--connect-timeout", "1", "--max-time", "2", "http://1.1.1.1/"), DEVNULL, DEVNULL)[0] == 0:
            self.probe = "external_http"
            self.reason = "http_reply"
            return True
        self.probe = "ip_traffic"
        self.reason = "traffic_unreachable"
        return False

    def persist_budget(self, last_seen, last_reboot):
        try:
            install_dir(PERSIST_DIR, 0o700)
        except OSError:
            return False
        return replace(REBOOT_BUDGET, f"v1 {last_seen} {last_reboot}\n", PERSIST_DIR)

    def reserve_reboot(self):
        if run(bounded("2s", "chronyc", "waitsync", "1", "0.1", "0.0", "1"), DEVNULL, DEVNULL)[0]:
            self.reason = "clock_unsynchronized"
            return False
        self.reason = "clock_unavailable"
        code, now = run(bounded("1s", "date", "+%s"), PIPE)
        if code:
            return False
        now = now.rstrip("\n")
        if not WALL_CLOCK.fullmatch(now) or int(now) < BUDGET_EPOCH:
            self.reason = "clock_invalid"
            return False
        now = int(now)
        last_seen = last_reboot = 0
        if os.path.lexists(REBOOT_BUDGET):
            if not os.path.isfile(REBOOT_BUDGET) or os.path.islink(REBOOT_BUDGET) or not readable(REBOOT_BUDGET):
                self.reason = "budget_unreadable"
                return False
            record = read_record(REBOOT_BUDGET)
            match = BUDGET_RECORD.fullmatch(record) if record is not None else None
            if not match:
                self.reason = "budget_corrupt"
                return False
            last_seen, last_reboot = map(int, match.groups())
            if (last_seen < BUDGET_EPOCH or last_reboot > last_seen
                    or (last_reboot != 0 and last_reboot < BUDGET_EPOCH)):
                self.reason = "budget_corrupt"
                return False
            if now < last_seen:
                self.reason = "clock_rollback"
                return False
        if last_reboot > 0 and now - last_reboot < 86400:
            if not self.persist_budget(now, last_reboot):
                self.reason = "budget_persist_failed"
                return False
            self.reason = "budget_spent"
            return False
        if not self.persist_budget(now, now):
            self.reason = "budget_persist_failed"
            return False
        return True

    def recover_wifi(self):
        if not self.pending:
            self.pending = 1
            self.stage = 0
            self.started = self.now
            self.due = self.now + 300
        self.success_started = 0
        self.successes = 0
        if not self.save_pending() or not self.transition("unhealthy", self.reason):
            return 1
        if not (self.stage < 3 and self.now >= self.due):
            return 1
        if self.stage == 0:
            self.stage = 1
            self.due = self.now + 120
            if not self.save_pending():
                return 1
            action("restart_supplicant", "succeeded", 4, "systemctl", "restart", WIFI_UNIT)
            action("reconfigure", "requested", 2, "networkctl", "reconfigure", "wlan0")
            action("renew", "requested", 2, "networkctl", "renew", "wlan0")
        elif self.stage == 1:
            self.stage = 2
            self.due = self.now + 180
            if not self.save_pending():
                return 1
            if os.path.isdir(SYSFS_DIR + "/wlan0"):
                try:
                    with open(LINK_UP_STATE, "wb"):
                        pass
                except OSError:
                    return 1
                self.link_needs_up = 1
                action("link_down", "succeeded", 2, "ip", "link", "set", "dev", "wlan0", "down")
                if not self.restore_link():
                    return 1
            action("reconfigure", "requested", 2, "networkctl", "reconfigure", "wlan0")
            action("renew", "requested", 2, "networkctl", "renew", "wlan0")
        elif self.stage == 2:
            self.due = self.now + 300
            if not self.save_pending():
                return 1
            if self.reserve_reboot():
                self.stage = 3
                if not self.save_pending():
                    return 1
                action("reboot", "requested", 2, "systemctl", "--no-block", "reboot")
            else:
                log(f"event=recovery_action layer=wifi action=reboot result=deferred reason={self.reason}")
        return 1

    def main(self):
        os.umask(0o077)
        try:
            install_dir(RUNTIME_DIR, 0o700)
        except OSError:
            return 2
        try:
            descriptor = os.open(RUNTIME_DIR + "/lock", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        except OSError:
            return 1
        if descriptor != LOCK_DESCRIPTOR:
            os.dup2(descriptor, LOCK_DESCRIPTOR)
            os.close(descriptor)
        if run(["flock", "-n", str(LOCK_DESCRIPTOR)], pass_fds=(LOCK_DESCRIPTOR,))[0]:
            return 2
        now = monotonic_now()
        if now is None:
            return 2
        self.now = now
        self.load_pending()
        if os.path.lexists(LINK_UP_STATE):
            self.link_needs_up = 1
            self.restore_link()
            if not self.observe_unknown("link_restore_pending"):
                return 2
            return 1
        unit_state = run(bounded("1s", "systemctl", "show", WIFI_UNIT, "--property=LoadState", "--value"),
                         PIPE, DEVNULL)[1].rstrip("\n")
        if unit_state == "not-found":
            if not self.observe_unknown("not_applicable"):
                return 2
            return 3
        if unit_state != "loaded":
            self.observe_unknown("unit_unavailable")
            return 2
        code, routes = run(bounded("1s", "ip", "-j", "-4", "route", "show", "default"), PIPE, DEVNULL)
        values, complete = json_values(routes.rstrip("\n") if code == 0 else "")
        if not routes_valid(values, complete):
            if not self.tailnet_peer:
                self.observe_unknown("routes_unavailable")
                return 2
            defaults = []
        else:
            try:
                defaults = default_routes(values)
            except ValueError:
                return 2
        if os.path.isdir(SYSFS_DIR + "/wlan0"):
            association = run(bounded("1s", "iw", "dev", "wlan0", "link"), PIPE, DEVNULL)[1].rstrip("\n")
            if association.startswith("Connected to "):
                self.probe = "default_route"
                self.reason = "no_default_route"
                for iface, gateway in defaults:
                    if iface != "wlan0":
                        continue
                    if self.interface_works(iface, gateway):
                        return self.observe_positive()
            else:
                self.probe = "association"
                self.reason = "not_associated"
        else:
            self.probe = "interface"
            self.reason = "interface_missing"
        if self.tailnet_peer:
            self.probe = "tsmp"
            self.target = self.tailnet_peer
            self.reason = "peer_pong"
            return self.observe_positive()
        for iface, gateway in defaults:
            if (not iface.startswith("e") or "/" in iface or not os.path.isdir(SYSFS_DIR + "/" + iface)
                    or os.path.isdir(SYSFS_DIR + "/" + iface + "/wireless")):
                continue
            if self.interface_works(iface, gateway):
                return self.observe_positive()
        return self.recover_wifi()


def main(arguments):
    if not arguments or arguments[0] != "--bounded":
        try:
            os.execvp("timeout", ["timeout", "--kill-after=8s", "20s", sys.argv[0], "--bounded", *arguments])
        except OSError as error:
            return 126 if isinstance(error, PermissionError) else 127
    arguments = arguments[1:]
    tailnet_peer = ""
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--tailnet-peer" or not PEER.fullmatch(arguments[1]):
            return 64
        tailnet_peer = arguments[1]
    recovery = WifiRecovery(tailnet_peer)
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)
    try:
        return recovery.main()
    finally:
        recovery.restore_link()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
