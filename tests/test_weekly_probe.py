"""The requested week-long observation contract; no paid model calls."""

import json
from datetime import UTC, datetime, timedelta

from atr.usage_probe import report, start_week

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def codex_row(timestamp, total, last=None):
    return {
        "timestamp": timestamp.isoformat(),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": total, "last_token_usage": last or total},
        },
    }


def test_week_comparison_has_real_deltas_and_keeps_missing_cost_unknown(tmp_path):
    from atr.usage_probe import ingest_file

    home, root = tmp_path / "home", tmp_path / "sessions"
    path = root / "rollout.jsonl"
    write_rows(
        path,
        [
            codex_row(
                NOW - timedelta(days=1),
                {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10},
            ),
            codex_row(
                NOW + timedelta(hours=1),
                {"input_tokens": 180, "cached_input_tokens": 100, "output_tokens": 30},
            ),
            codex_row(
                NOW + timedelta(hours=1, minutes=1),
                {"input_tokens": 180, "cached_input_tokens": 100, "output_tokens": 30},
            ),
        ],
    )
    start_week(home, now=NOW, roots={"codex": [root], "claude": []})
    ingest_file(home, "codex", path)
    ingest_file(home, "codex", path)
    result = report(home, now=NOW + timedelta(days=7))
    client = result["clients"]["codex"]
    assert client["baseline"]["input_tokens"] == 100
    assert client["observed"]["input_tokens"] == 80
    assert client["observed"]["output_tokens"] == 20
    assert client["observed"]["cached_input_tokens"] == 40
    assert result["cost_savings_usd"] is None
    assert result["causal_savings_proven"] is False


def test_week_is_fixed_and_not_renewed_by_reinstall(tmp_path):
    first = start_week(tmp_path, now=NOW, roots={"codex": [], "claude": []})
    again = start_week(tmp_path, now=NOW + timedelta(days=1))
    assert again["start"] == first["start"] == NOW.isoformat()
    assert again["end"] == (NOW + timedelta(days=7)).isoformat()


def test_claude_message_dedup_cache_semantics_and_no_content_storage(tmp_path):
    import sqlite3

    from atr.usage_probe import ingest_file

    root, home = tmp_path / "projects", tmp_path / "home"
    path = root / "conversation.jsonl"

    def message(output):
        return {
            "timestamp": (NOW + timedelta(hours=1)).isoformat(),
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "model": "test-model",
                "content": [{"type": "text", "text": "PRIVATE-PROMPT-TEXT"}],
                "usage": {
                    "input_tokens": 20,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": output,
                },
            },
        }

    write_rows(path, [message(2), message(4), message(4)])
    start_week(home, now=NOW, roots={"codex": [], "claude": [root]})
    ingest_file(home, "claude", path)
    data = report(home)["clients"]["claude"]["observed"]
    assert data["input_tokens"] == 80 and data["output_tokens"] == 4
    assert data["uncached_input_tokens"] == 20
    with sqlite3.connect(home / "usage.sqlite3") as db:
        assert "PRIVATE-PROMPT-TEXT" not in "\n".join(db.iterdump())


def test_partial_append_is_not_lost_and_foreign_path_is_not_read(tmp_path):
    from atr.usage_probe import ingest_file

    home, root = tmp_path / "home", tmp_path / "sessions"
    root.mkdir()
    path = root / "one.jsonl"
    encoded = json.dumps(
        codex_row(NOW + timedelta(hours=1), {"input_tokens": 4, "output_tokens": 2})
    )
    path.write_text(encoded[:20])
    start_week(home, now=NOW, roots={"codex": [root], "claude": []})
    ingest_file(home, "codex", path)
    with path.open("a") as stream:
        stream.write(encoded[20:] + "\n")
    ingest_file(home, "codex", path)
    foreign = tmp_path / "private.jsonl"
    foreign.write_text(encoded + "\n")
    assert ingest_file(home, "codex", foreign)["status"] == "outside_allowed_roots"
    assert report(home)["clients"]["codex"]["observed"]["total_tokens"] == 6


def test_probe_stops_after_deadline_and_distinguishes_retries(tmp_path):
    from atr.usage_probe import record

    start_week(tmp_path, now=NOW, roots={"codex": [], "claude": []})
    for _ in range(2):
        record(
            tmp_path,
            "codex",
            "run",
            now=NOW + timedelta(days=1),
            run_id="same-run",
            status="succeeded",
            tool_id="native.json",
            duration_ms=5,
            input_bytes=20,
            output_bytes=10,
        )
    record(
        tmp_path, "codex", "run", now=NOW + timedelta(days=8), run_id="too-late", status="succeeded"
    )
    result = report(tmp_path, now=NOW + timedelta(days=7))
    assert result["atr"]["run_attempts"] == 2 and result["atr"]["distinct_runs"] == 1


