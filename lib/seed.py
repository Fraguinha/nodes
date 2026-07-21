import base64
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path("/")


def target(path):
    return ROOT / path.lstrip("/")


def command(arguments, check=True, **kwargs):
    return subprocess.run(arguments, check=check, **kwargs)


def install_seed(files):
    os.umask(0o077)
    seed = target("/var/lib/cloud/seed/nocloud")
    seed.mkdir(mode=0o700, parents=True, exist_ok=True)
    seed.chmod(0o700)
    os.chown(seed, 0, 0)
    (seed / "network-config").unlink(missing_ok=True)
    for name in ("meta-data", "user-data", "network-config"):
        if name not in files:
            continue
        temporary = seed / (name + ".new")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(base64.b64decode(files[name]))
        temporary.chmod(0o600)
        temporary.replace(seed / name)
    for name in ("user-data", "network-config"):
        target("/boot/firmware/" + name).unlink(missing_ok=True)
    result = command(["blkid", "-t", "LABEL=CIDATA", "-o", "device"], check=False,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    devices = result.stdout.splitlines()
    if devices:
        media = Path(tempfile.mkdtemp())
        try:
            mounted = command(["mount", "-t", "vfat", "-o", "nodev,nosuid,noexec", devices[0], str(media)], check=False)
            if not mounted.returncode:
                for name in ("user-data", "network-config"):
                    (media / name).unlink(missing_ok=True)
                command(["umount", str(media)])
        finally:
            media.rmdir()
    target("/etc/cloud/cloud-init.disabled").unlink(missing_ok=True)
    command(["cloud-init", "clean", "--logs"])
    command(["systemd-run", "--quiet", "--on-active=5s", "/usr/bin/systemctl", "reboot"])


if __name__ == "__main__":
    install_seed(FILES)
