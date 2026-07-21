#!/usr/bin/env python3
import contextlib
import datetime
import fnmatch
import glob
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

CONFIG = "/etc/nodes/config"
POLICY = "/usr/local/share/nodes/node-policy.json"
SEED_DIR = "/var/lib/cloud/seed/nocloud"
BOOT_MEDIA = "/boot/firmware"
K3S_BIN = "/usr/local/bin/k3s"
K3S_CONFIG_DIR = "/etc/rancher/k3s/config.yaml.d"
CONTAINERD_CONFIG = "/var/lib/rancher/k3s/agent/etc/containerd/config.toml"
NVIDIA_SOURCES = "/etc/apt/sources.list.d/nvidia-container-toolkit.sources"
REBOOT_MARKER = "/run/nodes-reboot"
REBOOT_REQUIRED = "/run/reboot-required"
CLOUD_INIT_DISABLED = "/etc/cloud/cloud-init.disabled"
NETPLAN = "/etc/netplan"
SHADOW = "/etc/shadow"
PRIVATE_DIR = "/run"
NAME = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*=")
ESCAPES = {
    ord("a"): b"\a", ord("b"): b"\b", ord("e"): b"\x1b", ord("E"): b"\x1b", ord("f"): b"\f",
    ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("v"): b"\v", ord("\\"): b"\\",
    ord("'"): b"'", ord('"'): b'"', ord("?"): b"?",
}
OCTAL = b"01234567"
HEX = b"0123456789abcdefABCDEF"
BLANKS = b" \t\n"
FORBIDDEN = b"|&;()<>`$"
NVIDIA_RUNTIME = re.compile(rb"""runtimes\.['"]?nvidia['"]?\]""")
SPACE = "[ \t\v\f\r]"
SWAP = re.compile(rb"^([^#\s]+\s+\S+\s+swap\s)")
LAN = re.compile(".*" + SPACE + "src" + SPACE + "([0-9.]+)")
NVIDIA_MODULES = re.compile("linux-modules-nvidia-[0-9]+-server-generic")


class Failure(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def status(code):
    return 128 - code if code < 0 else code


def run(arguments, env=None, stdout=None, stderr=None):
    try:
        return status(subprocess.run(arguments, env=env, stdout=stdout, stderr=stderr).returncode)
    except OSError as error:
        return 126 if isinstance(error, PermissionError) else 127


def require(code, arguments):
    if code:
        raise Failure("command failed: " + " ".join(arguments), code)


def check(arguments, env=None, stdout=None, stderr=None):
    require(run(arguments, env, stdout, stderr), arguments)


def capture(arguments, stderr=None):
    try:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=stderr,
                                encoding="utf-8", errors="surrogateescape")
    except OSError as error:
        return 126 if isinstance(error, PermissionError) else 127, ""
    return status(result.returncode), result.stdout


def pipe(producer, consumer, env=None):
    try:
        first = subprocess.Popen(producer, stdout=subprocess.PIPE)
    except OSError as error:
        return 126 if isinstance(error, PermissionError) else 127
    try:
        second = subprocess.Popen(consumer, stdin=first.stdout, env=env)
    except OSError as error:
        first.stdout.close()
        first.wait()
        return 126 if isinstance(error, PermissionError) else 127
    first.stdout.close()
    second_code = status(second.wait())
    first_code = status(first.wait())
    return second_code or first_code


def check_pipe(producer, consumer, env=None):
    require(pipe(producer, consumer, env), [*producer, "|", *consumer])


def log(message):
    check(["logger", "-t", "node-setup", message])


