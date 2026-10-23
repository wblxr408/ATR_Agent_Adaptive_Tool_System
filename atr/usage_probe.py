"""Opt-in numeric telemetry. Conversation text is parsed transiently, never saved."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
ATR_NAMES = {
    "mcp__atr__atr_run",
    "mcp__atr__atr_search",
    "atr_run",
    "atr_search",
    "mcp__atr.atr_run",
    "mcp__atr.atr_search",
}


def _config(home: Path) -> dict[str, Any]:
    return json.loads((home / "probe.json").read_text(encoding="utf-8"))


def _db(home: Path) -> sqlite3.Connection:
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "usage.sqlite3", timeout=0.3)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS samples (
      source TEXT, item TEXT, client TEXT, timestamp TEXT, model TEXT,
      input_tokens INTEGER, cached_input_tokens INTEGER, cache_write_input_tokens INTEGER,
      output_tokens INTEGER, reasoning_output_tokens INTEGER, known INTEGER,
      PRIMARY KEY(source,item));
    CREATE TABLE IF NOT EXISTS cursors (
      source TEXT PRIMARY KEY, offset INTEGER, state TEXT, atr_seen INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS probe_events (
      id TEXT PRIMARY KEY, timestamp TEXT, client TEXT, kind TEXT,
      tool TEXT, status TEXT, run_id TEXT, duration_ms REAL,
      input_bytes INTEGER, output_bytes INTEGER, context_chars INTEGER, matched INTEGER);
    """)
    return db


def start_week(
    home: Path, *, now: datetime | None = None, roots: dict[str, list[Path]] | None = None
) -> dict[str, Any]:
    from atr.client_install import write_file

    if (home / "probe.json").exists():
        return _config(home)  # Reinstall/reconnect does not extend an enrollment.
    instant = now or datetime.now(UTC)
    paths = (
        roots
        if roots is not None
        else {
            "codex": [Path.home() / ".codex/sessions", Path.home() / ".codex/archived_sessions"],
            "claude": [Path.home() / ".claude/projects"],
        }
    )
    config = {
        "enabled": True,
        "start": instant.isoformat(),
        "end": (instant + timedelta(days=7)).isoformat(),
        "baseline_start": (instant - timedelta(days=7)).isoformat(),
        "roots": {client: [str(p.resolve()) for p in items] for client, items in paths.items()},
    }
    write_file(home / "probe.json", json.dumps(config, indent=2) + "\n")
    with closing(_db(home)):
        pass
    return config


def record(
    home: Path, client: str, kind: str, *, now: datetime | None = None, **metrics: Any
) -> None:
    """Whitelist numeric fields; telemetry failure must not fail the actual tool."""
    try:
        config = _config(home)
        instant = now or datetime.now(UTC)
        if not config["enabled"] or not datetime.fromisoformat(
            config["start"]
        ) <= instant < datetime.fromisoformat(config["end"]):
            return
        if client not in ("codex", "claude", "unknown") or kind not in (
            "hook",
            "search",
            "run",
            "stop",
        ):
            return

        def identifier(value: Any) -> str:
            return (
                value
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
                else ""
            )

        def number(key: str) -> int:
            value = metrics.get(key, 0)
            return min(value, 2**40) if type(value) is int and value >= 0 else 0

        duration = metrics.get("duration_ms", 0)
        duration = (
            min(float(duration), 3600000)
            if isinstance(duration, (int, float)) and 0 <= duration < float("inf")
            else 0
        )
        with closing(_db(home)) as db, db:
            db.execute(
                "INSERT INTO probe_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    instant.isoformat(),
                    client,
                    kind,
                    identifier(metrics.get("tool_id")),
                    identifier(metrics.get("status")),
                    identifier(metrics.get("run_id")),
                    duration,
                    number("input_bytes"),
                    number("output_bytes"),
                    number("context_chars"),
                    int(bool(metrics.get("matched", False))),
                ),
            )
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return


def _number(value: Any) -> int:
    return value if type(value) is int and 0 <= value < 2**50 else 0


def _model(value: Any) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", value)
        else "unknown"
    )


