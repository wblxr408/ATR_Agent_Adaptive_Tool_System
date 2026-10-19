import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from test_acceptance import reviewed, runtime, sample, workflow

from atr.agent_adapter import AgentAdapter, definitions
from atr.errors import ATRError
from atr.models import ExecuteRequest, Limits, Settings
from atr.runtime.sandbox import SandboxOutcome


def test_interface_schema_validates_real_execute_arguments() -> None:
    schema = next(d for d in definitions() if d["name"] == "execute")["input_schema"]
    validator = Draft202012Validator(schema)
    validator.validate({"root_task_id": "a", "argv": ["rg", "data"]})
    validator.validate({"action": "cancel", "run_id": "run_123"})
    assert sorted(d["name"] for d in definitions()) == ["execute", "read", "tool_create", "tool_delete", "tool_review"]


def test_control_directory_inside_workspace_is_rejected(tmp_path: Path) -> None:
    from atr.service import Runtime
    workspace = tmp_path / "work"
    workspace.mkdir()
    with pytest.raises(ATRError, match="separate"):
        Runtime(workspace=workspace, home=workspace / ".atr")


def test_fixed_interface_round_trip_and_safe_errors(tmp_path: Path) -> None:
    adapter = AgentAdapter(runtime(tmp_path))
    created = adapter.dispatch({"id": "1", "name": "tool_create", "arguments": {
        "root_task_id": "a", "program": sample().model_dump(mode="json")}})
    assert created["result"]["created"]
    bad = adapter.dispatch({"id": "2", "name": "execute", "arguments": {
        "root_task_id": "a", "argv": [], "secret": "never-echo-me"}})
    assert bad["error"]["code"] == "invalid_input"
    assert "never-echo-me" not in json.dumps(bad)


def test_single_slot_can_register_a_repeated_candidate(tmp_path: Path) -> None:
    rt = runtime(tmp_path, settings=Settings(max_concurrency=1))
    for task in ("a", "b"):
        result = rt.execute(ExecuteRequest(root_task_id=task, program=sample(),
            generated_independent=True, inputs={"numbers": [1]}))
        assert result.status == "succeeded"
    assert result.mode == "tool"


def test_kill_switch_cancels_running_program_and_other_client_can_cancel(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    started, stopped = threading.Event(), threading.Event()

    class Blocking:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            started.set()
            # Bounded wait just protects the test from a cancellation implementation failure.
            for _ in range(100):
                if cancel.is_set():
                    stopped.set()
                    return SandboxOutcome("cancelled")
                threading.Event().wait(.01)
            return SandboxOutcome("failed", error_code="cancel_not_delivered")
    rt.sandbox = Blocking()
    rt.execute(ExecuteRequest(root_task_id="a", program=sample(), inputs={"numbers": [1]}, background=True))
    assert started.wait(2)
    rt.set_switch("dynamic_execute", False)
    assert stopped.wait(.5)
    rt.close()


def test_cross_client_cancel_reaches_live_process(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    started, stopped = threading.Event(), threading.Event()

    class Blocking:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            started.set()
            for _ in range(150):
                if cancel.is_set():
                    stopped.set()
                    return SandboxOutcome("cancelled")
                threading.Event().wait(.01)
            return SandboxOutcome("failed", error_code="cancel_not_delivered")
    rt.sandbox = Blocking()
    result = rt.execute(ExecuteRequest(root_task_id="a", argv=["long-command"], background=True))
    assert started.wait(2)
    other = runtime(tmp_path)
    other.cancel(result.run_id)
    assert stopped.wait(.6)
    rt.close()
    other.close()


def test_archived_child_keeps_working_as_pinned_dependency(tmp_path: Path) -> None:
    clock = [datetime(2026, 10, 15, tzinfo=UTC)]
    rt = runtime(tmp_path, clock=lambda: clock[0])
    child = reviewed(rt)
    clock[0] += timedelta(days=10)
    parent = reviewed(rt, workflow())
    clock[0] += timedelta(days=5)
    rt.lifecycle.tick()
    assert rt.read("tool", tool_id=child)["state"] == "archived"
    result = rt.execute(ExecuteRequest(root_task_id="a", mode="tool", tool_id=parent,
                                      inputs={"numbers": [3, 6]}))
    assert result.status == "succeeded"
    assert rt.output(result) == {"total": {"sum": 9}}
    with pytest.raises(ATRError):
        rt.tool_delete(child, purge=True)


def test_zero_exit_without_output_contract_is_unknown_and_cannot_promote(tmp_path: Path) -> None:
    rt = runtime(tmp_path, settings=Settings(promotion_uses=1, promotion_tasks=1))
    program = sample()
    program.manifest.output_schema = {}
    tool_id = reviewed(rt, program)
    result = rt.execute(ExecuteRequest(root_task_id="a", mode="tool", tool_id=tool_id,
                                      inputs={"numbers": [3, 6]}))
    assert result.status == "succeeded"
    assert result.validation_status == "unknown"
    assert rt.read("tool", tool_id=tool_id)["state"] == "temporary"


def test_output_budget_is_enforced_at_untrusted_result_boundary(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    class Flood:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            return SandboxOutcome("succeeded", 0, b"x" * 2000)
    rt.sandbox = Flood()
    result = rt.execute(ExecuteRequest(root_task_id="a", argv=["flood"], limits=Limits(output_bytes=1024)))
    assert result.status == "failed"
    assert result.error_code == "output_limit"
    assert result.truncated


def test_cli_reads_workspace_fragment_and_uses_json_protocol(tmp_path: Path, capsys: Any) -> None:
    from atr.cli import main
    work = tmp_path / "work"
    work.mkdir()
    (work / "answer.txt").write_text("the actual answer is 42")
    arguments = tmp_path / "arguments.json"
    arguments.write_text(json.dumps({"path": "answer.txt", "offset": 21, "limit": 2}))
    assert main(["--workspace", str(work), "--home", str(tmp_path / "control"), "read", "workspace_read", str(arguments)]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "42"


def test_eval_report_rejects_unfair_pairs_and_empty_is_insufficient() -> None:
    from atr.evals import Observation, compare
    assert compare([])["status"] == "insufficient_data"
    base = Observation(task_id="a", domain="coding", arm="baseline", phase="hot", model="same",
        input_identity="same-input", permissions_identity="same-grants", prompt_token_budget=2000,
        task_budget_usd=1, success=True, quality=1, quality_evidence="unit contract",
        input_tokens=100, output_tokens=10, duration_ms=100)
    different = base.model_copy(update={"arm": "atr", "model": "different"})
    with pytest.raises(ATRError, match="model"):
        compare([base, different])
