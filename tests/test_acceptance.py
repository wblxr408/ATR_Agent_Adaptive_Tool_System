"""Focused acceptance for this greenfield implementation; no historical suite."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from atr.errors import ATRError
from atr.models import (
    Check,
    CreateRequest,
    Dependency,
    ExecuteRequest,
    Intent,
    Manifest,
    Permissions,
    Program,
    Settings,
    Step,
)
from atr.policy import Policy
from atr.service import Runtime


def sample(project: str = "repo:test") -> Program:
    return Program(manifest=Manifest(
        id="project.sum", project_key=project, description="Sum a list of numbers",
        tags=["numbers", "sum"], input_schema={"type": "object", "properties": {
            "numbers": {"type": "array", "items": {"type": "number"}}},
            "required": ["numbers"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"sum": {"type": "number"}},
                       "required": ["sum"], "additionalProperties": False},
        checks=[{"inputs": {"numbers": [2, 3]}, "expected": {"sum": 5}}]),
        files={"main.py": "import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({'sum':sum(x['numbers'])}))\n"})


class ArithmeticSandbox:
    """Contract fixture for unavailable external execution; no host code evaluation.

    This fixture provides numeric JSON output only. It is not isolation evidence.
    """

    def run(self, spec: Any, cancel: Any) -> Any:
        from atr.runtime.sandbox import SandboxOutcome
        value = json.loads(spec.stdin)
        return SandboxOutcome(status="succeeded", exit_code=0,
                              stdout=json.dumps({"sum": sum(value["numbers"])}).encode())


def runtime(tmp_path: Path, **kwargs: Any) -> Runtime:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return Runtime(workspace=work, home=tmp_path / "control", project_key="repo:test",
                   sandbox=ArithmeticSandbox(), **kwargs)


def test_independent_program_is_captured_and_searchable(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    result = rt.execute(ExecuteRequest(root_task_id="task-1", program=sample(),
                                      generated_independent=True, inputs={"numbers": [4, 9]}))
    assert result.status == "succeeded", result.model_dump()
    assert result.validation_status == "passed"
    assert result.candidate_id
    matches = rt.read("registry_search", query="sum", inputs={"numbers": [1, 2]})
    assert matches[0]["id"] == result.candidate_id


def test_creation_deduplicates_and_requires_review(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    first = rt.tool_create(CreateRequest(root_task_id="a", program=sample()))
    second = rt.tool_create(CreateRequest(root_task_id="b", program=sample()))
    assert first["created"] is True
    assert second["created"] is False
    assert second["tool_id"] == first["tool_id"]
    denied = rt.execute(ExecuteRequest(mode="tool", root_task_id="a", tool_id=first["tool_id"],
                                     inputs={"numbers": [2, 3]}))
    assert denied.status == "failed"
    assert denied.error_code == "review_required"


def reviewed(rt: Runtime, program: Program | None = None) -> str:
    created = rt.tool_create(CreateRequest(root_task_id="setup", program=program or sample()))
    tool_id = created["tool_id"]
    report = rt.tool_review(tool_id)
    assert report.passed, report
    return tool_id


def test_five_eligible_uses_across_two_tasks_promote_and_duplicate_does_not_count(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    tool_id = reviewed(rt)
    for i in range(5):
        request = ExecuteRequest(mode="tool", root_task_id=f"task-{i % 2}", tool_id=tool_id,
                                 inputs={"numbers": [i, 2]}, operation_id=f"op-{i}")
        result = rt.execute(request)
        assert result.status == "succeeded"
        assert rt.execute(request).run_id == result.run_id
    tool = rt.read("tool", tool_id=tool_id)
    assert tool["state"] == "persistent"
    assert tool["stats"]["eligible_uses"] == 5
    assert tool["stats"]["total_calls"] == 5
    assert tool["stats"]["distinct_root_tasks"] == 2


def test_retries_caches_and_invalid_inputs_cannot_promote(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    tool_id = reviewed(rt)
    first = rt.execute(ExecuteRequest(mode="tool", root_task_id="task", tool_id=tool_id,
                                     inputs={"numbers": [1, 2]}))
    assert first.status == "succeeded"
    for i in range(6):
        rt.execute(ExecuteRequest(mode="tool", root_task_id="task", tool_id=tool_id,
                                  inputs={"numbers": [i]}, retry_of=first.run_id))
        rt.execute(ExecuteRequest(mode="tool", root_task_id="task", tool_id=tool_id,
                                  inputs={"numbers": [i]}, cache_hit=True))
    invalid = rt.execute(ExecuteRequest(mode="tool", root_task_id="task", tool_id=tool_id,
                                       inputs={"numbers": "wrong"}))
    assert invalid.error_code == "input_contract"
    assert rt.read("tool", tool_id=tool_id)["stats"]["eligible_uses"] == 1


def test_archive_restore_and_dormancy_reset_current_epoch(tmp_path: Path) -> None:
    clock = [datetime(2026, 10, 15, tzinfo=UTC)]
    rt = runtime(tmp_path, clock=lambda: clock[0])
    tool_id = reviewed(rt)
    rt.execute(ExecuteRequest(mode="tool", root_task_id="a", tool_id=tool_id, inputs={"numbers": [1]}))
    clock[0] += timedelta(days=15)
    rt.lifecycle.tick()
    assert rt.read("registry_search", query="sum") == []
    assert rt.read("tool", tool_id=tool_id)["state"] == "archived"
    assert rt.tool_review(tool_id, action="restore").passed
    assert rt.read("tool", tool_id=tool_id)["stats"]["eligible_uses"] == 0


def test_persistent_dormant_requires_revalidation(tmp_path: Path) -> None:
    clock = [datetime(2026, 10, 15, tzinfo=UTC)]
    rt = runtime(tmp_path, clock=lambda: clock[0], settings=Settings(promotion_uses=1, promotion_tasks=1))
    tool_id = reviewed(rt)
    rt.execute(ExecuteRequest(mode="tool", root_task_id="a", tool_id=tool_id, inputs={"numbers": [1]}))
    clock[0] += timedelta(days=91)
    rt.lifecycle.tick()
    assert rt.read("tool", tool_id=tool_id)["state"] == "dormant"
    result = rt.execute(ExecuteRequest(mode="tool", root_task_id="b", tool_id=tool_id, inputs={"numbers": [1]}))
    assert result.error_code == "review_required"
    assert rt.tool_review(tool_id, action="restore").passed
    assert rt.read("tool", tool_id=tool_id)["state"] == "persistent"


def workflow() -> Program:
    return Program(manifest=Manifest(
        id="project.composite", project_key="repo:test", kind="workflow", description="Composite numeric analysis",
        dependencies=[Dependency(tool_id="project.sum", version="v1")],
        steps=[Step(name="total", tool_id="project.sum", version="v1", inputs={"numbers": {"$input": "numbers"}})],
        input_schema=sample().manifest.input_schema,
        output_schema={"type": "object", "required": ["total"]},
        checks=[Check(inputs={"numbers": [2, 3]}, expected={"total": {"sum": 5}})]))


def test_composite_pins_dependency_counts_indirect_and_protects_delete(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    child = reviewed(rt)
    parent = reviewed(rt, workflow())
    result = rt.execute(ExecuteRequest(mode="tool", root_task_id="task", tool_id=parent,
                                      inputs={"numbers": [8, 2]}))
    assert rt.output(result) == {"total": {"sum": 10}}
    assert rt.read("tool", tool_id=child)["stats"]["indirect_uses"] == 1
    with pytest.raises(ATRError, match="dependency"):
        rt.tool_delete(child, purge=True)


def test_failed_upgrade_keeps_previous_version_and_rollback_is_atomic(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    tool_id = reviewed(rt)
    updated = sample()
    updated.manifest.version = "v2"
    updated.manifest.checks = [Check(inputs={"numbers": [2, 3]}, expected={"sum": 99})]
    rt.tool_create(CreateRequest(root_task_id="upgrade", program=updated, base_version="v1"))
    assert not rt.tool_review(tool_id, "v2").passed
    assert rt.read("tool", tool_id=tool_id)["manifest"]["version"] == "v1"
    rt.tool_review(tool_id, "v1", action="rollback")
    assert rt.execute(ExecuteRequest(mode="tool", root_task_id="a", tool_id=tool_id,
                                     inputs={"numbers": [2, 3]})).status == "succeeded"


def test_control_switch_blocks_dynamic_but_keeps_direct_path(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    tool_id = reviewed(rt)
    rt.set_switch("dynamic_execute", False)
    assert rt.execute(ExecuteRequest(mode="tool", root_task_id="a", tool_id=tool_id,
                                     inputs={"numbers": [1]})).error_code == "dynamic_disabled"
    assert rt.execute(ExecuteRequest(root_task_id="a", argv=["python", "an-existing-command"],
                                     inputs={"numbers": [1]})).status == "succeeded"


def test_router_does_not_call_model_for_simple_command_and_checks_scope(tmp_path: Path) -> None:
    class RouterModel:
        def decide(self, summary: str, matches: list[Any]) -> Any:
            raise AssertionError("ordinary commands must never invoke routing model")
    rt = runtime(tmp_path, route_model=RouterModel())
    assert rt.route(Intent(root_task_id="a", summary="git status", simple_command=True)).decision == "DIRECT"
    with pytest.raises(ATRError, match="another project"):
        rt.tool_create(CreateRequest(root_task_id="a", program=sample("repo:other")))


def test_permission_request_cannot_grant_workspace_write(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    result = rt.execute(ExecuteRequest(root_task_id="a", argv=["python", "write.py"],
        inputs={"numbers": [1]}, operation_id="write", side_effect="workspace_write",
        permissions=Permissions(file_write=["workspace"])))
    assert result.error_code == "permission_denied"


def test_operation_id_binds_payload_and_preserves_one_effect(tmp_path: Path) -> None:
    rt = runtime(tmp_path, policy=Policy(allow_workspace_write=True))
    request = ExecuteRequest(root_task_id="a", argv=["python", "write.py"],
        inputs={"numbers": [1]}, operation_id="write", side_effect="workspace_write",
        permissions=Permissions(file_write=["workspace"]))
    first = rt.execute(request)
    assert first.status == "succeeded"
    assert rt.execute(request).run_id == first.run_id
    changed = request.model_copy(update={"inputs": {"numbers": [2]}})
    assert rt.execute(changed).error_code == "operation_conflict"


def test_artifacts_are_bounded_and_control_objects_are_not_public(tmp_path: Path) -> None:
    rt = runtime(tmp_path, settings=Settings(excerpt_chars=64))
    class LargeSandbox:
        def run(self, spec: Any, cancel: Any) -> Any:
            from atr.runtime.sandbox import SandboxOutcome
            return SandboxOutcome("succeeded", 0, b"x" * 1000 + b" api_key=super-secret")
    rt.sandbox = LargeSandbox()
    result = rt.execute(ExecuteRequest(root_task_id="a", argv=["large-output"]))
    assert len(result.stdout_excerpt) == 64
    assert result.truncated
    piece = rt.read("run_output", ref=result.artifacts[0].ref, offset=990, limit=100)
    assert "super-secret" not in piece["text"]
    assert "REDACTED" in piece["text"]
    created = rt.tool_create(CreateRequest(root_task_id="a", program=sample()))
    _, row = rt.registry.program(created["tool_id"])
    with pytest.raises(ATRError, match="not a public"):
        rt.read("run_output", ref=row["program_ref"])


def test_workspace_reads_reject_escape_and_sensitive_files(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    (rt.workspace / "result.txt").write_text("abcdef")
    assert rt.read("workspace_read", path="result.txt", offset=1, limit=3)["text"] == "bcd"
    for name in ("../control/registry.sqlite3", ".env", "private.pem"):
        with pytest.raises(ATRError):
            rt.read("workspace_read", path=name)


def test_expired_candidate_does_not_reappear_when_program_runs_again(tmp_path: Path) -> None:
    clock = [datetime(2026, 10, 15, tzinfo=UTC)]
    rt = runtime(tmp_path, clock=lambda: clock[0])
    first = rt.execute(ExecuteRequest(root_task_id="a", program=sample(), generated_independent=True,
                                     inputs={"numbers": [1]}))
    assert first.candidate_id
    clock[0] += timedelta(days=15)
    # Capture must expire before deduplication even if no scheduler tick happened.
    later = rt.execute(ExecuteRequest(root_task_id="b", program=sample(), generated_independent=True,
                                     inputs={"numbers": [2]}))
    assert later.candidate_id != first.candidate_id
    assert rt.read("registry_search", query="sum")[0]["id"] == later.candidate_id