def ingest_file(
    home: Path,
    client: str,
    path: Path,
    *,
    budget: int = 4 * 1024 * 1024,
    line_limit: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    """Incrementally read allowed JSONL only. Partial final lines remain pending."""
    config = _config(home)
    resolved = path.resolve()
    if not config["enabled"]:
        return {"status": "disabled"}
    if path.suffix != ".jsonl" or not any(
        resolved.is_relative_to(Path(root).resolve()) for root in config["roots"].get(client, [])
    ):
        return {"status": "outside_allowed_roots"}
    source = client + ":" + hashlib.sha256(path.name.encode()).hexdigest()
    low, high = (
        datetime.fromisoformat(config["baseline_start"]),
        datetime.fromisoformat(config["end"]),
    )
    with closing(_db(home)) as db, db, resolved.open("rb") as stream:
        cursor = db.execute("SELECT * FROM cursors WHERE source=?", (source,)).fetchone()
        state: dict[str, Any] = (
            json.loads(cursor["state"])
            if cursor
            else {"model": "unknown", "previous": None, "epoch": 0}
        )
        offset = cursor["offset"] if cursor else 0
        atr_seen = cursor["atr_seen"] if cursor else 0
        if resolved.stat().st_size < offset:
            offset, state["previous"] = 0, None
            state["epoch"] += 1
        stream.seek(offset)
        begin, processed, malformed = offset, 0, 0
        while stream.tell() - begin < budget:
            position = stream.tell()
            if state.get("discarding_line"):
                block = stream.readline(65536)
                if not block:
                    break
                offset = stream.tell()
                if block.endswith(b"\n"):
                    state["discarding_line"] = False
                continue
            line = stream.readline(line_limit + 1)
            if len(line) > line_limit:
                state["oversized_lines"] = state.get("oversized_lines", 0) + 1
                state["discarding_line"] = not line.endswith(b"\n")
                offset = stream.tell()
                continue
            if not line or not line.endswith(b"\n"):
                break
            offset = stream.tell()
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("nonobject")
                stamp = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("missing timezone")
                timestamp = stamp.astimezone(UTC).isoformat()
                payload = row.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if client == "codex" and row.get("type") == "turn_context":
                    state["model"] = _model(payload.get("model"))
                if (
                    row.get("type") == "response_item"
                    and payload.get("type") == "function_call"
                    and payload.get("name") in ATR_NAMES
                ):
                    atr_seen = 1
                sample: dict[str, int] | None = None
                item, model = str(position), state["model"]
                if client == "codex" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    total = info.get("total_token_usage")
                    if not isinstance(total, dict):
                        continue
                    current = {key: _number(total.get(key)) for key in FIELDS}
                    previous = state["previous"]
                    reset = (
                        previous is None
                        or current["input_tokens"] < previous["input_tokens"]
                        or current["output_tokens"] < previous["output_tokens"]
                    )
                    if reset:
                        last = info.get("last_token_usage")
                        sample = (
                            {key: _number(last.get(key)) for key in FIELDS}
                            if isinstance(last, dict)
                            else current
                        )
                        if previous is None and sample != current:
                            state["unattributed_prefix"] = True
                    else:
                        sample = {key: max(0, current[key] - previous[key]) for key in FIELDS}
                    state["previous"] = current
                    if not any(sample.values()):
                        continue
                elif client == "claude" and row.get("type") == "assistant":
                    message = row.get("message") or {}
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    identity = message.get("id")
                    item = (
                        identity
                        if isinstance(identity, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity)
                        else str(position)
                    )
                    model = _model(message.get("model"))
                    cached = _number(usage.get("cache_read_input_tokens"))
                    created = _number(usage.get("cache_creation_input_tokens"))
                    details = usage.get("output_tokens_details") or {}
                    sample = {
                        "input_tokens": _number(usage.get("input_tokens")) + cached + created,
                        "cached_input_tokens": cached,
                        "cache_write_input_tokens": created,
                        "output_tokens": _number(usage.get("output_tokens")),
                        "reasoning_output_tokens": _number(details.get("thinking_tokens"))
                        if isinstance(details, dict)
                        else 0,
                    }
                    content = message.get("content") or []
                    if isinstance(content, list) and any(
                        isinstance(part, dict)
                        and part.get("type") == "tool_use"
                        and part.get("name") in ATR_NAMES
                        for part in content
                    ):
                        atr_seen = 1
                if sample is not None and low <= stamp < high:
                    db.execute(
                        """INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(source,item) DO UPDATE SET
                        input_tokens=MAX(input_tokens,excluded.input_tokens),
                        cached_input_tokens=MAX(cached_input_tokens,excluded.cached_input_tokens),
                        cache_write_input_tokens=MAX(cache_write_input_tokens,excluded.cache_write_input_tokens),
                        output_tokens=MAX(output_tokens,excluded.output_tokens),
                        reasoning_output_tokens=MAX(reasoning_output_tokens,excluded.reasoning_output_tokens)""",
                        (
                            source,
                            str(state["epoch"]) + ":" + item,
                            client,
                            timestamp,
                            model,
                            *(sample[key] for key in FIELDS),
                            int(any(sample.values())),
                        ),
                    )
                    processed += 1
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                malformed += 1
        state["malformed_lines"] = state.get("malformed_lines", 0) + malformed
        db.execute(
            "INSERT INTO cursors VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET offset=excluded.offset,state=excluded.state,atr_seen=excluded.atr_seen",
            (source, offset, json.dumps(state), atr_seen),
        )
    return {
        "status": "collected",
        "samples": processed,
        "pending_bytes": max(0, resolved.stat().st_size - offset),
    }


def reconcile(home: Path) -> dict[str, int]:
    config = _config(home)
    scanned = failed = pending = 0
    for client, roots in config["roots"].items():
        for root in roots:
            for path in Path(root).rglob("*.jsonl"):
                try:
                    if (
                        path.stat().st_mtime
                        < datetime.fromisoformat(config["baseline_start"]).timestamp()
                    ):
                        continue
                    result = ingest_file(home, client, path, budget=2**40)
                    scanned += 1
                    pending += result.get("pending_bytes", 0)
                except (OSError, ValueError, sqlite3.Error):
                    failed += 1
    return {"files_scanned": scanned, "files_failed": failed, "pending_bytes": pending}


def report(home: Path, *, now: datetime | None = None) -> dict[str, Any]:
    config = _config(home)
    with closing(_db(home)) as db:

        def usage(client: str, start: str, end: str) -> dict[str, Any]:
            row = db.execute(
                "SELECT COUNT(*) AS samples,COUNT(DISTINCT source) AS sessions,"
                + ",".join(f"COALESCE(SUM({key}),0) AS {key}" for key in FIELDS)
                + ",COALESCE(SUM(known=0),0) AS zero_usage_samples FROM samples WHERE client=? AND timestamp>=? AND timestamp<?",
                (client, start, end),
            ).fetchone()
            value = dict(row)
            value["total_tokens"] = value["input_tokens"] + value["output_tokens"]
            value["uncached_input_tokens"] = max(
                0,
                value["input_tokens"]
                - value["cached_input_tokens"]
                - value["cache_write_input_tokens"],
            )
            models = db.execute(
                "SELECT model,SUM(input_tokens)+SUM(output_tokens) AS total_tokens FROM samples WHERE client=? AND timestamp>=? AND timestamp<? GROUP BY model",
                (client, start, end),
            ).fetchall()
            value["models"] = [dict(m) for m in models]
            value["session_cohorts"] = [
                dict(item)
                for item in db.execute(
                    "SELECT c.atr_seen,COUNT(DISTINCT s.source) AS sessions,SUM(s.input_tokens)+SUM(s.output_tokens) AS total_tokens "
                    "FROM samples s JOIN cursors c ON s.source=c.source "
                    "WHERE s.client=? AND s.timestamp>=? AND s.timestamp<? GROUP BY c.atr_seen",
                    (client, start, end),
                )
            ]
            return value

        clients = {}
        for client in ("codex", "claude"):
            baseline = usage(client, config["baseline_start"], config["start"])
            observed = usage(client, config["start"], config["end"])
            clients[client] = {
                "baseline": baseline,
                "observed": observed,
                "token_change_percent": 100
                * (observed["total_tokens"] / baseline["total_tokens"] - 1)
                if baseline["total_tokens"]
                else None,
            }
        events = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM probe_events WHERE timestamp>=? AND timestamp<?",
                (config["start"], config["end"]),
            )
        ]
        runs = [e for e in events if e["kind"] == "run"]
        searches = [e for e in events if e["kind"] == "search"]
        hooks = [e for e in events if e["kind"] == "hook"]
        states = [json.loads(row["state"]) for row in db.execute("SELECT state FROM cursors")]
        return {
            "window": config,
            "complete": (now or datetime.now(UTC)) >= datetime.fromisoformat(config["end"]),
            "clients": clients,
            "atr": {
                "run_attempts": len(runs),
                "distinct_runs": len({e["run_id"] for e in runs if e["run_id"]}),
                "success_attempts": sum(e["status"] == "succeeded" for e in runs),
                "failed_attempts": sum(e["status"] == "failed" for e in runs),
                "searches": len(searches),
                "search_hits": sum(e["matched"] for e in searches),
                "hook_deliveries": len(hooks),
                "hint_matches": sum(e["matched"] for e in hooks),
                "hint_characters": sum(e["context_chars"] for e in hooks),
                "server_and_hook_duration_ms": sum(e["duration_ms"] for e in events),
                "input_payload_bytes": sum(e["input_bytes"] for e in events),
                "output_payload_bytes": sum(e["output_bytes"] for e in events),
                "by_client": {
                    client: sum(e["client"] == client for e in runs)
                    for client in ("codex", "claude", "unknown")
                },
            },
            "coverage": {
                "sources": len(states),
                "malformed_lines": sum(s.get("malformed_lines", 0) for s in states),
                "oversized_lines": sum(s.get("oversized_lines", 0) for s in states),
                "sources_with_unattributed_prefix": sum(
                    bool(s.get("unattributed_prefix")) for s in states
                ),
            },
            "cost_savings_usd": None,
            "causal_savings_proven": False,
            "limitations": [
                "Local usage logs are not provider bills; missing/zero counters do not prove free usage.",
                "Before/after workload and model differences prevent a causal savings claim.",
                "Hook characters and JSON payload bytes are not token measurements.",
                "No matched task quality or no-ATR counterfactual was measured.",
            ],
        }


