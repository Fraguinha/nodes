import base64
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

PROJECT = Path(__file__).resolve().parents[1]
COMMON = PROJECT / "rootfs/common"
WHICH = shutil.which
SPEC = importlib.util.spec_from_file_location("node_apparmor", COMMON / "usr/local/bin/node-containerd-apparmor.py")
NODE = importlib.util.module_from_spec(SPEC)
exec(compile(Path(SPEC.origin).read_text(), SPEC.origin, "exec"), NODE.__dict__)
CONFIG_SPEC = importlib.util.spec_from_file_location("apparmor_reconciler", COMMON / "usr/local/bin/node-configure.py")
CONFIG = importlib.util.module_from_spec(CONFIG_SPEC)
exec(compile(Path(CONFIG_SPEC.origin).read_text(), CONFIG_SPEC.origin, "exec"), CONFIG.__dict__)
ASSETS = (
    "usr/local/bin/node-containerd-apparmor.py",
    "usr/local/share/nodes/containerd-v2.3.7-k3s1.apparmor",
    "etc/systemd/system/node-containerd-apparmor.service",
    "etc/systemd/system/k3s.service.d/10-apparmor.conf",
    "etc/systemd/system/k3s-agent.service.d/10-apparmor.conf",
)


class Host:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.k3s_version = "k3s version v1.37.2+k3s1 (f6973cb8)\n"
        self.runtime = "ctr github.com/k3s-io/containerd/v2 v2.3.7-k3s1\n"
        self.parser_version = "AppArmor parser version 5.0.0~beta1\n"
        self.parser_failure = None
        for path, content in (
            ("/sys/module/apparmor/parameters/enabled", "Y\n"),
            ("/sys/kernel/security/apparmor/profiles", NODE.NAME + " (enforce)\n"),
            ("/proc/self/attr/current", "unconfined\n"),
            (NODE.K3S, "k3s\n"),
            ("/etc/os-release", 'ID=ubuntu\nVERSION_ID="26.04"\n'),
            (NODE.BUNDLE + "/ctr", "ctr\n"),
            ("/etc/apparmor.d/abi/3.0", "abi\n"),
            ("/etc/apparmor.d/tunables/global", "global\n"),
            ("/etc/apparmor.d/abstractions/base", "base\n"),
            (NODE.PROFILE, (COMMON / NODE.PROFILE.lstrip("/")).read_text()),
        ):
            self.write(path, content)

    def write(self, path, content):
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def command(self, args, content=None):
        self.calls.append((args, content))
        if args == [str(self.root / NODE.K3S.lstrip("/")), "--version"]:
            return self.k3s_version
        if args[-2:] == ["ctr", "--version"] or args == [str(self.root / NODE.BUNDLE.lstrip("/") / "ctr"), "--version"]:
            return self.runtime
        if args[:2] == ["systemctl", "show"]:
            return "\n"
        if args == ["/parser", "--version"]:
            return self.parser_version
        if args in (["/parser", "-Q", "-K", "-T"], ["/parser", "-r", "-K", "-T"]):
            if self.parser_failure in args:
                raise RuntimeError("parser rejected policy")
            if "-r" in args:
                self.write("/sys/kernel/security/apparmor/profiles", NODE.NAME + " (enforce)\n")
            return ""
        raise AssertionError(args)

    def which(self, name, path=None):
        return "/parser" if name == "apparmor_parser" else WHICH(name, path=path)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}


class NodeAppArmorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.host = Host(Path(temporary.name))
        self.output = io.StringIO()
        self.errors = io.StringIO()
        for patch in (
            mock.patch.object(NODE, "ROOT", self.host.root),
            mock.patch.object(NODE, "command", self.host.command),
            mock.patch.object(NODE.shutil, "which", self.host.which),
            contextlib.redirect_stdout(self.output),
            contextlib.redirect_stderr(self.errors),
        ):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)

    def loads(self):
        return [(args, content) for args, content in self.host.calls if "-r" in args]

    def test_known_bundle_loads_exact_asset_and_preserves_enforcement(self):
        self.assertEqual(NODE.main([]), 0, self.errors.getvalue())
        profile = (COMMON / NODE.PROFILE.lstrip("/")).read_text()
        self.assertEqual(self.loads(), [(["/parser", "-r", "-K", "-T"], profile)])
        self.assertEqual((self.host.root / NODE.RECEIPT.lstrip("/")).read_text(), hashlib.sha256(profile.encode()).hexdigest() + "\n")
        self.assertIn("(enforce)", (self.host.root / "sys/kernel/security/apparmor/profiles").read_text())
        self.assertTrue(any(args[-2:] == ["ctr", "--version"] for args, _ in self.host.calls))

    def test_future_and_unknown_k3s_never_inspect_or_replace_runtime_policy(self):
        before = self.host.snapshot()
        for version in ("v1.38.0+k3s1", "v1.37.3+k3s1", "v1.37.2+k3s2", "v1.37.2+k3s1-custom"):
            self.host.calls.clear()
            self.host.k3s_version = "k3s version " + version + " (f6973cb8)\n"
            self.assertEqual(NODE.main([]), 0)
            self.assertEqual(len(self.host.calls), 1)
            self.assertEqual(self.host.snapshot(), before)
            self.assertEqual(self.loads(), [])

    def test_unknown_runtime_version_or_fork_never_loads(self):
        before = self.host.snapshot()
        for runtime in (self.host.runtime.replace("k3s-io", "containerd"), self.host.runtime.replace(NODE.VERSION, "v2.4.2")):
            self.host.runtime = runtime
            self.host.calls.clear()
            self.assertEqual(NODE.main([]), 0)
            self.assertEqual(len(self.host.calls), 2)
            self.assertEqual(self.loads(), [])
            self.assertEqual(self.host.snapshot(), before)

    def test_known_k3s_version_with_unknown_build_does_not_override(self):
        self.host.k3s_version = self.host.k3s_version.replace("f6973cb8", "ffffffff")
        before = self.host.snapshot()
        self.assertEqual(NODE.main([]), 0)
        self.assertEqual(len(self.host.calls), 1)
        self.assertEqual(self.host.snapshot(), before)

    def test_other_os_versions_never_select_or_load_a_runtime(self):
        for release in ('ID=ubuntu\nVERSION_ID="24.04"\n', 'ID=debian\nVERSION_ID="26.04"\n'):
            self.host.write("/etc/os-release", release)
            before = self.host.snapshot()
            self.assertEqual(NODE.main([]), 0)
            self.assertEqual(self.host.calls, [])
            self.assertEqual(self.host.snapshot(), before)

    def test_disabled_apparmor_and_absent_k3s_are_readonly_skips(self):
        self.host.write("/sys/module/apparmor/parameters/enabled", "N\n")
        before = self.host.snapshot()
        self.assertEqual(NODE.main([]), 0)
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.snapshot(), before)
        self.host.write("/sys/module/apparmor/parameters/enabled", "Y\n")
        for path in ("sys/module/apparmor/parameters/enabled", NODE.K3S.lstrip("/")):
            (self.host.root / path).unlink()
            before = self.host.snapshot()
            self.assertEqual(NODE.main([]), 0)
            self.assertEqual(self.host.calls, [])
            self.assertEqual(self.host.snapshot(), before)
            if path.startswith("sys/"):
                self.host.write("/sys/module/apparmor/parameters/enabled", "Y\n")

    def test_check_without_receipt_never_stages_loads_or_writes(self):
        before = self.host.snapshot()
        self.assertEqual(NODE.main(["--check"]), 1)
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual(self.loads(), [])
        self.assertFalse(any(args[-2:] == ["ctr", "--version"] for args, _ in self.host.calls))
        self.assertIn("pending:", self.output.getvalue())

    def test_check_after_load_is_readonly_and_detects_lost_enforcement(self):
        self.assertEqual(NODE.main([]), 0)
        self.host.calls.clear()
        before = self.host.snapshot()
        self.assertEqual(NODE.main(["--check"]), 0)
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual(self.loads(), [])
        self.host.write("/sys/kernel/security/apparmor/profiles", NODE.NAME + " (complain)\n")
        self.assertEqual(NODE.main(["--check"]), 1)
        self.assertEqual(self.loads(), [])

    def test_check_missing_cached_bundle_does_not_extract(self):
        (self.host.root / NODE.BUNDLE.lstrip("/") / "ctr").unlink()
        before = self.host.snapshot()
        self.assertEqual(NODE.main(["--check"]), 1)
        self.assertEqual(len(self.host.calls), 1)
        self.assertEqual(self.host.snapshot(), before)

    def test_render_and_rollback_baseline_differ_only_by_upstream_two_rules(self):
        before = self.host.snapshot()
        self.assertEqual(NODE.main(["--render"]), 0)
        candidate = self.output.getvalue()
        self.output.seek(0)
        self.output.truncate()
        self.assertEqual(NODE.main(["--render-baseline"]), 0)
        baseline = self.output.getvalue()
        self.assertEqual(candidate, (COMMON / NODE.PROFILE.lstrip("/")).read_text())
        for addition in NODE.ADDITIONS:
            candidate = candidate.replace(addition, "")
            self.assertNotIn(addition, baseline)
        self.assertEqual(candidate, baseline)
        self.assertEqual(sum(line.lstrip().startswith("deny ") for line in baseline.splitlines()), 15)
        self.assertIn("deny mount,", baseline)
        self.assertIn("deny /sys/kernel/security/** rwklx,", baseline)
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual(self.loads(), [])

    def test_incompatible_imports_peer_rootless_or_parser_refuse_load(self):
        for variation in ("imports", "peer", "rootlesskit", "parser"):
            with self.subTest(variation=variation):
                self.host.calls.clear()
                with contextlib.ExitStack() as stack:
                    if variation == "imports":
                        path = self.host.root / "etc/apparmor.d/abi/3.0"
                        path.unlink()
                        stack.callback(path.write_text, "abi\n")
                    elif variation == "peer":
                        path = self.host.write("/proc/self/attr/current", "other-profile (enforce)\n")
                        stack.callback(path.write_text, "unconfined\n")
                    elif variation == "rootlesskit":
                        path = self.host.write(NODE.BUNDLE + "/rootlesskit", "executable\n")
                        path.chmod(0o755)
                        stack.callback(path.unlink)
                    else:
                        self.host.parser_version = "AppArmor parser version 5.1.0\n"
                    self.assertEqual(NODE.main([]), 1)
                    self.assertEqual(self.loads(), [])
                    self.assertFalse((self.host.root / NODE.RECEIPT.lstrip("/")).exists())

    def socket_path(self):
        path = self.host.root / NODE.SOCKET.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def daemon_process(self, pid, context="unconfined\n", path="/nonexistent"):
        executable = self.host.root / NODE.BUNDLE.lstrip("/") / "k3s"
        if not executable.exists():
            self.host.write(NODE.BUNDLE + "/k3s", "multicall\n")
            (executable.parent / "containerd").symlink_to(executable)
        process = self.host.root / "proc" / str(pid)
        process.mkdir(parents=True, exist_ok=True)
        if not (process / "exe").is_symlink():
            (process / "exe").symlink_to(executable)
        self.host.write(f"/proc/{pid}/comm", "containerd\n")
        self.host.write(f"/proc/{pid}/attr/current", context)
        self.host.write(f"/proc/{pid}/environ", "PATH=" + path + "\0")

    def test_stale_socket_after_daemon_death_does_not_block_boot_load(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path()))
        listener.listen(1)
        listener.close()
        self.assertTrue(self.socket_path().exists())
        self.assertEqual(NODE.main([]), 0, self.errors.getvalue())
        self.assertEqual(len(self.loads()), 1)

    def test_active_listener_must_be_the_verified_multicall_daemon(self):
        pid = os.getpid()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.socket_path()))
            listener.listen(4)
            self.assertEqual(NODE.main(["--render"]), 1)
            self.assertIn("listener context could not be proved", self.errors.getvalue())
            self.daemon_process(pid, context="different (enforce)\n")
            self.assertEqual(NODE.main([]), 1)
            self.assertIn("daemon-peer context differs", self.errors.getvalue())
            rootless = self.host.write("/runtime-path/rootlesskit", "executable\n")
            rootless.chmod(0o755)
            self.daemon_process(pid, path=str(rootless.parent))
            self.assertEqual(NODE.main([]), 1)
            self.assertEqual(self.loads(), [])
            self.daemon_process(pid)
            self.assertEqual(NODE.main(["--render"]), 0, self.errors.getvalue())
            self.assertEqual(NODE.main([]), 0, self.errors.getvalue())
            self.assertEqual(len(self.loads()), 1)

    def test_compile_and_reload_errors_are_fatal_without_activation_receipt(self):
        for failure in ("-Q", "-r"):
            self.host.parser_failure = failure
            self.assertEqual(NODE.main([]), 1)
            self.assertFalse((self.host.root / NODE.RECEIPT.lstrip("/")).exists())
        self.assertIn("parser rejected policy", self.errors.getvalue())


