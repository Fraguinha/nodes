import base64
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .common import capture, confirm, die, pass_value, prompt, require_command, run, ssh_node, validate_hostname
from .inventory import inventory_check, inventory_check_removal, inventory_field, inventory_has, inventory_record, inventory_remove, inventory_require
from .render import network_profile_has_wifis, new_instance_id, render_meta_data, render_network_config, render_user_data

SYNC_REBOOT_TIMEOUT = 900
SYNC_CLOUD_INIT_TIMEOUT = 5400
SYNC_READY_TIMEOUT = 900
SYNC_POLL_INTERVAL = 10
PHYSICAL_USAGE = """Usage: nodes provision physical [options]

  --disk PATH
  --arch arm64|amd64
  --hostname NAME
  --role init|join|agent
  --server NAME
  --yes
  --help
"""
CLOUD_USAGE = """Usage: nodes provision cloud [options]

  --hostname NAME
  --role init|join|agent
  --server NAME
  --type TYPE
  --location LOCATION
  --image IMAGE
  --yes
  --help
"""
REMOVE_USAGE = """Usage: nodes remove [options]

  --hostname NAME
  --peer NAME
  --yes
  --help
"""
SYNC_USAGE = """Usage: nodes sync [--yes] HOSTNAME...

Re-provision existing nodes from cloud-init, one at a time. Each node gets a
fresh cloud-init seed rendered from config/nodes.json, is rebooted, and must
finish cloud-init and report Ready before the next node starts.

  --yes   Skip the confirmation prompt
  --help  Show this help

NODES_SSH_CONFIG optionally selects an SSH config with strict known-host checking.
"""


def parse_options(arguments, fields, usage, defaults=None):
    options = {field: "" for field in fields}
    options.update(defaults or {})
    options["yes"] = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("--help", "-h"):
            print(usage, end="")
            raise SystemExit(0)
        if argument == "--yes":
            options["yes"] = True
        elif argument.startswith("--") and argument[2:] in fields:
            if index + 1 == len(arguments):
                die(argument + " requires a value")
            index += 1
            options[argument[2:]] = arguments[index]
        else:
            print(usage, file=sys.stderr, end="")
            die("unknown option '" + argument + "'")
        index += 1
    return options


def load_tailscale_authkey():
    value = os.environ.get("NODES_TAILSCALE_AUTHKEY") or pass_value(os.environ.get("NODES_TAILSCALE_PASS_PATH") or "tailscale/authkey")
    if not value:
        die("Tailscale auth key not found in pass")
    if not value.startswith("tskey-"):
        die("Tailscale auth key must start with tskey-")
    os.environ.pop("NODES_TAILSCALE_AUTHKEY", None)
    return value


def load_github_token():
    value = os.environ.get("NODES_GITHUB_TOKEN") or pass_value(os.environ.get("NODES_GITHUB_PASS_PATH") or "github/token")
    if not value:
        die("GitHub token not found in pass")
    os.environ.pop("NODES_GITHUB_TOKEN", None)
    return value


def load_wifi_credentials():
    ssid = os.environ.get("NODES_WIFI_SSID") or pass_value(os.environ.get("NODES_WIFI_SSID_PASS_PATH") or "wifi/ssid") or prompt("Wi-Fi SSID: ")
    password = os.environ.get("NODES_WIFI_PASSWORD") or pass_value(os.environ.get("NODES_WIFI_PASSWORD_PASS_PATH") or "wifi/password")
    if not password:
        password = prompt("Wi-Fi password: ", secret=True)
        print()
    os.environ.pop("NODES_WIFI_PASSWORD", None)
    return ssid, password


def work_directory(prefix):
    os.umask(0o077)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=os.environ.get("TMPDIR") or "/tmp")


