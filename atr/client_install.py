"""Idempotent user-client installation, preserving unrelated config and backups."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8-sig") == content:
        return
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(path, path.with_name(path.name + ".atr-backup-" + stamp))
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as output:
            temporary = output.name
            output.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("client config must be an object")
    return value


def _hook(config: dict[str, Any], command: str) -> dict[str, Any]:
    entries = config.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    # Only manage our exact module entry. Keep every unrelated handler/group.
    for group in entries:
        group["hooks"] = [
            h for h in group.get("hooks", []) if "-m atr.hook --home " not in h.get("command", "")
        ]
    entries[:] = [group for group in entries if group.get("hooks")]
    entries.append({"hooks": [{"type": "command", "command": command, "timeout": 3}]})
    return config


def _usage_hooks(config: dict[str, Any], home: Path, client: str) -> dict[str, Any]:
    if not (home / "probe.json").exists():
        return config
    if not _read(home / "probe.json").get("enabled", False):
        return config
    argv = [
        sys.executable,
        "-m",
        "atr.usage_probe",
        "--home",
        str(home),
        "--client",
        client,
        "hook",
    ]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else __import__("shlex").join(argv)
    for event in ("Stop", "SessionEnd", "SubagentStop"):
        groups = config.setdefault("hooks", {}).setdefault(event, [])
        for group in groups:
            group["hooks"] = [
                hook
                for hook in group.get("hooks", [])
                if "-m atr.usage_probe --home " not in hook.get("command", "")
            ]
        groups[:] = [group for group in groups if group.get("hooks")]
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
    return config


def install_clients(home: Path, codex: Path, claude: Path, claude_config: Path) -> list[str]:
    home = home.resolve()
    args = ["-m", "atr.mcp_server", "--home", str(home), "--client", "codex"]
    server = {"command": sys.executable, "args": [*args[:-1], "claude"]}
    command = (
        subprocess.list2cmdline([sys.executable, "-m", "atr.hook", "--home", str(home)])
        if os.name == "nt"
        else __import__("shlex").join([sys.executable, "-m", "atr.hook", "--home", str(home)])
    )
    config = codex / "config.toml"
    original = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    # TOML need not be reformatted; remove only the exact ATR server tables.
    pattern = r"(?ms)^\[mcp_servers\.atr(?:\.[^\]\r\n]+)?\][^\r\n]*\r?\n.*?(?=^\[|\Z)"
    cleaned = re.sub(pattern, "", original).rstrip()
    value = (
        cleaned
        + ("\n\n" if cleaned else "")
        + "[mcp_servers.atr]\n"
        + "command = "
        + json.dumps(sys.executable)
        + "\nargs = "
        + json.dumps(args)
        + "\nstartup_timeout_sec = 30\n"
    )
    parsed = tomllib.loads(value)
    before = tomllib.loads(original)
    before.get("mcp_servers", {}).pop("atr", None)
    preserved = dict(parsed)
    preserved["mcp_servers"] = dict(parsed["mcp_servers"])
    preserved["mcp_servers"].pop("atr")
    if not preserved["mcp_servers"] and "mcp_servers" not in before:
        preserved.pop("mcp_servers")
    if preserved != before:
        raise ValueError("cannot edit ATR TOML table while preserving other settings")
    # Parse everything before making changes; malformed input cannot cause a partial config write.
    codex_hooks = _usage_hooks(
        _hook(_read(codex / "hooks.json"), command + " --client codex"), home, "codex"
    )
    claude_hooks = _usage_hooks(
        _hook(_read(claude / "settings.json"), command + " --client claude"), home, "claude"
    )
    cc = _read(claude_config)
    cc.setdefault("mcpServers", {})["atr"] = server
    writes = {
        config: value,
        codex / "hooks.json": json.dumps(codex_hooks, ensure_ascii=False, indent=2) + "\n",
        claude / "settings.json": json.dumps(claude_hooks, ensure_ascii=False, indent=2) + "\n",
        claude_config: json.dumps(cc, ensure_ascii=False, indent=2) + "\n",
    }
    for path, content in writes.items():
        write_file(path, content)
    return [str(path) for path in writes]


def default_native_home() -> Path:
    return (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "atr" / "native"
    )


def main() -> int:
    from atr.native import NativeSession, register

    parser = argparse.ArgumentParser(
        description="Operator-only ATR native setup; no Docker image needed"
    )
    parser.add_argument("--home", type=Path, default=default_native_home())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install-clients")
    sub.add_parser("init")
    approve = sub.add_parser(
        "register", help="Trust operator-reviewed pure Python code, cases and call rule"
    )
    approve.add_argument("entry", type=Path)
    args = parser.parse_args()
    if args.command == "register":
        register(args.home, json.loads(args.entry.read_text(encoding="utf-8-sig")))
    with NativeSession(args.home):
        pass
    result: dict[str, Any] = {"home": str(args.home), "ready": True}
    if args.command == "install-clients":
        result["configured"] = install_clients(
            args.home, Path.home() / ".codex", Path.home() / ".claude", Path.home() / ".claude.json"
        )
        result["activation"] = (
            "Restart/reload MCP in both clients. Review the new Codex hook with /hooks."
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
