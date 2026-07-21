import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from lib import inventory as nodes_inventory

INVENTORY = {
    "init-a": {"platform": "physical", "role": "init"},
    "join-a": {"platform": "physical", "role": "join", "server": "init-a"},
    "agent-a": {"platform": "physical", "role": "agent", "server": "join-a"},
    "cloud-a": {"platform": "cloud", "role": "join", "server": "init-a"},
}


class InventoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        (self.project / "config").mkdir(parents=True)
        self.inventory = self.project / "config/nodes.json"
        self.write(INVENTORY)

    def write(self, inventory):
        self.inventory.write_text(json.dumps(inventory))

    def call(self, function, *args, inventory=None):
        errors = io.StringIO()
        with mock.patch.object(nodes_inventory, "NODES_INVENTORY", self.inventory if inventory is None else inventory), contextlib.redirect_stderr(errors):
            try:
                return 0, function(*args), errors.getvalue()
            except SystemExit as error:
                return error.code, None, errors.getvalue()

    def succeed(self, function, *args, inventory=None):
        code, value, stderr = self.call(function, *args, inventory=inventory)
        self.assertEqual(code, 0, stderr)
        return value

    def fail_with(self, function, *args):
        code, _, stderr = self.call(function, *args)
        self.assertNotEqual(code, 0)
        return stderr

    def test_repository_inventory_is_valid(self):
        self.assertEqual(nodes_inventory.NODES_INVENTORY, PROJECT / "config/nodes.json")
        self.succeed(nodes_inventory.inventory_validate, inventory=nodes_inventory.NODES_INVENTORY)
        self.assertEqual(self.succeed(nodes_inventory.inventory_field, "carolion", "server", inventory=nodes_inventory.NODES_INVENTORY), "a50passos")

    def test_fields_are_read_per_node(self):
        fields = "".join(self.succeed(nodes_inventory.inventory_field, "cloud-a", field) + "|" for field in ("platform", "role", "server"))
        self.assertEqual(fields, "cloud|join|init-a|")
        self.assertEqual("[" + self.succeed(nodes_inventory.inventory_field, "init-a", "server") + "]", "[]")

    def test_missing_node_fails(self):
        stderr = self.fail_with(nodes_inventory.inventory_field, "ghost", "role")
        self.assertIn("node 'ghost' is not in", stderr)

    def test_invalid_inventories_are_rejected(self):
        invalid = {
            "unknown platform": {"node-a": {"platform": "vm", "role": "init"}},
            "unknown role": {"node-a": {"platform": "cloud", "role": "worker"}},
            "missing server": {"init-a": INVENTORY["init-a"], "node-a": {"platform": "cloud", "role": "join"}},
            "server not in inventory": {"node-a": {"platform": "cloud", "role": "join", "server": "ghost"}},
            "agent server": {**INVENTORY, "node-a": {"platform": "cloud", "role": "join", "server": "agent-a"}},
            "self server": {"node-a": {"platform": "cloud", "role": "join", "server": "node-a"}},
            "init with server": {"init-a": {"platform": "physical", "role": "init", "server": "join-a"}, "join-a": INVENTORY["join-a"]},
            "extra key": {"init-a": {**INVENTORY["init-a"], "arch": "arm64"}},
            "invalid hostname": {"Node_A": {"platform": "cloud", "role": "init"}},
            "not an object": [],
        }
        for name, inventory in invalid.items():
            with self.subTest(name):
                self.write(inventory)
                stderr = self.fail_with(nodes_inventory.inventory_validate)
                self.assertIn("is invalid", stderr)
        self.inventory.write_text("{not json")
        self.fail_with(nodes_inventory.inventory_validate)

    def test_record_adds_new_nodes_and_rejects_conflicts(self):
        self.succeed(nodes_inventory.inventory_record, "cloud-b", "cloud", "join", "join-a")
        self.succeed(nodes_inventory.inventory_record, "init-b", "physical", "init", "")
        inventory = json.loads(self.inventory.read_text())
        self.assertEqual(inventory["cloud-b"], {"platform": "cloud", "role": "join", "server": "join-a"})
        self.assertEqual(inventory["init-b"], {"platform": "physical", "role": "init"})
        self.assertEqual(self.inventory.stat().st_mode & 0o777, 0o644)
        before = self.inventory.read_text()
        for args in (("cloud-a", "physical", "join", "init-a"), ("cloud-c", "cloud", "join", "agent-a"), ("cloud-c", "cloud", "join", "ghost")):
            with self.subTest(args=args):
                self.fail_with(nodes_inventory.inventory_record, *args)
        self.assertEqual(self.inventory.read_text(), before)
        self.succeed(nodes_inventory.inventory_check, "cloud-a", "cloud", "join", "init-a")

    def test_remove_refuses_referenced_servers(self):
        stderr = self.fail_with(nodes_inventory.inventory_remove, "join-a")
        self.assertIn("use 'join-a' as their server", stderr)
        self.succeed(nodes_inventory.inventory_remove, "cloud-a")
        self.assertNotIn("cloud-a", json.loads(self.inventory.read_text()))


if __name__ == "__main__":
    unittest.main()
