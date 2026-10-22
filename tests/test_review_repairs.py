"""The six reported defect triggers; substitutes are not isolation evidence."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from atr.adapters import JSONModelAdapter
from atr.control.authority import BehaviorRule, ResourceScope, TaskContext, TrustedCase
from atr.models import Check, CreateRequest, ExecuteRequest, Manifest, Program, Settings
from atr.policy import Policy
from atr.runtime.sandbox import DockerSandbox, SandboxOutcome
from atr.service import Runtime


class Output:
    def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
        return SandboxOutcome("succeeded", 0, spec.files.get("result.json", b"null"))


def runtime(tmp_path: Path, **kwargs: Any) -> Runtime:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return Runtime(workspace=workspace, home=tmp_path / "control", project_key="repo:repair",
                   sandbox=kwargs.pop("sandbox", Output()), **kwargs)


def program(version: str = "v1", value: Any = 1) -> Program:
    return Program(manifest=Manifest(id="repair.value", version=version,
        project_key="repo:repair", description="Return a value",
        output_schema={"type": "integer"}, checks=[Check(expected=value)]),
        files={"result.json": json.dumps(value)})


def approved(rt: Runtime, context: TaskContext, request: ExecuteRequest, expected: Any = None) -> ExecuteRequest:
    return rt.control.authorize_operation(context, request,
        business_key=request.operation_id or request.argv[0] if request.argv else "approved",
        rule=BehaviorRule(checks=[Check(expected=expected)]))


def test_secret_json_keys_nested_and_colliding_remain_redacted(tmp_path: Path) -> None:
    with runtime(tmp_path, secrets=["private-value"]) as rt:
        value = {"private-value": {"private-value": "safe"}, "[REDACTED]": "other"}
        result = rt.store.artifact("run", "stdout", json.dumps(value).encode(), rt.clock())
        output = json.loads(rt.store.get(result.ref))
        assert "private-value" not in json.dumps(output)
        assert len(output) == 2
        assert sorted(v for v in output.values() if isinstance(v, str)) == ["other"]
        assert "private-value" not in rt.store.read_artifact(result.ref)["text"]


def test_workspace_can_commit_129_files_without_artifact_count_limit(tmp_path: Path) -> None:
    stage, work = tmp_path / "stage", tmp_path / "work"
    stage.mkdir()
    work.mkdir()
    baseline = {f"file-{n}.txt": b"old" for n in range(128)}
    for name, value in baseline.items():
        (stage / name).write_bytes(value)
        (work / name).write_bytes(value)
    (stage / "file-128.txt").write_bytes(b"new")
    changes = DockerSandbox._commit(stage, work, Policy(allow_workspace_write=True), 65536, baseline)
    assert changes["changed_paths"] == ["file-128.txt"]
    assert (work / "file-128.txt").read_bytes() == b"new"


def test_pinned_bad_historical_version_does_not_rollback_healthy_current(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        context = rt.control.issue_task(scope=ResourceScope(tool_actions={
            "repair.value": ["create", "review"]}))
        for number in range(1, 4):
            candidate = program(f"v{number}", number)
            rt.control.approve_program(candidate, cases=[TrustedCase(
                rule=BehaviorRule(checks=[Check(expected=number)]))])
            rt.tool_create(CreateRequest(root_task_id=context.root_task_id, program=candidate,
                base_version=f"v{number-1}" if number > 1 else None), context=context)
            assert rt.tool_review("repair.value", f"v{number}", context=context).passed
        # The provider fixture changes behavior after successful admission of v2.
        class BadHistorical(Output):
            def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
                return SandboxOutcome("succeeded", 0, b'"invalid"')
        rt.sandbox = BadHistorical()
        request = approved(rt, context, ExecuteRequest(root_task_id=context.root_task_id, mode="tool",
            tool_id="repair.value", version="v2"), 2)
        result = rt.execute(request, context=context)
        assert result.error_code == "output_contract"
        assert rt.registry.program("repair.value")[1]["version"] == "v3"


def test_close_cancels_queued_call_without_running_it(tmp_path: Path) -> None:
    entered = threading.Event()
    cancelled = threading.Event()
    calls: list[list[str]] = []

    class Blocking:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            calls.append(spec.argv)
            entered.set()
            assert cancel.wait(5), "close did not cancel the active call"
            cancelled.set()
            return SandboxOutcome("cancelled")

    rt = runtime(tmp_path, sandbox=Blocking(), settings=Settings(max_concurrency=1))
    context = rt.control.issue_task()
    first = rt.execute(approved(rt, context, ExecuteRequest(root_task_id=context.root_task_id,
        argv=["active"], background=True)), context=context)
    assert entered.wait(5)
    queued = rt.execute(approved(rt, context, ExecuteRequest(root_task_id=context.root_task_id,
        argv=["queued"], background=True)), context=context)
    rt.close()
    assert cancelled.is_set()
    assert calls == [["active"]]
    with runtime(tmp_path) as reopened:
        assert reopened.read("run", run_id=first.run_id)["status"] == "cancelled"
        assert reopened.read("run", run_id=queued.run_id)["status"] == "cancelled"


@pytest.mark.parametrize("choices", [None, [], [{}], [{"message": None}]])
def test_malformed_model_envelope_retains_usage_and_error(
    tmp_path: Path, choices: Any,
) -> None:
    adapter = JSONModelAdapter(endpoint="https://example.org/chat", model="test",
        api_key="private", input_usd_per_million=1, output_usd_per_million=2)
    envelope = {"id": "bill-known", "usage": {"prompt_tokens": 9, "completion_tokens": 4}}
    if choices is not None:
        envelope["choices"] = choices
    adapter.client.request = lambda *a, **kw: (json.dumps(envelope).encode(), {})  # type: ignore[method-assign]
    with runtime(tmp_path, adapters={"model": adapter}, policy=Policy(
            network_capabilities=["model"], secret_capabilities=["model_api"])) as rt:
        from atr.models import Permissions
        candidate = Program(manifest=Manifest(id="repair.model", project_key="repo:repair",
            description="Model result", kind="model_pipeline", adapter="model",
            permissions=Permissions(network=["model"], secrets=["model_api"]),
            output_schema={"type": "object"}))
        context = rt.control.issue_task(scope=ResourceScope(adapters=["model"]))
        rt.control.approve_program(candidate, cases=[TrustedCase(
            inputs={"text": "data"}, rule=BehaviorRule(checks=[Check(expected=None)]))])
        request = approved(rt, context, ExecuteRequest(root_task_id=context.root_task_id, program=candidate,
            inputs={"text": "data"}))
        result = rt.execute(request, context=context)
        assert result.error_code == "adapter_contract"
        report = rt.ledger.report(context.root_task_id)
        assert (report["input_tokens"], report["output_tokens"]) == (9, 4)
        costs = rt.store.rows("SELECT data FROM costs WHERE category='nested_llm'")
        assert json.loads(costs[0]["data"])["billing_ref"] == "bill-known"
        assert report["unsettled_budget_usd"] == .1


def test_review_null_root_does_not_satisfy_missing_expected_path(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        candidate = program(value=None)
        candidate.manifest.output_schema = {"type": "null"}
        candidate.manifest.checks = [Check(expected=None, expected_path="required_field")]
        context = rt.control.issue_task(scope=ResourceScope(tool_actions={
            "repair.value": ["create", "review"]}))
        rt.control.approve_program(candidate, cases=[TrustedCase(rule=BehaviorRule(
            checks=[Check(expected=None, expected_path="required_field")]))])
        rt.tool_create(CreateRequest(root_task_id=context.root_task_id, program=candidate), context=context)
        report = rt.tool_review("repair.value", context=context)
        assert not report.passed
        assert report.evidence[0]["status"] == "failed"
