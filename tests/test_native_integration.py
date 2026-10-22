"""Consumer contract for the requested image-free client integration."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from atr.native import NativeSession


def test_find_reusable_tool_by_user_intent(tmp_path: Path) -> None:
    with NativeSession(tmp_path / "control") as session:
        hits = session.search("CSV 转 JSON")
        assert hits and hits[0]["tool_id"] == "native.csv"


@pytest.mark.parametrize(
    "tool,inputs,expected",
    [
        ("native.json", {"operation": "select", "value": {"rows": [3, 7]}, "path": ["rows", 1]}, 7),
        (
            "native.csv",
            {"operation": "parse", "text": 'name,note\n甲,"x,y"\n'},
            [{"name": "甲", "note": "x,y"}],
        ),
        ("native.text", {"operation": "unique_lines", "text": "b\na\nb\n"}, ["b", "a"]),
    ],
)
def test_native_transform_and_idempotent_business_result(tmp_path, tool, inputs, expected):
    with NativeSession(tmp_path / "control") as session:
        result = session.run(tool, inputs, "consumer-operation")
        assert result["status"] == "succeeded", result
        assert result["output"]["value"] == expected
        again = session.run(tool, inputs, "consumer-operation")
        assert again["run_id"] == result["run_id"]
        changed = session.run(tool, {**inputs, "unexpected": True}, "consumer-operation")
        assert changed["status"] != "succeeded"


def test_model_cannot_execute_unregistered_program_or_choose_task(tmp_path):
    with NativeSession(tmp_path / "control") as session:
        result = session.run("untrusted.tool", {"root_task_id": "new-budget"}, "attack")
        assert result["error"] == "native_not_approved"
        assert (
            session.run(
                "native.json", {"operation": "select", "value": {}, "path": ["absent"]}, "invalid"
            )["status"]
            != "succeeded"
        )


def test_hook_only_injects_matching_approved_tool_hint(tmp_path):
    from atr.client_install import install_clients
    from atr.hook import hint

    with NativeSession(tmp_path / "control"):
        pass
    assert hint({"prompt": "帮我写首诗"}, tmp_path / "control") == {}
    response = hint({"prompt": "CSV 转 JSON"}, tmp_path / "control")
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "native.csv" in context and len(context) <= 700
    codex, claude = tmp_path / ".codex", tmp_path / ".claude"
    codex.mkdir()
    claude.mkdir()
    original = '[mcp_servers.other]\ncommand="keep"\n'
    (codex / "config.toml").write_text(original)
    existing_hook = {
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "keep-hook"}]}]}
    }
    (codex / "hooks.json").write_text(json.dumps(existing_hook))
    (claude / "settings.json").write_text(json.dumps({"env": {"KEY": "preserve"}}))
    cc_config = tmp_path / ".claude.json"
    cc_config.write_text(json.dumps({"mcpServers": {"other": {"command": "keep"}}, "misc": 123}))
    install_clients(tmp_path / "control", codex, claude, cc_config)
    first = (codex / "config.toml").read_text()
    install_clients(tmp_path / "control", codex, claude, cc_config)
    assert (codex / "config.toml").read_text() == first
    import tomllib

    assert tomllib.loads(first)["mcp_servers"]["other"]["command"] == "keep"
    assert (
        json.loads((codex / "hooks.json").read_text())["hooks"]["UserPromptSubmit"][0]
        == existing_hook["hooks"]["UserPromptSubmit"][0]
    )
    assert json.loads((claude / "settings.json").read_text())["env"]["KEY"] == "preserve"
    assert json.loads(cc_config.read_text())["misc"] == 123
    assert json.loads(cc_config.read_text())["mcpServers"]["atr"]["command"] == sys.executable


def test_stdio_mcp_calls_real_native_transform(tmp_path):
    """JSON-RPC client asserts the actual output, not just server startup."""
    process = subprocess.Popen(
        [sys.executable, "-m", "atr.mcp_server", "--home", str(tmp_path / "control")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:

        def call(identifier, method, params):
            process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
                + "\n"
            )
            process.stdin.flush()
            while True:
                line = process.stdout.readline()
                assert line, process.stderr.read()
                result = json.loads(line)
                if result.get("id") == identifier:
                    return result

        call(
            1,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "atr-consumer-test", "version": "1"},
            },
        )
        process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.flush()
        result = call(
            2,
            "tools/call",
            {
                "name": "atr_run",
                "arguments": {
                    "tool_id": "native.text",
                    "inputs": {"operation": "unique_lines", "text": "x\nx\ny"},
                    "business_key": "mcp-transform",
                },
            },
        )
        payload = result["result"]
        assert not payload.get("isError"), payload
        value = payload["structuredContent"]
        value = value.get("result", value)
        assert value["output"]["value"] == ["x", "y"]
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_operator_registers_custom_tool_with_independent_acceptance(tmp_path):
    from atr.native import register

    home = tmp_path / "control"
    entry = {
        "program": {
            "manifest": {
                "id": "custom.sum",
                "scope": "global",
                "description": "Sum inline numbers",
            },
            "files": {
                "main.py": "import json,sys\nx=json.load(sys.stdin)\njson.dump({'sum':sum(x['numbers'])},sys.stdout)"
            },
        },
        "cases": [{"inputs": {"numbers": [4, 6]}, "rule": {"checks": [{"expected": {"sum": 10}}]}}],
        "call_rule": {"checks": [{"expected_path": "sum", "expected": 10}]},
    }
    register(home, entry)
    with NativeSession(home) as session:
        result = session.run("custom.sum", {"numbers": [8, 2]}, "sum-operation")
        assert result["status"] == "succeeded", result
        assert result["output"] == {"sum": 10}
        rejected = session.run("custom.sum", {"numbers": [1, 2]}, "different-operation")
        assert rejected["error"] == "behavior_contract"


def test_native_execution_requires_exact_operator_bundle_and_enforces_timeout(tmp_path):
    import threading

    from atr.errors import ATRError
    from atr.models import Limits, Manifest, Permissions, Program
    from atr.policy import Policy
    from atr.runtime.native import NativeSandbox
    from atr.runtime.sandbox import SandboxSpec

    sandbox = NativeSandbox()
    program = Program(
        manifest=Manifest(id="custom.wait", scope="global", description="Trusted wait"),
        files={"main.py": "import time\ntime.sleep(60)"},
    )
    spec = SandboxSpec(
        program.manifest.entrypoint,
        b"{}",
        program.sandbox_files(),
        tmp_path,
        Permissions(),
        Limits(timeout_seconds=0.15),
        Policy(),
        65536,
    )
    with pytest.raises(ATRError, match="operator"):
        sandbox.run(spec, threading.Event())
    sandbox.approve(program)
    outcome = sandbox.run(spec, threading.Event())
    assert outcome.status == "timed_out"
    flag = threading.Event()
    flag.set()
    assert sandbox.run(spec, flag).status == "cancelled"


def test_failed_operator_acceptance_does_not_install_broken_tool(tmp_path):
    from atr.errors import ATRError
    from atr.native import register

    home = tmp_path / "control"
    entry = {
        "program": {
            "manifest": {"id": "custom.wrong", "scope": "global", "description": "Wrong answer"},
            "files": {"main.py": "print('1')"},
        },
        "cases": [{"rule": {"checks": [{"expected": 2}]}}],
        "call_rule": {"checks": [{"expected": 2}]},
    }
    with pytest.raises(ATRError, match="acceptance"):
        register(home, entry)
    with NativeSession(home) as session:
        result = session.run(
            "native.text", {"operation": "unique_lines", "text": "a\na"}, "still-usable"
        )
        assert result["output"]["value"] == ["a"]


def test_two_clients_can_admit_shared_native_catalog_together(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def client(index):
        barrier.wait()
        with NativeSession(tmp_path / "control") as session:
            return session.run(
                "native.json", {"operation": "keys", "value": {"z": 1}}, f"client-{index}"
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(client, range(2)))
    assert [r["output"]["value"] for r in results] == [["z"], ["z"]]


def test_native_session_closes_after_task_lease_expires(tmp_path):
    from datetime import timedelta

    session = NativeSession(tmp_path / "control")
    later = session.runtime.clock() + timedelta(days=2)
    session.runtime.control.clock = lambda: later
    session.close()
    assert session.runtime.closed
