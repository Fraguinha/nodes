import base64
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re

from .common import PROJECT, die, validate_hostname, yaml_quote

NODES_PROJECT_DIR = PROJECT


def json_text(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(map(json_text, value)) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json_text(key) + ":" + json_text(item) for key, item in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("\x7f", "\\u007f")


def shell_quote(value):
    if not value:
        return "''"
    if any(not char.isprintable() for char in value):
        escapes = {"\a": "\\a", "\b": "\\b", "\x1b": "\\E", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\v": "\\v", "'": "\\'", "\\": "\\\\"}
        quoted = ""
        for char in value:
            if char in escapes:
                quoted += escapes[char]
            elif char.isprintable():
                quoted += char
            else:
                quoted += "".join("\\%03o" % byte for byte in char.encode())
        return "$'" + quoted + "'"
    quoted = "".join("\\" + char if char in " \t\n\\\"'`$&;|<>(){}[]*?!^," else char for char in value)
    if value.startswith("#"):
        quoted = "\\" + quoted
    return quoted


def read_policy():
    path = Path(NODES_PROJECT_DIR) / "rootfs/common/usr/local/share/nodes/node-policy.json"
    if path.is_symlink() or not path.is_file():
        die("canonical node policy is missing or not a regular file")
    try:
        return json.loads(path.read_text(), parse_float=Decimal)
    except (ValueError, OSError):
        die("invalid canonical node policy")


def validate_node_policy():
    policy = read_policy()
    try:
        pi, network = policy["users"]["pi"], policy["network"]
        ethernet, wifi, apt = network["ethernet"], network["wifi"], policy["apt"]
        def string_array(value):
            return isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value)
        def profile_keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if re.search(r"^(access-points|password|auth|ssid|psk|macaddress)$", key) or not profile_keys(item):
                        return False
            elif isinstance(value, list):
                return all(profile_keys(item) for item in value)
            return True
        valid = (
            type(policy["version"]) in (int, float, Decimal) and policy["version"] == 1
            and policy["root"] == {"password": "!", "expire": False}
            and type(policy["root"]["expire"]) is bool
            and policy["swap"] == {"size": 0} and type(policy["swap"]["size"]) is not bool
            and all(isinstance(item, str) for item in (policy["timezone"], pi["shell"], pi["sudo"], ethernet["id"], ethernet["match_name"], wifi["interface"], wifi["key_management"]))
            and string_array(pi["groups"])
            and all(string_array(policy["packages"][group]) for group in ("common", "physical", "nvidia"))
            and all(type(apt[key]) is bool for key in ("package_update", "package_upgrade"))
            and isinstance(apt["conf"], dict) and bool(apt["conf"]) and all(isinstance(item, str) for item in apt["conf"].values())
            and type(network["version"]) in (int, float, Decimal) and network["version"] == 2
            and isinstance(network["profiles"], dict) and profile_keys(network["profiles"])
            and all(type(item) is bool for item in (ethernet["dhcp4"], ethernet["optional"], wifi["dhcp4"], wifi["optional"]))
            and all(type(item) in (int, float, Decimal) for item in (ethernet["route_metric"], wifi["route_metric"]))
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        die("invalid canonical node policy")
    return policy


def entry(target, mode, content):
    return ("  - path: " + target + '\n    permissions: "' + mode + '"\n    owner: root:root\n'
            + "    encoding: b64\n    content: " + base64.b64encode(content).decode() + "\n")


def render_tree(tree):
    tree = Path(tree)
    return "".join(entry("/" + str(path.relative_to(tree)), "0755" if os.access(path, os.X_OK) else "0644", path.read_bytes())
                   for path in sorted(tree.rglob("*"), key=lambda path: os.fsencode(path))
                   if path.is_file() and not path.is_symlink() and "__pycache__" not in path.relative_to(tree).parts)


def render_write_files(platform, role, config):
    root = Path(NODES_PROJECT_DIR) / "rootfs"
    text = render_tree(root / "common")
    cloud_config = "datasource_list: [NoCloud, None]\n"
    if platform == "physical":
        text += render_tree(root / "physical")
    else:
        cloud_config += "network: {config: disabled}\n"
    text += entry("/etc/cloud/cloud.cfg.d/99-nodes.cfg", "0644", cloud_config.encode())
    if role != "agent":
        text += entry("/etc/rancher/k3s/config.yaml.d/20-server-ha.yaml", "0644", (root / "common/usr/local/share/nodes/k3s-server-ha.yaml").read_bytes())
    return text + entry("/etc/nodes/config", "0600", config.encode())


def write_output(output, text):
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def render_user_data(output, node_hostname, role, platform, server_hostname, tailscale_authkey, github_token, mode="provision"):
    validate_hostname(node_hostname)
    if role not in ("init", "join", "agent"):
        die("role must be 'init', 'join' or 'agent'")
    if platform not in ("physical", "cloud"):
        die("platform must be 'physical' or 'cloud'")
    if not tailscale_authkey:
        die("Tailscale auth key is required")
    if mode not in ("provision", "resync"):
        die("render mode must be 'provision' or 'resync'")
    if role == "init":
        if not github_token:
            die("GitHub token is required for the init role")
    else:
        validate_hostname(server_hostname)
    policy = validate_node_policy()
    template = Path(NODES_PROJECT_DIR) / "cloud-init/user-data.yaml"
    if not template.is_file():
        die(str(template) + " not found")
    config = "".join(key + "=" + shell_quote(value) + "\n" for key, value in zip(
        ("NODE_HOSTNAME", "NODE_ROLE", "NODE_PLATFORM", "SERVER_HOSTNAME", "TAILSCALE_AUTHKEY", "GITHUB_TOKEN"),
        (node_hostname, role, platform, server_hostname, tailscale_authkey, github_token)))
    keys_path = Path(NODES_PROJECT_DIR) / "config/authorized_keys"
    if not keys_path.is_file() or not keys_path.stat().st_size:
        die(str(keys_path) + " is empty or missing")
    pi, apt = policy["users"]["pi"], policy["apt"]
    replacements = {
        "timezone: __TIMEZONE__": "timezone: " + yaml_quote(policy["timezone"]) + "\n",
        "    groups: __PI_GROUPS__": "    groups: " + json_text(pi["groups"]) + "\n",
        "    shell: __PI_SHELL__": "    shell: " + yaml_quote(pi["shell"]) + "\n",
        "    sudo: __PI_SUDO__": "    sudo: " + yaml_quote(pi["sudo"]) + "\n",
        "  expire: __ROOT_EXPIRE__": "  expire: " + json_text(policy["root"]["expire"]) + "\n",
        "      password: __ROOT_PASSWORD__": "      password: " + yaml_quote(policy["root"]["password"]) + "\n",
        "package_update: __PACKAGE_UPDATE__": "package_update: " + json_text(apt["package_update"]) + "\n",
        "package_upgrade: __PACKAGE_UPGRADE__": "package_upgrade: " + json_text(False if mode == "resync" else apt["package_upgrade"]) + "\n",
        "  preserve_sources_list: __PRESERVE_SOURCES_LIST__": "  preserve_sources_list: " + json_text(mode == "resync") + "\n",
        "__APT_CONF__": "".join("    " + key + " " + json_text(value) + ";\n" for key, value in apt["conf"].items()),
        "  size: __SWAP_SIZE__": "  size: " + json_text(policy["swap"]["size"]) + "\n",
        "hostname: __HOSTNAME__": "hostname: " + yaml_quote(node_hostname) + "\n",
        "__SSH_AUTHORIZED_KEYS__": "".join("      - " + yaml_quote(key) + "\n" for key in keys_path.read_text().split("\n") if key),
        "__PACKAGES__": "".join("  - " + package + "\n" for package in policy["packages"]["common"] + (policy["packages"]["physical"] if platform == "physical" else [])),
        "__WRITE_FILES__": render_write_files(platform, role, config),
    }
    write_output(output, "".join(replacements.get(line, line + "\n") for line in template.read_text().splitlines()))


def new_instance_id(node_hostname):
    validate_hostname(node_hostname)
    return node_hostname + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def render_meta_data(output, node_hostname, instance_id):
    validate_hostname(node_hostname)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", instance_id):
        die("invalid instance-id '" + instance_id + "'")
    write_output(output, "instance-id: " + instance_id + "\nlocal-hostname: " + node_hostname + "\n")


def network_profile(node_hostname, policy=None):
    network = (policy or validate_node_policy())["network"]
    profile = network["profiles"].get(node_hostname)
    if profile is not None and profile is not False:
        return profile
    ethernet, wifi = network["ethernet"], network["wifi"]
    return {"ethernets": {ethernet["id"]: {"match": {"name": ethernet["match_name"]}, "dhcp4": ethernet["dhcp4"], "optional": ethernet["optional"], "dhcp4-overrides": {"route-metric": ethernet["route_metric"]}}},
            "wifis": {wifi["interface"]: {"dhcp4": wifi["dhcp4"], "optional": wifi["optional"], "dhcp4-overrides": {"route-metric": wifi["route_metric"]}}}}


def network_profile_has_wifis(node_hostname):
    validate_hostname(node_hostname)
    return bool(network_profile(node_hostname).get("wifis"))


def render_network_config(output, wifi_ssid, wifi_password, node_hostname):
    validate_hostname(node_hostname)
    policy = validate_node_policy()
    profile = network_profile(node_hostname, policy)
    has_wifis = bool(profile.get("wifis"))
    if has_wifis:
        if not wifi_ssid or len(wifi_ssid) > 32:
            die("Wi-Fi SSID must be between 1 and 32 characters")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", wifi_password) and not 8 <= len(wifi_password) <= 63:
            die("Wi-Fi password must be 8-63 characters or a 64-digit PSK")
    lines = []
    for line in (Path(NODES_PROJECT_DIR) / "cloud-init/network.yaml").read_text().splitlines():
        if line == "  version: __NETWORK_VERSION__":
            line = "  version: " + json_text(policy["network"]["version"])
        elif line == "  ethernets: __ETHERNET_CONFIG__":
            line = "  ethernets: " + json_text(profile.get("ethernets"))
        elif line == "  wifis:":
            if not has_wifis:
                break
        elif line == "      __WIFI_FIELDS__: {}":
            lines.extend("      " + key + ": " + json_text(value) + "\n" for key, value in profile["wifis"]["wlan0"].items())
            continue
        elif line == "            key-management: __WIFI_KEY_MANAGEMENT__":
            line = "            key-management: " + policy["network"]["wifi"]["key_management"]
        elif "__WIFI_SSID__:" in line:
            line = "        " + yaml_quote(wifi_ssid) + ":"
        elif "password: __WIFI_PASSWORD__" in line:
            line = "            password: " + yaml_quote(wifi_password)
        lines.append(line + "\n")
    write_output(output, "".join(lines))