def render(data: dict[str, Any]) -> str:
    lines = [
        "# ATR 一周使用量比较",
        "",
        f"窗口：{data['window']['start']} 至 {data['window']['end']}（UTC，结束不含）。",
        "状态：已满一周。"
        if data["complete"]
        else "状态：采集中，尚未满一周；当前总量与完整基线不宜直接比较。",
        "",
        "| 客户端 | 前7天 Token | 本周 Token | 本周会话 | 变化 |",
        "|---|---:|---:|---:|---:|",
    ]
    for client, item in data["clients"].items():
        change = item["token_change_percent"]
        change_text = (
            (f"{change:+.1f}%" if change is not None else "基线不足")
            if data["complete"]
            else "未满期"
        )
        lines.append(
            f"| {client} | {item['baseline']['total_tokens']} | {item['observed']['total_tokens']} | {item['observed']['sessions']} | {change_text} |"
        )
    a = data["atr"]
    lines.extend(
        [
            "",
            f"ATR尝试 {a['run_attempts']} 次，不同运行 {a['distinct_runs']} 次；成功尝试 {a['success_attempts']}，失败尝试 {a['failed_attempts']}。",
            f"检索 {a['searches']} 次、命中 {a['search_hits']}；提示hook {a['hook_deliveries']} 次、匹配 {a['hint_matches']}，注入 {a['hint_characters']} 字符。",
            f"服务方法与hook逻辑计时合计 {a['server_and_hook_duration_ms']:.1f}ms（不包含全部进程启动/客户端/模型时间）。",
            "",
            "费用节省：未知。以上是日志观测与前后变化，不证明ATR因果收益；缺失计数不记作免费。",
            "没有相同任务的无ATR对照或质量验收，不能把总Token下降当作净节省。",
            "",
            "数据只保存数值、模型与来源标识，不保存prompt/代码/工具入出参；日志格式变化、遗漏、部分行及供应商零计数均会降低覆盖。",
            "",
            "覆盖：" + json.dumps(data["coverage"], ensure_ascii=False),
        ]
    )
    return "\n".join(lines) + "\n"


