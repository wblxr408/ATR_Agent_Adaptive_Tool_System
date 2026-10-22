"""Consumer contracts for the remaining plan requirements found on 2026-10-22.

Execution/provider substitutes expose scheduling and control behavior only;
they provide no Docker isolation or live billing evidence.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_acceptance import ArithmeticSandbox, reviewed, runtime, sample, workflow
from test_proxies import EvidenceAdapter, research_program

from atr.adapters import AdapterResponse
from atr.errors import ATRError
from atr.models import (
    Check,
    Cost,
    CreateRequest,
    Dependency,
    ExecuteRequest,
    Intent,
    Limits,
    Manifest,
    Permissions,
    Program,
    Settings,
    Step,
)
from atr.policy import Policy
from atr.runtime.sandbox import SandboxOutcome


@pytest.mark.parametrize("marker", ["retry", "cache"])
def test_workflow_attempt_markers_apply_to_child_promotion(tmp_path: Path, marker: str) -> None:
    with runtime(tmp_path) as rt:
        child = reviewed(rt)
        parent = reviewed(rt, workflow())
        first = rt.execute(ExecuteRequest(root_task_id="task", mode="tool", tool_id=parent,
                                          inputs={"numbers": [1]}))
        assert first.status == "succeeded"
        for number in range(2, 6):
            attributes = {"retry_of": first.run_id} if marker == "retry" else {"cache_hit": True}
            result = rt.execute(ExecuteRequest(root_task_id="task", mode="tool", tool_id=parent,
                inputs={"numbers": [number]}, **attributes))
            assert rt.output(result) == {"total": {"sum": number}}
        assert rt.read("tool", tool_id=child)["stats"]["eligible_uses"] == 1
        assert rt.read("tool", tool_id=child)["state"] == "temporary"


def test_nested_adapter_receives_parent_remaining_timeout(tmp_path: Path) -> None:
    class TimedEvidence(EvidenceAdapter):
        timeouts: list[float]

        def __init__(self) -> None:
            super().__init__()
            self.timeouts = []

        def call(self, inputs: Any, **kwargs: Any) -> AdapterResponse:
            self.timeouts.append(kwargs["timeout"])
            return super().call(inputs, **kwargs)

    adapter = TimedEvidence()
    with runtime(tmp_path, policy=Policy(network_capabilities=["research"]),
                 adapters={"research": adapter}) as rt:
        child = reviewed(rt, research_program())
        adapter.timeouts.clear()
        parent = Program(manifest=Manifest(id="project.pipeline", project_key="repo:test",
            kind="workflow", description="Bounded evidence workflow", side_effect="read_only",
            permissions=Permissions(network=["research"]), runtime=Limits(timeout_seconds=2),
            dependencies=[Dependency(tool_id=child, version="v1")],
            steps=[Step(name="evidence", tool_id=child, version="v1")],
            output_schema={"type": "object", "required": ["evidence"]}))
        result = rt.execute(ExecuteRequest(root_task_id="bounded", program=parent))
        assert result.status == "succeeded"
        assert rt.output(result)["evidence"]["fact_status"] == "unknown"
        assert len(adapter.timeouts) == 1
        assert 0 < adapter.timeouts[0] <= 2


@pytest.mark.parametrize("has_operation", [False, True])
def test_external_candidate_does_not_execute_automatic_review(
    tmp_path: Path, has_operation: bool,
) -> None:
    adapter = EvidenceAdapter()
    policy = Policy(network_capabilities=["research"], allow_external_write=True,
                    allow_external_review=True)
    with runtime(tmp_path, policy=policy, adapters={"research": adapter}) as rt:
        program = research_program()
        program.manifest.side_effect = "external_write"
        first = rt.execute(ExecuteRequest(root_task_id="a", program=program,
            generated_independent=True, operation_id="effect-a"))
        assert first.status == "succeeded"
        assert first.candidate_id
        second = rt.execute(ExecuteRequest(root_task_id="b", program=program,
            generated_independent=True, operation_id="effect-b" if has_operation else None))
        if has_operation:
            assert second.status == "succeeded"
            assert second.mode == "direct"
            assert adapter.calls == 2
            assert not rt.read("tool", tool_id=program.manifest.id)["review"]
            assert rt.tool_review(program.manifest.id, root_task_id="manual-review").passed
            assert adapter.calls == 3
        else:
            assert second.error_code == "operation_required"
            assert adapter.calls == 1


@pytest.mark.parametrize("pause", [False, True])
def test_candidate_registration_failure_retains_direct_execution(tmp_path: Path, pause: bool) -> None:
    with runtime(tmp_path, settings=Settings(max_creations_per_task=0 if not pause else 8)) as rt:
        first = rt.execute(ExecuteRequest(root_task_id="a", program=sample(),
            generated_independent=True, inputs={"numbers": [1]}))
        assert first.candidate_id
        if pause:
            rt.set_switch("auto_create", False)
        second = rt.execute(ExecuteRequest(root_task_id="b", program=sample(),
            generated_independent=True, inputs={"numbers": [3, 7]}))
        assert second.status == "succeeded"
        assert second.mode == "direct"
        assert rt.output(second) == {"sum": 10}


def writable_program(*, external: bool = False) -> Program:
    program = sample()
    program.manifest.permissions = Permissions(file_read=["workspace"],
                                               file_write=["workspace", "artifacts"])
    program.manifest.side_effect = "external_write" if external else "workspace_write"
    return program


class WorkspaceWriter(ArithmeticSandbox):
    def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
        values = json.loads(spec.stdin)["numbers"]
        (spec.workspace / "value.txt").write_text(str(sum(values)))
        if values == [-1]:
            return SandboxOutcome("succeeded", 0, b'{"sum":"invalid"}')
        return super().run(spec, cancel)


def test_failed_output_contract_does_not_publish_workspace_edits(tmp_path: Path) -> None:
    with runtime(tmp_path, policy=Policy(allow_workspace_write=True)) as rt:
        (rt.workspace / "value.txt").write_text("original")
        rt.sandbox = WorkspaceWriter()
        failed = rt.execute(ExecuteRequest(root_task_id="a", program=writable_program(),
            operation_id="write-invalid", inputs={"numbers": [-1]}))
        assert failed.error_code == "output_contract"
        assert (rt.workspace / "value.txt").read_text() == "original"
        succeeded = rt.execute(ExecuteRequest(root_task_id="b", program=writable_program(),
            operation_id="write-valid", inputs={"numbers": [2, 3]}))
        assert succeeded.status == "succeeded"
        assert (rt.workspace / "value.txt").read_text() == "5"
        changes = next(a for a in succeeded.artifacts if a.name == "workspace_changes.json")
        assert "value.txt" in json.loads(rt.read("run_output", ref=changes.ref)["text"])["changed_paths"]


def test_failed_workflow_discards_successful_earlier_child_edits(tmp_path: Path) -> None:
    with runtime(tmp_path, policy=Policy(allow_workspace_write=True)) as rt:
        (rt.workspace / "value.txt").write_text("original")
        rt.sandbox = WorkspaceWriter()
        child = reviewed(rt, writable_program())
        parent = workflow()
        parent.manifest.permissions = writable_program().manifest.permissions
        parent.manifest.side_effect = "workspace_write"
        parent.manifest.steps.append(Step(name="broken", tool_id=child, version="v1",
                                          inputs={"numbers": [-1]}))
        result = rt.execute(ExecuteRequest(root_task_id="a", program=parent,
            operation_id="workflow-edit", inputs={"numbers": [2, 3]}))
        assert result.error_code == "dependency_failed"
        assert (rt.workspace / "value.txt").read_text() == "original"


def test_direct_workflow_cannot_understate_child_permissions(tmp_path: Path) -> None:
    with runtime(tmp_path, policy=Policy(allow_workspace_write=True)) as rt:
        (rt.workspace / "value.txt").write_text("original")
        rt.sandbox = WorkspaceWriter()
        reviewed(rt, writable_program())
        result = rt.execute(ExecuteRequest(root_task_id="a", program=workflow(),
                                           inputs={"numbers": [2, 3]}))
        assert result.error_code == "permission_escalation"
        assert (rt.workspace / "value.txt").read_text() == "original"


def test_external_review_with_workspace_capability_is_disposable(tmp_path: Path) -> None:
    policy = Policy(allow_workspace_write=True, allow_external_write=True, allow_external_review=True)
    with runtime(tmp_path, policy=policy) as rt:
        (rt.workspace / "value.txt").write_text("original")
        rt.sandbox = WorkspaceWriter()
        tool = rt.tool_create(CreateRequest(root_task_id="a", program=writable_program(external=True)))["tool_id"]
        assert rt.tool_review(tool).passed
        assert (rt.workspace / "value.txt").read_text() == "original"


def test_external_write_with_workspace_capability_holds_project_lock(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockingWriter(WorkspaceWriter):
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            if json.loads(spec.stdin)["numbers"] == [1]:
                entered.set()
                if not release.wait(3):
                    return SandboxOutcome("failed", error_code="test_writer_not_released")
            return super().run(spec, cancel)

    with runtime(tmp_path, policy=Policy(allow_workspace_write=True, allow_external_write=True)) as rt:
        (rt.workspace / "value.txt").write_text("original")
        rt.sandbox = BlockingWriter()
        rt.execute(ExecuteRequest(root_task_id="a", program=writable_program(external=True),
            operation_id="external-with-files", inputs={"numbers": [1]}, background=True))
        try:
            assert entered.wait(2)
            competing = rt.execute(ExecuteRequest(root_task_id="b", program=writable_program(),
                operation_id="competing", inputs={"numbers": [2]}))
            assert competing.error_code == "workspace_busy"
            assert (rt.workspace / "value.txt").read_text() == "original"
        finally:
            release.set()


def test_failed_versions_consume_bounded_repair_budget(tmp_path: Path) -> None:
    with runtime(tmp_path, settings=Settings(max_repairs=1)) as rt:
        broken = sample()
        broken.manifest.checks = [Check(inputs={"numbers": [2, 3]}, expected={"sum": 99})]
        tool = rt.tool_create(CreateRequest(root_task_id="repair-task", program=broken))["tool_id"]
        assert not rt.tool_review(tool, root_task_id="repair-task").passed
        broken.manifest.version = "v2"
        rt.tool_create(CreateRequest(root_task_id="repair-task", program=broken, base_version="v1"))
        assert not rt.tool_review(tool, "v2", root_task_id="repair-task").passed
        broken.manifest.version = "v3"
        with pytest.raises(ATRError, match="repair"):
            rt.tool_create(CreateRequest(root_task_id="repair-task", program=broken, base_version="v1"))


def test_budget_rejection_does_not_report_a_routing_model_call(tmp_path: Path) -> None:
    class Model:
        max_cost_usd = .01
        calls = 0

        def decide(self, summary: str, matches: list[Any]) -> Any:
            self.calls += 1
            return "DEFER", Cost(category="router_llm", amount=.003)

    model = Model()
    with runtime(tmp_path, settings=Settings(max_task_cost=0), route_model=model) as rt:
        result = rt.route(Intent(root_task_id="a", summary="ambiguous request"))
        assert result.decision == "DEFER"
        assert model.calls == 0
        assert result.model_calls == 0
        assert rt.ledger.metrics()["model_fallback_ratio"] == 0


def test_pending_reservation_prevents_complete_billing_report(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        rt.ledger.reserve("inflight", .5)
        rt.ledger.record("inflight", Cost(category="external_api", amount=0))
        assert rt.ledger.report("inflight")["billing_complete"] is False


def test_unsettled_token_evidence_survives_ordinary_retention(tmp_path: Path) -> None:
    class Unbilled(EvidenceAdapter):
        max_cost_usd = .75

        def call(self, inputs: Any, **kwargs: Any) -> AdapterResponse:
            return AdapterResponse({"sources": [], "fact_status": "unknown"},
                                   [Cost(category="external_api", input_tokens=10)])

    clock = [datetime(2026, 10, 22, tzinfo=UTC)]
    with runtime(tmp_path, clock=lambda: clock[0], settings=Settings(aggregate_days=1),
                 policy=Policy(network_capabilities=["research"]), adapters={"research": Unbilled()}) as rt:
        result = rt.execute(ExecuteRequest(root_task_id="a", program=research_program()))
        assert result.status == "succeeded"
        clock[0] += timedelta(days=2)
        rt.lifecycle.tick()
        report = rt.ledger.report("a")
        assert report["unsettled_budget_usd"] == .75
        assert report["input_tokens"] == 10
        assert report["unknown_entries"] == 1


def test_receipt_identity_rejects_different_billing_payload(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        cost = Cost(category="external_api", amount=.01, input_tokens=10)
        rt.ledger.record("a", cost, entry_id="receipt-1")
        rt.ledger.record("a", cost, entry_id="receipt-1")
        assert rt.ledger.report("a")["known_totals"]["USD"] == .01
        with pytest.raises(ATRError, match="receipt"):
            rt.ledger.record("a", Cost(category="external_api", amount=.02), entry_id="receipt-1")


def test_amount_only_settlement_preserves_known_token_usage(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        reservation = rt.ledger.reserve("a", .5)
        rt.ledger.record("a", Cost(category="nested_llm", input_tokens=100, output_tokens=7,
                                   cached_input_tokens=40), entry_id=reservation + ":0")
        rt.ledger.release(reservation, billing_complete=False)
        receipt = Cost(category="nested_llm", amount=.1, billing_ref="actual-provider-receipt")
        rt.ledger.settle(reservation, receipt)
        rt.ledger.settle(reservation, receipt)
        report = rt.ledger.report("a")
        assert report["input_tokens"] == 100
        assert report["output_tokens"] == 7
        assert report["cached_input_tokens"] == 40
        assert report["known_totals"]["USD"] == .1
        assert report["billing_complete"] is True


def test_route_refreshes_environment_before_selecting_reuse(tmp_path: Path) -> None:
    class ChangedEnvironment(ArithmeticSandbox):
        def fingerprint(self) -> str:
            return "oci:changed-image"

    with runtime(tmp_path) as rt:
        tool = reviewed(rt)
        rt.sandbox = ChangedEnvironment()
        result = rt.route(Intent(root_task_id="a", tool_id=tool, summary="sum",
                                 inputs={"numbers": [1]}))
        assert result.decision == "DEFER"
        assert result.selected is None


def test_json_artifacts_remain_parseable_after_credential_redaction(tmp_path: Path) -> None:
    class JSONCredentials:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            return SandboxOutcome("succeeded", 0,
                b'{"password":"sensitive-value","missing_secret":null,"answer":42}')

    with runtime(tmp_path) as rt:
        rt.sandbox = JSONCredentials()
        result = rt.execute(ExecuteRequest(root_task_id="a", argv=["json-result"]))
        assert result.status == "succeeded"
        assert rt.output(result) == {"password": "[REDACTED]", "missing_secret": None, "answer": 42}


def test_same_task_promotion_exception_requires_one_task_to_meet_threshold(tmp_path: Path) -> None:
    with runtime(tmp_path, settings=Settings(promotion_uses=100, promotion_tasks=100,
                                             same_task_uses=3)) as rt:
        tool = reviewed(rt)
        for task, number in [("a", 1), ("a", 2), ("b", 3), ("b", 4)]:
            result = rt.execute(ExecuteRequest(root_task_id=task, mode="tool", tool_id=tool,
                                              inputs={"numbers": [number]}))
            assert result.status == "succeeded"
        assert rt.read("tool", tool_id=tool)["state"] == "temporary"
        rt.execute(ExecuteRequest(root_task_id="a", mode="tool", tool_id=tool,
                                  inputs={"numbers": [5]}))
        assert rt.read("tool", tool_id=tool)["state"] == "persistent"


@pytest.mark.parametrize("action", ["delete", "switch"])
def test_queued_run_pins_and_protects_accepted_version(tmp_path: Path, action: str) -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockFirst(ArithmeticSandbox):
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            if json.loads(spec.stdin)["numbers"] == [0]:
                entered.set()
                if not release.wait(3):
                    return SandboxOutcome("failed", error_code="test_not_released")
            return super().run(spec, cancel)

    with runtime(tmp_path, settings=Settings(max_concurrency=1)) as rt:
        tool = reviewed(rt)
        newer = sample()
        newer.manifest.version = "v2"
        rt.tool_create(CreateRequest(root_task_id="upgrade", program=newer, base_version="v1"))
        assert rt.tool_review(tool, "v2").passed
        rt.tool_review(tool, "v1", action="rollback")
        rt.sandbox = BlockFirst()
        rt.execute(ExecuteRequest(root_task_id="block", argv=["long-operation"],
                                  inputs={"numbers": [0]}, background=True))
        try:
            assert entered.wait(2)
            queued = rt.execute(ExecuteRequest(root_task_id="queued", mode="tool", tool_id=tool,
                                               inputs={"numbers": [1]}, background=True))
            if action == "delete":
                with pytest.raises(ATRError, match="unfinished"):
                    rt.tool_delete(tool, "v1", purge=True)
            else:
                rt.tool_review(tool, "v2", action="rollback")
            release.set()
            deadline = time.monotonic() + 3
            while (result := rt.read("run", run_id=queued.run_id))["status"] == "running":
                assert time.monotonic() < deadline, result
                time.sleep(.01)
            assert result["status"] == "succeeded", result
            assert result["version"] == "v1"
        finally:
            release.set()