class AppArmorIntegrationTest(unittest.TestCase):
    def test_cloud_render_delivers_exact_backport_assets_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cloud.yaml"
            subprocess.run(["bash", "-euc", 'source "$1/lib/render.sh"; render_user_data "$2" fixture agent cloud server tskey-fixture ""', "render", str(PROJECT), str(output)], check=True, capture_output=True)
            entries = {entry["path"].lstrip("/"): entry for entry in yaml.safe_load(output.read_text())["write_files"]}
            for asset in ASSETS:
                entry = entries[asset]
                self.assertEqual(base64.b64decode(entry["content"]), (COMMON / asset).read_bytes())
                self.assertEqual(int(entry["permissions"], 8), CONFIG.ASSETS["/" + asset])
            self.assertFalse(any("/etc/apparmor.d/" in "/" + path for path in entries))

    def test_reconciler_check_does_not_activate_and_apply_only_starts_helper(self):
        for check in (True, False):
            reconciler = CONFIG.Reconciler("physical", "server", check=check)
            with mock.patch.object(reconciler, "command", return_value=CONFIG.Result()) as command, mock.patch.object(reconciler, "properties", return_value={"UnitFileState": "enabled"}):
                reconciler.apparmor()
            calls = [call.args[0] for call in command.call_args_list]
            if check:
                self.assertEqual(calls, [["/usr/local/bin/node-containerd-apparmor.py", "--check"]])
            else:
                self.assertEqual(calls, [["systemctl", "daemon-reload"], ["systemctl", "enable", "node-containerd-apparmor.service"], ["systemctl", "start", "node-containerd-apparmor.service"], ["/usr/local/bin/node-containerd-apparmor.py", "--check"]])
            self.assertEqual(reconciler.records, [{"item": "containerd-apparmor", "state": "match"}])


if __name__ == "__main__":
    unittest.main()