def wait_for(description, *arguments):
    while run(list(arguments), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
        log("waiting for " + description)
        time.sleep(5)


def retry_output(function):
    while True:
        value = function()
        if value:
            return value
        time.sleep(10)


def ansi_c(data, position):
    value = bytearray()
    while position < len(data):
        byte = data[position]
        position += 1
        if byte == ord("'"):
            return value, position
        if byte != ord("\\"):
            value.append(byte)
            continue
        if position >= len(data):
            break
        code = data[position]
        position += 1
        if code in ESCAPES:
            value += ESCAPES[code]
        elif code in OCTAL:
            digits = bytes([code])
            while len(digits) < 3 and position < len(data) and data[position] in OCTAL:
                digits += data[position:position + 1]
                position += 1
            value.append(int(digits, 8) & 0xFF)
        elif code in b"xuU":
            limit = {ord("x"): 2, ord("u"): 4, ord("U"): 8}[code]
            digits = b""
            while len(digits) < limit and position < len(data) and data[position] in HEX:
                digits += data[position:position + 1]
                position += 1
            if not digits:
                value += b"\\" + bytes([code])
            elif code == ord("x"):
                value.append(int(digits, 16))
            else:
                try:
                    value += chr(int(digits, 16)).encode("utf-8")
                except (UnicodeEncodeError, ValueError):
                    raise ValueError("invalid unicode escape") from None
        elif code == ord("c"):
            if position >= len(data):
                break
            control = data[position]
            position += 1
            value.append(0x7F if control == ord("?") else bytes([control]).upper()[0] & 0x1F)
        else:
            value += b"\\" + bytes([code])
    raise ValueError("unterminated ANSI-C quote")


def double_quoted(data, position):
    value = bytearray()
    while position < len(data):
        byte = data[position]
        position += 1
        if byte == ord('"'):
            return value, position
        if byte in b"$`":
            raise ValueError("expansion in double quotes")
        if byte == ord("\\") and position < len(data) and data[position] in b'$`"\\\n':
            if data[position] != ord("\n"):
                value.append(data[position])
            position += 1
            continue
        value.append(byte)
    raise ValueError("unterminated double quote")


def parse_word(data, position):
    value = bytearray()
    while position < len(data) and data[position] not in BLANKS:
        byte = data[position]
        if byte == ord("\\"):
            if position + 1 >= len(data):
                raise ValueError("trailing backslash")
            if data[position + 1] != ord("\n"):
                value.append(data[position + 1])
            position += 2
        elif byte == ord("'"):
            end = data.find(b"'", position + 1)
            if end < 0:
                raise ValueError("unterminated single quote")
            value += data[position + 1:end]
            position = end + 1
        elif data.startswith(b"$'", position):
            quoted, position = ansi_c(data, position + 2)
            value += quoted
        elif byte == ord('"'):
            quoted, position = double_quoted(data, position + 1)
            value += quoted
        elif byte in FORBIDDEN:
            raise ValueError("shell syntax in config value")
        else:
            value.append(byte)
            position += 1
    if b"\0" in value:
        raise ValueError("NUL in config value")
    return bytes(value), position


def parse_config(data):
    values = {}
    position = 0
    while position < len(data):
        if data[position] in BLANKS:
            position += 1
            continue
        if data[position] == ord("#"):
            end = data.find(b"\n", position)
            position = len(data) if end < 0 else end
            continue
        match = NAME.match(data, position)
        if not match:
            raise ValueError("invalid config entry")
        value, position = parse_word(data, match.end())
        values[match.group()[:-1].decode()] = value.decode("utf-8", "surrogateescape")
    return values


def read_config(path):
    try:
        return parse_config(Path(path).read_bytes())
    except ValueError as error:
        raise Failure("invalid " + path + ": " + str(error)) from None


def load_policy():
    return json.loads(Path(POLICY).read_text())


def read_value(path):
    try:
        return Path(path).read_text(errors="surrogateescape").rstrip("\n")
    except OSError:
        return ""


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def k3s_install_env(k3s_binary):
    if os.access(k3s_binary, os.X_OK):
        return "INSTALL_K3S_SKIP_DOWNLOAD", "true"
    return "INSTALL_K3S_CHANNEL", "stable"


def write_tls_config(output, sans):
    temporary = Path(str(output) + ".tmp")
    temporary.write_text("tls-san:\n" + "".join("  - " + json.dumps(san) + "\n" for san in sans))
    os.chmod(temporary, 0o644)
    os.replace(temporary, output)


def persist_seed_from(media):
    media = Path(media)
    seed = Path(SEED_DIR)
    if not (media / "user-data").is_file():
        return
    if not (seed / "user-data").is_file() and (media / "meta-data").is_file():
        seed.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(seed, 0o700)
        (seed / "network-config").unlink(missing_ok=True)
        for name in ("meta-data", "user-data", "network-config"):
            if (media / name).is_file():
                write_private(seed / name, (media / name).read_bytes())
        log("cloud-init seed moved from " + str(media) + " to " + str(seed))
    (media / "user-data").unlink(missing_ok=True)
    (media / "network-config").unlink(missing_ok=True)
    os.sync()


def persist_seed():
    persist_seed_from(BOOT_MEDIA)
    device = capture(["blkid", "-t", "LABEL=CIDATA", "-o", "device"], stderr=subprocess.DEVNULL)[1].split("\n", 1)[0]
    if not device:
        return
    mount_dir = tempfile.mkdtemp()
    try:
        if run(["mount", "-t", "vfat", "-o", "nodev,nosuid,noexec", device, mount_dir]) == 0:
            try:
                persist_seed_from(mount_dir)
            finally:
                check(["umount", mount_dir])
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(mount_dir)


def configure_accounts():
    try:
        user = load_policy()["users"]["pi"]
        shell = user["shell"]
        groups = user["groups"]
    except (KeyError, TypeError):
        raise Failure("invalid node policy") from None
    if not isinstance(shell, str) or not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
        raise Failure("invalid node policy")
    check(["usermod", "--shell", shell, "--append", "--groups", ",".join(groups), "pi"])
    changed = []
    for line in Path(SHADOW).read_text(errors="surrogateescape").split("\n"):
        fields = line.split(":")
        if fields[0] == "root":
            changed.append(fields[2] if len(fields) > 2 else "")
    root_changed = "\n".join(changed).rstrip("\n")
    if root_changed in ("", "0"):
        check(["chage", "--lastday", datetime.date.today().isoformat(), "root"])


def disable_swap():
    check(["swapoff", "-a"])
    fstab = Path("/etc/fstab")
    original = fstab.read_bytes()
    content = b"\n".join(SWAP.sub(rb"#\1", line, count=1) for line in original.split(b"\n"))
    if content == original:
        return
    details = fstab.stat()
    descriptor, temporary = tempfile.mkstemp(dir=fstab.parent, prefix=".fstab.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, details.st_mode & 0o7777)
        os.chown(temporary, details.st_uid, details.st_gid)
        os.replace(temporary, fstab)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def disable_multipath():
    for uuid in sorted(glob.glob("/sys/block/dm-*/dm/uuid")):
        if not os.access(uuid, os.R_OK):
            continue
        try:
            value = Path(uuid).read_bytes()
        except OSError:
            continue
        if value.lower().startswith(b"mpath-"):
            log("multipath maps present; multipathd left unchanged")
            return
    check(["systemctl", "mask", "multipathd.service", "multipathd.socket"])
    run(["systemctl", "stop", "multipathd.socket", "multipathd.service"], stderr=subprocess.DEVNULL)


def restrict_netplan():
    with os.scandir(NETPLAN) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and fnmatch.fnmatchcase(entry.name, "*.yaml"):
                os.chmod(entry.path, 0o600)


def configure_services():
    check(["systemctl", "daemon-reload"])
    check(["sysctl", "--system"], stdout=subprocess.DEVNULL)
    check(["systemctl", "restart", "systemd-journald.service"])
    if run(["systemctl", "cat", "rsyslog.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        check(["systemctl", "restart", "rsyslog.service"])
    if run(["timeout", "90s", "systemctl", "enable", "--now", "watchdog-load.service"]):
        log("watchdog activation requires assessment")
    check(["timeout", "15s", "systemctl", "daemon-reexec"])
    check(["systemctl", "enable", "--now", "node-health.timer"])
    check(["systemctl", "enable", "--now", "node-containerd-apparmor.service"])


def configure_physical():
    check(["udevadm", "control", "--reload"])
    if os.path.exists("/sys/class/net/wlan0"):
        if run(["iw", "dev", "wlan0", "set", "power_save", "off"]):
            log("could not disable Wi-Fi power saving")


def tailscale_status():
    code, output = capture(["tailscale", "status", "--json"], stderr=subprocess.DEVNULL)
    if code:
        return None
    try:
        return json.loads(output)
    except ValueError:
        return None


def setup_tailscale(hostname, authkey):
    if shutil.which("tailscale") is None:
        check_pipe(["curl", "-fsSL", "https://tailscale.com/install.sh"], ["sh"])
    check(["systemctl", "enable", "--now", "tailscaled"])
    current = tailscale_status()
    if isinstance(current, dict) and current.get("BackendState") == "Running":
        check(["tailscale", "set", "--ssh=true", "--hostname=" + hostname])
    else:
        private = tempfile.mkdtemp(prefix="node-setup.", dir=PRIVATE_DIR)
        try:
            os.chmod(private, 0o700)
            key = os.path.join(private, "tailscale-authkey")
            write_private(key, authkey.encode("utf-8", "surrogateescape"))
            check(["tailscale", "up", "--auth-key=file:" + key, "--ssh", "--hostname=" + hostname])
        finally:
            shutil.rmtree(private, ignore_errors=True)
    wait_for("Tailscale", "tailscale", "status")


def tailscale_ip():
    arguments = ["tailscale", "ip", "-4"]
    code, output = capture(arguments)
    require(code, arguments)
    return output.split("\n", 1)[0]


def resolve_peer_ip(server_hostname):
    current = tailscale_status()
    peers = current.get("Peer") if isinstance(current, dict) else None
    if not isinstance(peers, dict):
        return ""
    for peer in peers.values():
        if not isinstance(peer, dict) or peer.get("HostName") != server_hostname:
            continue
        for address in peer.get("TailscaleIPs") or []:
            if isinstance(address, str) and ":" not in address:
                return address
    return ""


def node_token(server_ip):
    code, output = capture([
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/etc/nodes/known_hosts",
        "pi@" + server_ip,
        "sudo cat /var/lib/rancher/k3s/server/node-token",
    ], stderr=subprocess.DEVNULL)
    return "" if code else output.rstrip("\n")


def nvidia_devices():
    devices = []
    for device in sorted(glob.glob("/sys/bus/pci/devices/*")):
        vendor = os.path.join(device, "vendor")
        kind = os.path.join(device, "class")
        if not (os.access(vendor, os.R_OK) and os.access(kind, os.R_OK)):
            continue
        if read_value(vendor) == "0x10de" and read_value(kind).startswith("0x03"):
            devices.append(device)
    return devices


def missing_packages(packages):
    missing = []
    for package in packages:
        code, output = capture(["dpkg-query", "-W", "-f=${db:Status-Status}", package], stderr=subprocess.DEVNULL)
        if (output.rstrip("\n") if code == 0 else "") != "installed":
            missing.append(package)
    return missing


def nvidia_packages():
    try:
        packages = load_policy()["packages"]["nvidia"]
    except (KeyError, TypeError):
        raise Failure("invalid node policy") from None
    if not isinstance(packages, list) or not all(isinstance(package, str) for package in packages):
        raise Failure("invalid node policy")
    return list(packages)


def setup_nvidia():
    devices = nvidia_devices()
    if not devices:
        return False, False
    reboot = False
    shutil.copyfile("/usr/local/share/nodes/nvidia-udev.rules", "/etc/udev/rules.d/71-nvidia.rules")
    os.chmod("/etc/udev/rules.d/71-nvidia.rules", 0o644)
    Path(NVIDIA_SOURCES).write_text(
        "Types: deb\n"
        "URIs: https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH)\n"
        "Suites: /\n"
        "Signed-By: /usr/local/share/nodes/nvidia-container-toolkit.asc\n")
    os.chmod(NVIDIA_SOURCES, 0o644)
    check(["udevadm", "control", "--reload-rules"])
    running = os.uname().release
    packages = nvidia_packages()
    for package in list(packages):
        if NVIDIA_MODULES.fullmatch(package):
            packages.append(package[:-len("generic")] + running)
    if not missing_packages(["linux-modules-extra-" + running]):
        for kernel in sorted(glob.glob("/boot/vmlinuz-*")):
            if os.path.isfile(kernel):
                packages.append("linux-modules-extra-" + kernel[len("/boot/vmlinuz-"):])
    missing = missing_packages(packages)
    if missing:
        check(["apt-get", "-o", "DPkg::Lock::Timeout=300", "update"])
        check(["apt-get", "-o", "DPkg::Lock::Timeout=300", "-o", "Dpkg::Options::=--force-confold",
               "install", "--yes", "--no-upgrade", *missing],
              env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"})
        check(["update-initramfs", "-u", "-k", "all"])
    for device in devices:
        if os.path.basename(os.path.realpath(os.path.join(device, "driver"))) != "nvidia":
            log("NVIDIA GPU " + os.path.basename(device) + " is not bound to nvidia; reboot required")
            reboot = True
    return True, reboot


def nvidia_runtime_registered():
    try:
        return bool(NVIDIA_RUNTIME.search(Path(CONTAINERD_CONFIG).read_bytes()))
    except OSError:
        return False


def register_nvidia_runtime(unit):
    if not os.access("/usr/bin/nvidia-container-runtime", os.X_OK) or nvidia_runtime_registered():
        return
    check(["systemctl", "restart", unit])
    attempt = 0
    while not nvidia_runtime_registered():
        attempt += 1
        if attempt >= 24:
            log("NVIDIA container runtime not registered with K3s")
            return
        time.sleep(5)


def lan_ip():
    arguments = ["ip", "-4", "route", "get", "1.1.1.1"]
    code, output = capture(arguments)
    require(code, arguments)
    return "\n".join(match.group(1) for match in map(LAN.match, output.split("\n")) if match)


def public_ip():
    code, output = capture(["curl", "-fsS", "--max-time", "5", "http://169.254.169.254/hetzner/v1/metadata/public-ipv4"])
    if code:
        output += capture(["curl", "-fsS", "--max-time", "10", "https://ifconfig.me/ip"])[1]
    return output.rstrip("\n")


def configure_k3s_files(role, platform, hostname, address):
    directory = Path(K3S_CONFIG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)
    (directory / "20-leader-election.yaml").unlink(missing_ok=True)
    if role == "agent":
        (directory / "20-server-ha.yaml").unlink(missing_ok=True)
        (directory / "30-node-tls.yaml").unlink(missing_ok=True)
        return
    sans = [address]
    if platform == "physical":
        lan = lan_ip()
        if lan:
            sans.append(lan)
        sans.append(hostname + ".local")
    else:
        public = public_ip()
        if public:
            sans.append(public)
    write_tls_config(directory / "30-node-tls.yaml", sans)


def k3s_arguments(role, address, server_ip):
    common = [
        "--node-ip", address,
        "--node-external-ip", address,
        "--flannel-iface", "tailscale0",
        "--resolv-conf", "/etc/k3s-resolv.conf",
        "--kubelet-arg", "allowed-unsafe-sysctls=net.ipv4.ip_forward",
    ]
    server = ["--server", "https://" + server_ip + ":6443"]
    if role == "agent":
        return ["agent", *server, *common], "k3s-agent.service"
    arguments = ["server", *common, "--secrets-encryption", "--write-kubeconfig-mode", "0644", "--disable", "servicelb"]
    return arguments + (["--cluster-init"] if role == "init" else server), "k3s.service"


def install_k3s(arguments, token):
    name, value = k3s_install_env(K3S_BIN)
    env = {**os.environ, name: value}
    if token:
        env["K3S_TOKEN"] = token
    check_pipe(["curl", "-fsSL", "https://get.k3s.io"], ["sh", "-s", "-", *arguments], env)


def bootstrap_flux(github_token):
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    wait_for("Kubernetes API", "kubectl", "get", "nodes")
    check_pipe(["curl", "-fsSL", "https://fluxcd.io/install.sh"], ["bash"])
    check(["flux", "bootstrap", "github", "--owner=Fraguinha", "--repository=flux", "--branch=main",
           "--path=clusters/k8s-cluster", "--personal"], env={**os.environ, "GITHUB_TOKEN": github_token})


def setup():
    if not os.access(CONFIG, os.R_OK):
        raise Failure(CONFIG + " not found")
    config = read_config(CONFIG)
    role = config.get("NODE_ROLE", "")
    platform = config.get("NODE_PLATFORM", "")
    hostname = config.get("NODE_HOSTNAME", "")
    if role not in ("init", "join", "agent"):
        raise Failure("invalid node role")
    if platform not in ("physical", "cloud"):
        raise Failure("invalid node platform")
    k3s_present = os.access(K3S_BIN, os.X_OK)

    Path(CLOUD_INIT_DISABLED).unlink(missing_ok=True)
    persist_seed()
    configure_accounts()
    disable_swap()
    disable_multipath()
    restrict_netplan()
    configure_services()
    if platform == "physical":
        configure_physical()

    wait_for("internet access", "ping", "-c1", "-W2", "1.1.1.1")
    check(["systemctl", "restart", "chrony.service"])
    wait_for("time synchronization", "chronyc", "waitsync", "1", "0.1", "0.0", "1")

    setup_tailscale(hostname, config.get("TAILSCALE_AUTHKEY", ""))
    address = tailscale_ip()
    nvidia_present, reboot = setup_nvidia()
    configure_k3s_files(role, platform, hostname, address)

    server_ip = ""
    token = ""
    if role != "init":
        server_hostname = config.get("SERVER_HOSTNAME", "")
        server_ip = retry_output(lambda: resolve_peer_ip(server_hostname))
        token = retry_output(lambda: node_token(server_ip))

    arguments, unit = k3s_arguments(role, address, server_ip)
    install_k3s(arguments, token)
    if nvidia_present:
        register_nvidia_runtime(unit)
    if role == "init":
        bootstrap_flux(config.get("GITHUB_TOKEN", ""))

    config.clear()
    Path(CONFIG).unlink(missing_ok=True)
    if platform == "physical" and (not k3s_present or os.path.exists(REBOOT_REQUIRED)):
        reboot = True
    if reboot:
        Path(REBOOT_MARKER).touch()
    log("provisioning complete")


def terminate(signum, frame):
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    try:
        setup()
    except Failure as error:
        print("Error: " + str(error), file=sys.stderr)
        return error.code
    except OSError as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