def node_options(options, platform):
    host = options["hostname"] or prompt("Hostname: ")
    validate_hostname(host)
    if platform == "cloud" and not run(["hcloud", "server", "describe", host], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        die("server '" + host + "' already exists")
    role, server = options["role"], options["server"]
    if inventory_has(host):
        role = role or inventory_field(host, "role")
        server = server or inventory_field(host, "server")
    if not role:
        role = prompt("Role (init/join/agent) [join]: " if platform == "cloud" else "Role (init/join/agent): ")
        if platform == "cloud":
            role = role or "join"
    if role not in ("init", "join", "agent"):
        die("role must be 'init', 'join' or 'agent'")
    if role != "init":
        server = server or prompt("Server Tailscale hostname: ")
        validate_hostname(server)
    inventory_check(host, platform, role, server)
    return host, role, server


def up_command(arguments):
    options = parse_options(arguments, ("hostname", "role", "server", "type", "location", "image"), CLOUD_USAGE, {
        "type": os.environ.get("NODES_HCLOUD_TYPE") or "cx23",
        "location": os.environ.get("NODES_HCLOUD_LOCATION") or "nbg1",
        "image": os.environ.get("NODES_HCLOUD_IMAGE") or "ubuntu-24.04",
    })
    server_type, location, image = (options[field] for field in ("type", "location", "image"))
    require_command("pass")
    require_command("hcloud", "brew install hcloud")
    host, role, server = node_options(options, "cloud")
    authkey = load_tailscale_authkey()
    github = load_github_token() if role == "init" else ""
    print("\n  Hostname: " + host + "\n  Role:     " + role)
    if role != "init":
        print("  Server:   " + server)
    print("  Type:     " + server_type + "\n  Location: " + location + "\n  Image:    " + image + "\n")
    confirm("Create the server?", options["yes"])
    with work_directory("nodes-cloud.") as directory:
        output = Path(directory) / "user-data"
        render_user_data(output, host, role, "cloud", server, authkey, github)
        run(["hcloud", "server", "create", "--name", host, "--type", server_type, "--image", image, "--location", location, "--user-data-from-file", output])
        inventory_record(host, "cloud", role, server)
    print("Done. Cloud-init will provision the node in approximately five minutes.")


def down_command(arguments):
    options = parse_options(arguments, ("hostname", "peer"), REMOVE_USAGE, {"peer": os.environ.get("NODES_CLUSTER_PEER", "")})
    require_command("hcloud", "brew install hcloud")
    require_command("ssh")
    host = options["hostname"] or prompt("Hostname: ")
    validate_hostname(host)
    if run(["hcloud", "server", "describe", host], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        die("server '" + host + "' not found")
    if inventory_has(host):
        inventory_check_removal(host)
    peer = options["peer"]
    if not peer:
        lines = capture(["hcloud", "server", "list", "-o", "columns=name"]).stdout.splitlines()[1:]
        peer = next((line.split()[0] for line in lines if line.split() and line.split()[0] != host), host)
    validate_hostname(peer)
    print("\n  Delete: " + host + "\n  Via:    " + peer + "\n")
    confirm("Drain and delete the server?", options["yes"])
    print("Draining " + host + "...")
    if ssh_node(["pi@" + peer, "sudo", "k3s", "kubectl", "drain", host, "--ignore-daemonsets", "--delete-emptydir-data", "--force", "--disable-eviction", "--timeout=30s"], check=False).returncode:
        print("Warning: could not drain " + host, file=sys.stderr)
    print("Removing " + host + " from Kubernetes...")
    if ssh_node(["pi@" + peer, "sudo", "k3s", "kubectl", "delete", "node", host, "--wait=false"], check=False).returncode:
        print("Warning: could not remove " + host + " from Kubernetes", file=sys.stderr)
    print("Deleting " + host + "...")
    run(["hcloud", "server", "delete", host])
    if inventory_has(host):
        inventory_remove(host)
    print("Done.")


def disk_info(disk):
    result = run(["diskutil", "info", "-plist", disk], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        return None
    try:
        return plistlib.loads(result.stdout)
    except (ValueError, plistlib.InvalidFileException):
        return None


def candidate_disks():
    disks = re.findall(r"^(/dev/disk[0-9]+)", capture(["diskutil", "list", "physical"]).stdout, re.M)
    return [disk for disk in disks if (info := disk_info(disk)) and info.get("Internal") is False
            and info.get("VirtualOrPhysical") != "Virtual" and info.get("BusProtocol") != "Disk Image"]


def disk_description(disk):
    info = disk_info(disk)
    if info is None:
        die("disk '" + disk + "' not found")
    size = str(info.get("TotalSize", ""))
    size = "%.1f GB" % (int(size) / 1e9) if re.fullmatch(r"[0-9]+", size) else size
    return disk + " - " + (info.get("MediaName") or "unknown") + " (" + (size or "unknown size") + ")"


def validate_target_disk(disk):
    if not re.fullmatch(r"/dev/disk[0-9]+", disk):
        die("invalid disk '" + disk + "'")
    info = disk_info(disk)
    if info is None:
        die("disk '" + disk + "' not found")
    if "Internal" not in info:
        die("could not determine whether '" + disk + "' is internal")
    if info["Internal"] is not False:
        die("refusing to write internal disk '" + disk + "'")


def latest_lts_version(base):
    versions = re.findall(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", capture(["curl", "-fsSL", "--retry", "3", base + "/"]).stdout)
    if not versions:
        raise SystemExit(1)
    versions = [value for value in versions if int(value.split(".")[0]) % 2 == 0 and value.split(".")[1] == "04"]
    return max(versions, key=lambda value: (*map(int, (value.split(".") + ["0"])[:3]), value)) if versions else ""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_verified_image(base, image, destination):
    checksums = capture(["curl", "-fsSL", "--retry", "3", base + "/SHA256SUMS"]).stdout
    expected = next((line.split()[0] for line in checksums.splitlines() if len(line.split()) > 1 and line.split()[1].lstrip("*") == image), "")
    if not expected:
        die("checksum for " + image + " not found")
    if destination.is_file():
        if sha256_file(destination) == expected:
            print("Using cached image: " + image)
            return
        destination.unlink()
    print("Downloading " + image + "...")
    run(["curl", "-fL", "--retry", "3", "--continue-at", "-", "--output", destination, base + "/" + image])
    if sha256_file(destination) != expected:
        destination.unlink()
        die("checksum verification failed for " + image)


def raw_disk_target(disk, size):
    return 'json:{"driver":"raw","size":' + str(size) + ',"file":{"driver":"host_device","filename":"' + disk + '"}}'


def verify_written_image(source, source_format, disk):
    import json
    size = str(json.loads(capture(["qemu-img", "info", "--output=json", "-f", source_format, source]).stdout).get("virtual-size", ""))
    if not re.fullmatch(r"[1-9][0-9]*", size):
        die("could not determine the image size")
    print("Verifying the written image...")
    if run(["sudo", "qemu-img", "compare", "-p", "-f", source_format, source, raw_disk_target(disk, size)], check=False).returncode:
        die("written image does not match the source")


def flash_command(arguments):
    options = parse_options(arguments, ("disk", "arch", "hostname", "role", "server"), PHYSICAL_USAGE)
    if os.uname().sysname != "Darwin":
        die("physical flashing is supported on macOS")
    for name in ("curl", "diskutil", "plutil", "pass", "shasum"):
        require_command(name)
    disk = options["disk"]
    if not disk:
        disks = candidate_disks()
        if not disks:
            die("no external disks found")
        if len(disks) == 1:
            disk = disks[0]
            print("Detected: " + disk_description(disk))
        else:
            print("External disks:")
            for index, value in enumerate(disks, 1):
                print("  %d) %s" % (index, disk_description(value)))
            choice = prompt("Select disk [1-" + str(len(disks)) + "]: ")
            try:
                number = int(choice, 8 if choice.startswith("0") else 10) if re.fullmatch(r"[0-9]+", choice) else 0
            except ValueError:
                number = 0
            if not 1 <= number <= len(disks):
                die("invalid disk selection")
            disk = disks[number - 1]
    validate_target_disk(disk)
    arch = options["arch"] or prompt("Architecture (arm64/amd64): ")
    if arch not in ("arm64", "amd64"):
        die("architecture must be 'arm64' or 'amd64'")
    require_command("qemu-img", "brew install qemu")
    qemu = shutil.which("qemu-img")
    require_command("xz" if arch == "arm64" else "sgdisk", "brew install xz" if arch == "arm64" else "brew install gptfdisk")
    sgdisk = shutil.which("sgdisk")
    host, role, server = node_options(options, "physical")
    authkey = load_tailscale_authkey()
    github = load_github_token() if role == "init" else ""
    ssid, password = load_wifi_credentials() if network_profile_has_wifis(host) else ("", "")
    print("\n  Disk:     " + disk_description(disk) + "\n  Arch:     " + arch + "\n  Hostname: " + host + "\n  Role:     " + role)
    if role != "init":
        print("  Server:   " + server)
    if ssid:
        print("  Wi-Fi:    " + ssid)
    print()
    confirm("Erase " + disk + " and continue?", options["yes"])
    with work_directory("nodes-flash.") as directory:
        work = Path(directory)
        cache = Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")) / "nodes/images"
        cache.mkdir(parents=True, exist_ok=True)
        render_user_data(work / "user-data", host, role, "physical", server, authkey, github)
        render_network_config(work / "network-config", ssid, password, host)
        render_meta_data(work / "meta-data", host, new_instance_id(host))
        print("Finding the latest Ubuntu Server LTS...")
        base = "https://cdimage.ubuntu.com/releases" if arch == "arm64" else "https://cloud-images.ubuntu.com/releases"
        version = latest_lts_version(base)
        if not version:
            die("could not find an Ubuntu LTS release")
        release = base + "/" + version + "/release"
        listing = capture(["curl", "-fsSL", "--retry", "3", release + "/"]).stdout
        pattern = r"ubuntu-[0-9.]+-preinstalled-server-arm64\+raspi\.img\.xz" if arch == "arm64" else r"ubuntu-[0-9.]+-server-cloudimg-amd64\.img"
        match = re.search(pattern, listing)
        if not match:
            raise SystemExit(1)
        image = match[0]
        print("Selected image: " + image)
        if arch == "arm64" and version.startswith("26.04"):
            print("Raspberry Pi 5 requires boot EEPROM 2025-02-11 or newer for Ubuntu " + version + ".")
            print("Check it on the Pi with: rpi-eeprom-update")
            confirm("EEPROM requirement verified?", options["yes"])
        image_path = cache / image
        download_verified_image(release, image, image_path)
        source, source_format, boot_sector = image_path, "qcow2", None
        if arch == "arm64":
            source, source_format, boot_sector = work / image.removesuffix(".xz"), "raw", work / "arm-boot-sector.bin"
            print("Decompressing " + image + "...")
            with source.open("wb") as handle:
                run(["xz", "-dc", image_path], stdout=handle)
            with source.open("r+b") as handle:
                boot_sector.write_bytes(handle.read(512))
                handle.seek(0)
                handle.write(bytes(512))
        run(["sudo", "-v"])
        print("Writing " + image + " to " + disk + " with QEMU...")
        run(["diskutil", "unmountDisk", disk])
        raw_disk = "/dev/r" + disk.removeprefix("/dev/")
        run(["sudo", qemu, "convert", "-p", "-n", "-S", "0", "-f", source_format, "-O", "raw", source, raw_disk])
        run(["sync"])
        verify_written_image(source, source_format, raw_disk)
        if boot_sector:
            print("Publishing the Raspberry Pi partition table...")
            run(["sudo", qemu, "convert", "-n", "-S", "0", "-f", "raw", "-O", "raw", boot_sector, raw_disk_target(raw_disk, 512)])
            run(["sync"])
            verify_written_image(boot_sector, "raw", raw_disk)
        run(["diskutil", "unmountDisk", disk], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if arch == "arm64":
            print("Installing the cloud-init seed...")
            partition = disk + "s1"
        else:
            print("Adding the cloud-init seed partition...")
            run(["sudo", sgdisk, "-e", disk])
            run(["sudo", sgdisk, "-n", "0:-64M:0", "-t", "0:0700", "-c", "0:CIDATA", disk])
            listing = capture(["sudo", sgdisk, "-p", disk]).stdout
            number = next((line.split()[0] for line in listing.splitlines() if "CIDATA" in line and line.split()), "")
            if not number:
                die("could not create the CIDATA partition")
            partition = disk + "s" + number
        for attempt in range(10):
            if Path(partition).exists():
                break
            run(["sleep", "1"])
        if not Path(partition).exists():
            print()
            prompt("Reconnect the drive, then press enter: ")
            if not Path(partition).exists():
                die("could not find " + partition)
        if arch == "amd64":
            run(["sudo", "newfs_msdos", "-v", "CIDATA", partition])
        run(["diskutil", "mount", partition], stdout=subprocess.DEVNULL)
        info = disk_info(partition)
        if info is None or "MountPoint" not in info:
            die("could not determine the mount point for " + partition)
        mount = Path(info["MountPoint"])
        if not mount.is_dir():
            die("could not mount " + partition)
        if arch == "arm64" and not (mount / "config.txt").is_file():
            run(["diskutil", "unmount", partition], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            die(partition + " is not the Raspberry Pi system-boot partition")
        for name in ("user-data", "meta-data", "network-config"):
            run(["cp", work / name, mount / name])
        run(["sync"])
        run(["diskutil", "eject", disk])
        inventory_record(host, "physical", role, server)
    print("Done. Insert the drive into the " + ("Raspberry Pi" if arch == "arm64" else "PC") + " and power it on.")


def render_sync_seed(seed_dir, host, authkey, github, ssid, password):
    seed_dir = Path(seed_dir)
    platform, role, server = (inventory_field(host, field) for field in ("platform", "role", "server"))
    seed_dir.mkdir(mode=0o700)
    render_user_data(seed_dir / "user-data", host, role, platform, server, authkey, github, "resync")
    render_meta_data(seed_dir / "meta-data", host, new_instance_id(host))
    if platform == "physical":
        render_network_config(seed_dir / "network-config", ssid, password, host)


def seed_install_script(seed_dir):
    files = {name: base64.b64encode((Path(seed_dir) / name).read_bytes()).decode()
             for name in ("meta-data", "user-data", "network-config") if (Path(seed_dir) / name).is_file()}
    return "FILES = " + repr(files) + "\n" + Path(__file__).with_name("seed.py").read_text()


def remote_boot_id(host):
    result = ssh_node(["pi@" + host, "cat", "/proc/sys/kernel/random/boot_id"], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return None if result.returncode else result.stdout.rstrip("\n")


def node_rebooted(host, previous_boot_id):
    boot_id = remote_boot_id(host)
    return bool(boot_id and boot_id != previous_boot_id)


def cloud_init_state(host):
    result = ssh_node(["pi@" + host, 'sudo -n timeout 300 cloud-init status --wait >/dev/null 2>&1; status=$?; if [ -e /run/nodes-reboot ]; then echo rebooting; else echo "$status"; fi'], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.rstrip("\n")


def node_ready(host, via):
    result = ssh_node(["pi@" + via, 'sudo -n k3s kubectl get node ' + host + ' -o \'jsonpath={.status.conditions[?(@.type=="Ready")].status}\''], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return not result.returncode and result.stdout.rstrip("\n") == "True"


def wait_until(timeout, function, *arguments):
    deadline = time.monotonic() + timeout
    while not function(*arguments):
        if time.monotonic() >= deadline:
            return False
        run(["sleep", str(SYNC_POLL_INTERVAL)])
    return True


def wait_for_cloud_init(host):
    deadline = time.monotonic() + SYNC_CLOUD_INIT_TIMEOUT
    while True:
        state = cloud_init_state(host)
        if state == "0":
            return
        if state == "2":
            print("Warning: cloud-init on " + host + " finished with recoverable errors", file=sys.stderr)
            return
        if state == "1":
            print("cloud-init failed on " + host + ". Last lines of /var/log/cloud-init-output.log:", file=sys.stderr)
            ssh_node(["pi@" + host, "sudo -n tail -n 50 /var/log/cloud-init-output.log"], check=False, stdout=sys.stderr)
            die("cloud-init failed on " + host)
        if time.monotonic() >= deadline:
            die("timed out waiting for cloud-init on " + host)
        run(["sleep", str(SYNC_POLL_INTERVAL)])


def sync_node(host, seed_dir):
    via = inventory_field(host, "server") if inventory_field(host, "role") == "agent" else host
    print("==> " + host)
    boot_id = remote_boot_id(host)
    if boot_id is None:
        die("could not reach " + host + " over SSH")
    if not boot_id:
        die("could not read the boot id of " + host)
    print("Installing the cloud-init seed and rebooting " + host + "...")
    if ssh_node(["pi@" + host, "sudo -n python3 -"], check=False, input=seed_install_script(seed_dir), text=True).returncode:
        die("could not install the cloud-init seed on " + host)
    print("Waiting for " + host + " to reboot...")
    if not wait_until(SYNC_REBOOT_TIMEOUT, node_rebooted, host, boot_id):
        die("timed out waiting for " + host + " to reboot")
    print("Waiting for cloud-init on " + host + "...")
    wait_for_cloud_init(host)
    print("Waiting for " + host + " to become Ready...")
    if not wait_until(SYNC_READY_TIMEOUT, node_ready, host, via):
        die("timed out waiting for " + host + " to become Ready")
    print(host + " is Ready.")


def sync_command(arguments):
    assume_yes, hosts = False, []
    for argument in arguments:
        if argument == "--yes":
            assume_yes = True
        elif argument in ("--help", "-h"):
            print(SYNC_USAGE, end="")
            return
        elif argument.startswith("-"):
            die("unknown option '" + argument + "'")
        else:
            hosts.append(argument)
    if not hosts:
        die("usage: nodes sync [--yes] HOSTNAME...")
    require_command("ssh")
    seen, needs_github, needs_wifi = set(), False, False
    for host in hosts:
        inventory_require(host)
        if host in seen:
            die("node '" + host + "' is listed more than once")
        seen.add(host)
        needs_github |= inventory_field(host, "role") == "init"
        needs_wifi |= inventory_field(host, "platform") == "physical" and network_profile_has_wifis(host)
    authkey = load_tailscale_authkey()
    github = load_github_token() if needs_github else ""
    ssid, password = load_wifi_credentials() if needs_wifi else ("", "")
    with work_directory("nodes-sync.") as directory:
        for host in hosts:
            render_sync_seed(Path(directory) / host, host, authkey, github, ssid, password)
        del authkey, github, password
        print("\nEach node is re-provisioned from cloud-init and rebooted, one at a time:")
        for host in hosts:
            print("  " + host + " (" + inventory_field(host, "platform") + ", " + inventory_field(host, "role") + ")")
        print()
        confirm("Reboot and re-provision these nodes?", assume_yes)
        for host in hosts:
            sync_node(host, Path(directory) / host)
    print("Done.")