def test_real_installed_hook_collects_usage_and_preserves_other_handlers(tmp_path):
    import subprocess

    from atr.client_install import install_clients
    from atr.native import NativeSession

    instant = datetime.now(UTC)
    home, root = tmp_path / "home", tmp_path / "transcripts"
    path = root / "one.jsonl"
    start_week(home, now=instant, roots={"codex": [root], "claude": []})
    with NativeSession(home):
        pass
    write_rows(
        path, [codex_row(instant + timedelta(seconds=1), {"input_tokens": 8, "output_tokens": 2})]
    )
    codex, claude = tmp_path / ".codex", tmp_path / ".claude"
    codex.mkdir()
    (codex / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-existing"}]}]}}
        )
    )
    install_clients(home, codex, claude, tmp_path / ".claude.json")
    install_clients(home, codex, claude, tmp_path / ".claude.json")
    config = json.loads((codex / "hooks.json").read_text())
    assert config["hooks"]["Stop"][0]["hooks"][0]["command"] == "keep-existing"
    event = {"prompt": "CSV 转 JSON", "transcript_path": str(path)}
    for name in ("UserPromptSubmit", "Stop"):
        command = next(
            h["command"]
            for group in config["hooks"][name]
            for h in group["hooks"]
            if "-m atr." in h["command"]
        )
        run = subprocess.run(
            command, input=json.dumps(event).encode(), capture_output=True, shell=True, timeout=10
        )
        assert run.returncode == 0, run.stderr
        assert isinstance(json.loads(run.stdout), dict)
    result = report(home)
    assert result["atr"]["hook_deliveries"] == 1 and result["atr"]["hint_matches"] == 1
    assert result["clients"]["codex"]["observed"]["total_tokens"] == 10


def test_real_mcp_probe_measures_calls_without_saving_payload(tmp_path):
    import asyncio
    import sqlite3
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = tmp_path / "home"
    start_week(home, roots={"codex": [], "claude": []})

    async def invoke():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "atr.mcp_server", "--home", str(home), "--client", "codex"],
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool("atr_search", {"query": "CSV"})
            for _ in range(2):
                value = await session.call_tool(
                    "atr_run",
                    {
                        "tool_id": "native.json",
                        "inputs": {"operation": "select", "value": "PRIVATE-PAYLOAD"},
                        "business_key": "one",
                    },
                )
                assert not value.is_error

    asyncio.run(asyncio.wait_for(invoke(), 30))
    result = report(home)["atr"]
    assert result["searches"] == 1 and result["search_hits"] == 1
    assert result["run_attempts"] == 2 and result["distinct_runs"] == 1
    assert result["by_client"]["codex"] == 2 and result["input_payload_bytes"] > 0
    with sqlite3.connect(home / "usage.sqlite3") as db:
        assert "PRIVATE-PAYLOAD" not in "\n".join(db.iterdump())


def test_oversized_tool_output_does_not_hide_later_usage(tmp_path):
    from atr.usage_probe import ingest_file

    home, root = tmp_path / "home", tmp_path / "sessions"
    path = root / "large.jsonl"
    write_rows(
        path,
        [
            {"timestamp": NOW.isoformat(), "tool_output": "x" * 4096},
            codex_row(NOW + timedelta(hours=1), {"input_tokens": 4, "output_tokens": 2}),
        ],
    )
    start_week(home, now=NOW, roots={"codex": [root], "claude": []})
    ingest_file(home, "codex", path, line_limit=1024)
    data = report(home)
    assert data["clients"]["codex"]["observed"]["total_tokens"] == 6
    assert data["coverage"]["oversized_lines"] == 1


def test_stop_removes_only_probe_hooks_and_keeps_atr_available(tmp_path):
    from atr.client_install import install_clients
    from atr.usage_probe import stop_week

    home, codex, claude = tmp_path / "home", tmp_path / ".codex", tmp_path / ".claude"
    start_week(home, now=NOW, roots={"codex": [], "claude": []})
    codex.mkdir()
    (codex / "hooks.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}})
    )
    install_clients(home, codex, claude, tmp_path / ".claude.json")
    stop_week(home, codex=codex, claude=claude)
    config = json.loads((codex / "hooks.json").read_text())
    assert config["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "keep"}]}]
    assert "-m atr.hook --home " in config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert json.loads((home / "probe.json").read_text())["enabled"] is False
    install_clients(home, codex, claude, tmp_path / ".claude.json")
    reinstalled = json.loads((codex / "hooks.json").read_text())
    assert reinstalled["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "keep"}]}]
