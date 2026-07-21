import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]


def die(message):
    print("Error: " + message, file=sys.stderr)
    raise SystemExit(1)


def require_command(name, hint=""):
    if not shutil.which(name):
        die(name + " not found" + (" (" + hint + ")" if hint else ""))


def validate_hostname(value):
    if len(value) > 63 or not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", value):
        die("invalid hostname '" + value + "'")


def prompt(message, secret=False):
    if sys.stdin.isatty():
        print(message, end="", file=sys.stderr, flush=True)
    if secret and sys.stdin.isatty():
        import termios
        previous = termios.tcgetattr(sys.stdin)
        hidden = termios.tcgetattr(sys.stdin)
        hidden[3] &= ~termios.ECHO
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, hidden)
            answer = sys.stdin.readline()
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, previous)
    else:
        answer = sys.stdin.readline()
    if not answer or not answer.endswith("\n"):
        raise SystemExit(1)
    return answer.rstrip("\n").strip(" \t")


def confirm(message, assume_yes=False):
    if not assume_yes and prompt(message + " [y/N]: ") not in ("y", "Y"):
        print("Aborted.")
        raise SystemExit(1)


def run(arguments, check=True, **kwargs):
    try:
        result = subprocess.run([str(arg) for arg in arguments], **kwargs)
    except FileNotFoundError:
        print(str(arguments[0]) + ": command not found", file=sys.stderr)
        raise SystemExit(127)
    if check and result.returncode:
        raise SystemExit(result.returncode if result.returncode > 0 else 128 - result.returncode)
    return result


def capture(arguments, check=True, **kwargs):
    return run(arguments, check=check, stdout=subprocess.PIPE, text=True, **kwargs)


def pass_value(path):
    try:
        result = subprocess.run(["pass", "show", path], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return ""
    return os.fsdecode(result.stdout).split("\n", 1)[0] if not result.returncode else ""


def yaml_quote(value):
    if any(char in value for char in "\n\r\t"):
        die("YAML values cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ssh_node(arguments, **kwargs):
    config = os.environ.get("NODES_SSH_CONFIG", "")
    options = []
    if config:
        if not Path(config).is_file() or not os.access(config, os.R_OK):
            die("NODES_SSH_CONFIG must name a readable regular file")
        options = ["-F", config]
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o",
                "ConnectionAttempts=1", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                "-o", "StrictHostKeyChecking=" + ("yes" if config else "accept-new"), "-o",
                "UpdateHostKeys=no", *options, *arguments], **kwargs)
