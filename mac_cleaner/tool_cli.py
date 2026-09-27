"""Manage and launch external coding-agent CLI tools."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _tool_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "poofmac" / "tools.json"


def _load_cli_tools() -> dict[str, list[str]]:
    tools = {"copilot": ["copilot"], "codex": ["codex"]}
    try:
        saved = json.loads(_tool_config_path().read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            for name, command in saved.items():
                if (
                    isinstance(name, str)
                    and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name)
                    and name not in tools
                    and isinstance(command, list)
                    and command
                    and all(isinstance(part, str) and part for part in command)
                ):
                    tools[name] = command
    except (OSError, ValueError):
        pass
    return tools


def run_tools(argv: list[str]) -> int:
    """List, register, and launch tools directly without invoking a shell."""
    parser = argparse.ArgumentParser(
        prog="poofmac tools",
        description="Launch installed coding CLIs or register your own command.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="List built-in and registered tools")
    add_parser = subparsers.add_parser("add", help="Register a CLI command")
    add_parser.add_argument("name", help="Alias, e.g. aider")
    add_parser.add_argument("command", nargs=argparse.REMAINDER)
    remove_parser = subparsers.add_parser("remove", help="Remove a registered tool")
    remove_parser.add_argument("name")
    run_parser = subparsers.add_parser("run", help="Launch a registered CLI tool")
    run_parser.add_argument("name")
    run_parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    tools = _load_cli_tools()
    path = _tool_config_path()
    builtins = {"copilot", "codex"}

    if args.action == "list":
        print("PoofMac CLI tools")
        for name, command in sorted(tools.items()):
            executable = shutil.which(command[0])
            state = f"available: {executable}" if executable else "not found on PATH"
            print(f"  {name:<12} {state}")
        print("\nRun with: poofmac tools run <name> [arguments]")
        return 0

    if args.action == "add":
        name = args.name.strip().lower()
        command = list(args.command)
        if command and command[0] == "--":
            command.pop(0)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            parser.error("tool names must use lowercase letters, digits, '-' or '_'")
        if name in builtins:
            parser.error(f"{name!r} is built in and cannot be replaced")
        if name in tools:
            parser.error(f"{name!r} is already registered; remove it first to replace it")
        if not command:
            parser.error("provide an executable, e.g. poofmac tools add aider aider")
        saved = {key: value for key, value in tools.items() if key not in builtins}
        saved[name] = command
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        print(f"Registered {name}: {' '.join(command)}")
        return 0

    if args.action == "remove":
        name = args.name.strip().lower()
        if name in builtins:
            parser.error(f"{name!r} is built in and cannot be removed")
        if name not in tools:
            parser.error(f"unknown registered tool: {name}")
        saved = {key: value for key, value in tools.items() if key not in builtins and key != name}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        print(f"Removed {name}")
        return 0

    if args.name not in tools:
        parser.error(f"unknown tool {args.name!r}; use 'poofmac tools list'")
    extra = list(args.args)
    if extra and extra[0] == "--":
        extra.pop(0)
    try:
        return subprocess.run([*tools[args.name], *extra], check=False).returncode
    except FileNotFoundError:
        print(
            f"Could not find executable {tools[args.name][0]!r}. "
            "Install it or register its full path.",
            file=sys.stderr,
        )
        return 127
