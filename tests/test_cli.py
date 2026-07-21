import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from lib import commands

SPEC = importlib.util.spec_from_loader("nodes_cli", loader=None)
CLI = importlib.util.module_from_spec(SPEC)
exec(compile((PROJECT / "nodes").read_text(), str(PROJECT / "nodes"), "exec"), CLI.__dict__)


class CliTest(unittest.TestCase):
    def test_help_does_not_require_external_tools(self):
        for arguments in (("help",), ("provision", "--help"), ("provision", "physical", "--help"),
                          ("provision", "cloud", "--help"), ("remove", "--help"), ("sync", "--help")):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(PROJECT / "nodes"), *arguments],
                                        env={"PATH": "/nonexistent"}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("Usage: nodes"))

    def test_guided_menu_dispatches_every_workflow(self):
        for choice, expected, answers in (("1", "flash_command", []), ("2", "up_command", []),
                                         ("3", "down_command", []), ("4", "sync_command", ["cloud-1 carolion"])):
            with self.subTest(choice=choice), contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(CLI, "prompt", side_effect=[choice, *answers]), \
                    mock.patch.object(CLI, expected) as dispatch:
                CLI.main([])
                dispatch.assert_called_once_with(["cloud-1", "carolion"] if choice == "4" else [])

    def test_missing_values_and_unknown_options_keep_errors(self):
        for parser in (commands.flash_command, commands.up_command, commands.down_command):
            for arguments, error in ((["--hostname"], "--hostname requires a value"),
                                     (["--bogus"], "unknown option '--bogus'")):
                with self.subTest(parser=parser.__name__, arguments=arguments), \
                        contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as failure:
                    parser(arguments)
                self.assertEqual(failure.exception.code, 1)
                self.assertIn(error, errors.getvalue())

    def test_cloud_creation_uses_private_rendered_file_and_records_after_success(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "user-data"
            def render(path, *arguments):
                events.append(("render", arguments))
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "w") as handle:
                    handle.write("secret user data")
            def run(arguments, **kwargs):
                events.append(("create", arguments))
                self.assertEqual(Path(arguments[-1]).stat().st_mode & 0o777, 0o600)
                self.assertNotIn("secret", " ".join(map(str, arguments)))
                return SimpleNamespace(returncode=0)
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(commands, "require_command"), \
                    mock.patch.object(commands, "node_options", return_value=("cloud-a", "join", "server-a")), \
                    mock.patch.object(commands, "load_tailscale_authkey", return_value="tskey-secret"), \
                    mock.patch.object(commands, "confirm"), \
                    mock.patch.object(commands, "work_directory", return_value=contextlib.nullcontext(directory)), \
                    mock.patch.object(commands, "render_user_data", side_effect=render), \
                    mock.patch.object(commands, "run", side_effect=run), \
                    mock.patch.object(commands, "inventory_record", side_effect=lambda *args: events.append(("record", args))), \
                    mock.patch.dict(os.environ, {"NODES_HCLOUD_TYPE": "cx99"}):
                commands.up_command(["--hostname", "cloud-a", "--role", "join", "--server", "server-a", "--type", "cx23", "--location", "nbg1", "--image", "ubuntu-24.04", "--yes"])
            self.assertEqual([event[0] for event in events], ["render", "create", "record"])
            self.assertEqual(events[0][1], ("cloud-a", "join", "cloud", "server-a", "tskey-secret", ""))
            self.assertEqual(events[1][1], ["hcloud", "server", "create", "--name", "cloud-a", "--type", "cx23", "--image", "ubuntu-24.04", "--location", "nbg1", "--user-data-from-file", output])
            self.assertEqual(events[2][1], ("cloud-a", "cloud", "join", "server-a"))


if __name__ == "__main__":
    unittest.main()
