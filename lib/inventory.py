import json
import os
from pathlib import Path
import re
import tempfile

from .common import PROJECT, die, validate_hostname

NODES_INVENTORY = PROJECT / "config/nodes.json"


def valid_host(value):
    return isinstance(value, str) and re.search(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", value) is not None


def valid_inventory(nodes):
    if not isinstance(nodes, dict):
        return False
    for host, node in nodes.items():
        if not valid_host(host) or not isinstance(node, dict) or set(node) - {"platform", "role", "server"}:
            return False
        if node.get("platform") not in ("physical", "cloud") or node.get("role") not in ("init", "join", "agent"):
            return False
        if node["role"] == "init":
            if "server" in node:
                return False
        elif not valid_host(node.get("server")) or node["server"] == host:
            return False
    return all(node["server"] in nodes and nodes[node["server"]]["role"] != "agent"
               for node in nodes.values() if node["role"] != "init")


def inventory_validate():
    path = Path(NODES_INVENTORY)
    if path.is_symlink() or not path.is_file():
        raise SystemExit(1)
    try:
        nodes = json.loads(path.read_text())
        if not valid_inventory(nodes):
            raise ValueError()
    except (ValueError, OSError):
        die(str(path) + " is invalid")
    return nodes


def inventory_has(host):
    return host in inventory_validate()


def inventory_require(host):
    validate_hostname(host)
    if not inventory_has(host):
        die("node '" + host + "' is not in " + str(NODES_INVENTORY))


def inventory_field(host, field):
    inventory_require(host)
    return inventory_validate()[host].get(field, "")


def inventory_entry(platform, role, server):
    entry = {"platform": platform, "role": role}
    if role != "init":
        entry["server"] = server
    return entry


def inventory_check(host, platform, role, server):
    validate_hostname(host)
    nodes = inventory_validate()
    entry = inventory_entry(platform, role, server)
    if host in nodes and nodes[host] != entry:
        die("node '" + host + "' is recorded differently in " + str(NODES_INVENTORY))
    nodes[host] = entry
    if not valid_inventory(nodes):
        die("node '" + host + "' would make " + str(NODES_INVENTORY) + " invalid")


def inventory_write(nodes):
    descriptor, temporary = tempfile.mkstemp(prefix=Path(NODES_INVENTORY).name + ".", dir=Path(NODES_INVENTORY).parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(nodes, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, NODES_INVENTORY)
    finally:
        Path(temporary).unlink(missing_ok=True)


def inventory_record(host, platform, role, server):
    inventory_check(host, platform, role, server)
    nodes = inventory_validate()
    nodes[host] = inventory_entry(platform, role, server)
    inventory_write(nodes)


def inventory_check_removal(host):
    if any(node.get("server") == host for node in inventory_validate().values()):
        die("other nodes in " + str(NODES_INVENTORY) + " use '" + host + "' as their server")


def inventory_remove(host):
    inventory_check_removal(host)
    nodes = inventory_validate()
    nodes.pop(host, None)
    inventory_write(nodes)