def stop_week(
    home: Path, *, codex: Path | None = None, claude: Path | None = None
) -> dict[str, Any]:
    """Disable enrollment and remove only this home's dedicated collection hooks."""
    from atr.client_install import write_file

    config = _config(home)
    config["enabled"] = False
    write_file(home / "probe.json", json.dumps(config, indent=2) + "\n")
    for path in (
        (codex or Path.home() / ".codex") / "hooks.json",
        (claude or Path.home() / ".claude") / "settings.json",
    ):
        if not path.exists():
            continue
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        for event in ("Stop", "SessionEnd", "SubagentStop"):
            groups = value.get("hooks", {}).get(event, [])
            for group in groups:
                group["hooks"] = [
                    h
                    for h in group.get("hooks", [])
                    if not (
                        "-m atr.usage_probe --home " in h.get("command", "")
                        and str(home.resolve()) in h["command"]
                    )
                ]
            groups[:] = [g for g in groups if g.get("hooks")]
            if not groups:
                value.get("hooks", {}).pop(event, None)
        write_file(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return config


def main() -> int:
    from atr.client_install import default_native_home, write_file

    parser = argparse.ArgumentParser(description="Bounded local ATR usage observation")
    parser.add_argument("--home", type=Path, default=default_native_home())
    parser.add_argument("--client", choices=["codex", "claude", "unknown"], default="unknown")
    parser.add_argument("command", choices=["start", "report", "status", "stop", "hook"])
    args = parser.parse_args()
    if args.command == "start":
        value = start_week(args.home)
        value = {**value, "scan": reconcile(args.home)}
    elif args.command == "stop":
        value = stop_week(args.home)
    elif args.command == "status":
        value = _config(args.home)
    elif args.command == "hook":
        try:
            config = _config(args.home)
            if not config["enabled"] or datetime.now(UTC) >= datetime.fromisoformat(config["end"]):
                print("{}")
                return 0
            event = json.loads(sys.stdin.buffer.read(262145))
            if isinstance(event, dict):
                for key in ("transcript_path", "agent_transcript_path"):
                    if isinstance(event.get(key), str):
                        ingest_file(args.home, args.client, Path(event[key]))
                record(args.home, args.client, "stop")
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            pass
        value = {}
    else:
        scan = reconcile(args.home)
        value = report(args.home)
        value["coverage"].update(scan)
        write_file(
            args.home / "weekly-report.json", json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        )
        write_file(args.home / "weekly-report.md", render(value))
    sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
