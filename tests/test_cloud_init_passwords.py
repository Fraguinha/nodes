import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
PROVIDER_HASH = (
    "$6$rounds=4096$5DJ8a9WMTEzIo5J4$"
    "Yms6imfeBvf3Yfu84mQBerh18l7OR1Wm1BJXZqFSpJ6BVas0AYJqIjP7czkOaAZHZi1kxQ5Y1IhgWN8K9NgxR1"
)


class CloudInitPasswordsTest(unittest.TestCase):
    def test_expired_provider_root_is_locked_and_unexpired(self):
        if sys.platform != "linux":
            self.skipTest("Requires Ubuntu with cloud-init installed")
        if os.geteuid() != 0:
            self.skipTest("Requires root for isolated shadow --root operations")
        try:
            import yaml
            from cloudinit import helpers, subp
            from cloudinit.config import cc_set_passwords
            from cloudinit.distros.ubuntu import Distro
        except ImportError as error:
            self.skipTest(f"Requires Ubuntu cloud-init and PyYAML: {error}")

        authorized_keys = [
            line
            for line in (PROJECT / "config/authorized_keys").read_text().splitlines()
            if line
        ]

        for platform in ("cloud", "physical"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                output = temporary / "user-data.yaml"
                subprocess.run(
                    [
                        "bash", "-euo", "pipefail", "-c",
                        'source "$1/lib/render.sh"; '
                        'render_user_data "$2" node-test join "$3" nodes-init tskey-test ""',
                        "test", str(PROJECT), str(output), platform,
                    ],
                    check=True,
                )
                config = yaml.safe_load(output.read_text())
                pi = next(user for user in config["users"] if user["name"] == "pi")
                self.assertEqual(pi["ssh_authorized_keys"], authorized_keys)
                self.assertEqual(pi["sudo"], "ALL=(ALL) NOPASSWD:ALL")
                self.assertEqual(pi["shell"], "/bin/bash")
                self.assertIn("sudo", pi["groups"])

                root = temporary / "root"
                etc = root / "etc"
                etc.mkdir(parents=True)
                (etc / "passwd").write_text(
                    "root:x:0:0:root:/root:/bin/bash\n"
                    "pi:x:1000:1000::/home/pi:/bin/bash\n"
                )
                (etc / "group").write_text("root:x:0:\npi:x:1000:\nsudo:x:27:pi\n")
                pi_shadow = "pi:!:20000:0:99999:7:::"
                shadow = etc / "shadow"
                shadow.write_text(f"root:{PROVIDER_HASH}:0:0:99999:7:::\n{pi_shadow}\n")
                shadow.chmod(0o600)
                (etc / "login.defs").write_text(
                    "PASS_MAX_DAYS 99999\nPASS_MIN_DAYS 0\nPASS_WARN_AGE 7\n"
                )
                (etc / "nsswitch.conf").write_text(
                    "passwd: files\ngroup: files\nshadow: files\n"
                )
                before = subp.subp(
                    ["passwd", "--root", str(root), "--status", "root"]
                ).stdout.split()
                self.assertEqual(before[1], "P")
                self.assertEqual(shadow.read_text().splitlines()[0].split(":")[2], "0")

                distro = Distro(
                    "ubuntu", {}, helpers.Paths({"cloud_dir": str(temporary / "cloud")})
                )
                account_config = {
                    key: config[key]
                    for key in ("chpasswd", "password")
                    if key in config
                }
                real_subp = subp.subp

                def run_in_root(command, **kwargs):
                    self.assertIn(command[0], ("chpasswd", "passwd"))
                    return real_subp(
                        [command[0], "--root", str(root), *command[1:]], **kwargs
                    )

                with mock.patch.object(subp, "subp", side_effect=run_in_root):
                    cc_set_passwords.handle(
                        "set_passwords", account_config, SimpleNamespace(distro=distro), []
                    )

                after = subp.subp(
                    ["passwd", "--root", str(root), "--status", "root"]
                ).stdout.split()
                self.assertEqual(after[1], "L")
                root_shadow = shadow.read_text().splitlines()[0].split(":")
                self.assertGreater(int(root_shadow[2]), 0)
                self.assertIn(pi_shadow, shadow.read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
