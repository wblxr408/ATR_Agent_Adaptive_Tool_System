"""Contracts for malicious callers and host-owned authority, no live providers."""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_review_repairs import program, runtime

from atr.agent_adapter import AgentAdapter
from atr.control.authority import (
    AdapterGrant,
    BehaviorRule,
    ResourceScope,
    TaskContext,
    TrustedCase,
)
from atr.errors import ATRError
from atr.models import (
    Check,
    CreateRequest,
    Dependency,
    ExecuteRequest,
    Manifest,
    Permissions,
    Program,
    Settings,
    Step,
)
from atr.policy import Policy
from atr.runtime.sandbox import SandboxOutcome


def test_agent_cannot_invent_a_root_task_to_obtain_execution(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        result = rt.execute(ExecuteRequest(root_task_id="invented", argv=["unapproved"]))
        assert result.error_code == "task_authority"


def test_self_authored_checks_cannot_admit_a_tool(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        rt.registry.create(program(), "operator-setup")
        context = rt.control.issue_task(scope=ResourceScope(tool_actions={"repair.value": ["review"]}))
        report = rt.tool_review("repair.value", context=context)
        assert not report.passed
        assert "trusted_acceptance_required" in " ".join(report.errors)


def rule(value: Any, *, changes: dict[str, str | None] | None = None) -> BehaviorRule:
    return BehaviorRule(checks=[Check(expected=value)], expected_changes=changes or {})


def grant(rt: Any, context: TaskContext, *, argv: list[str] | None = None,
          candidate: Any = None, expected: Any = None, key: str = "business-operation",
          permissions: Permissions | None = None, side_effect: str = "read_only",
          changes: dict[str, str | None] | None = None, background: bool = False) -> ExecuteRequest:
    request = ExecuteRequest(root_task_id=context.root_task_id, argv=argv, program=candidate,
        permissions=permissions or Permissions(), side_effect=side_effect, background=background)
    return rt.control.authorize_operation(context, request, business_key=key,
        rule=rule(expected, changes=changes))


class Editing:
    def __init__(self) -> None:
        self.calls = 0
        self.seen: dict[str, bytes] = {}

    def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
        self.calls += 1
        self.seen = {p.name: p.read_bytes() for p in spec.workspace.iterdir() if p.is_file()}
        if spec.argv[0] == "delete":
            (spec.workspace / spec.argv[1]).unlink()
        elif spec.argv[0] == "put":
            (spec.workspace / spec.argv[1]).write_text(spec.argv[2])
        return SandboxOutcome("succeeded", 0, b'{"ok":true}')


def test_bound_operation_executes_once_and_rejects_input_or_identity_changes(tmp_path: Path) -> None:
    adapter = Editing()
    with runtime(tmp_path, sandbox=adapter, policy=Policy(allow_workspace_write=True)) as rt:
        context = rt.control.issue_task(scope=ResourceScope(write_paths={"result.txt": ["create"]}))
        request = grant(rt, context, argv=["put", "result.txt", "approved"], expected={"ok": True},
            permissions=Permissions(file_write=["workspace"]), side_effect="workspace_write",
            changes={"result.txt": "approved"})
        first = rt.execute(request, context=context)
        assert first.status == "succeeded", first
        assert rt.execute(request, context=context).run_id == first.run_id
        assert (rt.workspace / "result.txt").read_text() == "approved"
        altered = request.model_copy(update={"argv": ["put", "result.txt", "malicious"]})
        assert rt.execute(altered, context=context).error_code == "operation_authority"
        rotated = request.model_copy(update={"operation_id": "another-id"})
        assert rt.execute(rotated, context=context).error_code == "operation_authority"
        invented = request.model_copy(update={"root_task_id": "new-budget"})
        assert rt.execute(invented, context=context).error_code == "task_authority"
        assert adapter.calls == 1


@pytest.mark.parametrize("argv,changes", [
    (["delete", "business.txt"], {"business.txt": None}),
    (["put", "other.txt", "changed"], {"other.txt": "changed"}),
])
def test_granted_workspace_write_cannot_delete_or_change_other_resources(
    tmp_path: Path, argv: list[str], changes: dict[str, str | None],
) -> None:
    with runtime(tmp_path, sandbox=Editing(), policy=Policy(allow_workspace_write=True)) as rt:
        (rt.workspace / "business.txt").write_text("valuable")
        context = rt.control.issue_task(scope=ResourceScope(read_paths=["business.txt"],
            write_paths={"business.txt": ["update"]}))
        request = grant(rt, context, argv=argv, expected={"ok": True},
            changes=changes, permissions=Permissions(file_read=["workspace"], file_write=["workspace"]),
            side_effect="workspace_write")
        result = rt.execute(request, context=context)
        assert result.error_code == "permission_denied", result
        assert (rt.workspace / "business.txt").read_text() == "valuable"
        assert sorted(p.name for p in rt.workspace.iterdir()) == ["business.txt"]


def test_trusted_output_acceptance_prevents_publishing_valid_but_false_result(tmp_path: Path) -> None:
    with runtime(tmp_path, sandbox=Editing(), policy=Policy(allow_workspace_write=True)) as rt:
        context = rt.control.issue_task(scope=ResourceScope(write_paths={"result.txt": ["create"]}))
        request = grant(rt, context, argv=["put", "result.txt", "wrong"], expected={"ok": False},
            permissions=Permissions(file_write=["workspace"]), side_effect="workspace_write",
            changes={"result.txt": "wrong"})
        result = rt.execute(request, context=context)
        assert result.error_code == "behavior_contract"
        assert list(rt.workspace.iterdir()) == []


def test_unexpected_effects_are_discarded_even_inside_write_scope(tmp_path: Path) -> None:
    with runtime(tmp_path, sandbox=Editing(), policy=Policy(allow_workspace_write=True)) as rt:
        context = rt.control.issue_task(scope=ResourceScope(write_paths={"result.txt": ["create"]}))
        request = grant(rt, context, argv=["put", "result.txt", "malicious"], expected={"ok": True},
            permissions=Permissions(file_write=["workspace"]), side_effect="workspace_write",
            changes={"result.txt": "approved"})
        assert rt.execute(request, context=context).error_code == "behavior_contract"
        assert list(rt.workspace.iterdir()) == []


def test_host_adapter_binds_task_and_denies_cross_task_artifact_reads(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        first, second = rt.control.issue_task(), rt.control.issue_task()
        request = grant(rt, first, argv=["return-null"])
        result = rt.execute(request, context=first)
        assert result.status == "succeeded", result
        agent = AgentAdapter(rt, context=second)
        response = agent.dispatch({"id": 1, "name": "read", "arguments": {
            "operation": "run_output", "arguments": {"ref": result.artifacts[0].ref}}})
        assert response["error"]["code"] == "permission_denied"
        response = agent.dispatch({"id": 2, "name": "execute", "arguments": request.model_dump(mode="json")})
        assert response["error"]["code"] == "task_authority"


def test_task_read_grants_filter_workspace_and_agent_read_api(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        (rt.workspace / "allowed.txt").write_text("allowed")
        (rt.workspace / "other.txt").write_text("not for this task")
        context = rt.control.issue_task(scope=ResourceScope(read_paths=["allowed.txt"]))
        agent = AgentAdapter(rt, context=context)
        allowed = agent.call("read", {"operation": "workspace_read", "arguments": {"path": "allowed.txt"}})
        assert allowed["text"] == "allowed"
        rejected = agent.dispatch({"name": "read", "arguments": {
            "operation": "workspace_read", "arguments": {"path": "other.txt"}}})
        assert rejected["error"]["code"] == "permission_denied"


def test_task_deadline_call_budget_and_forged_credential(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 22, tzinfo=UTC)]
    with runtime(tmp_path, clock=lambda: now[0]) as rt:
        context = rt.control.issue_task(max_calls=1, ttl_seconds=10)
        first = grant(rt, context, argv=["first"], key="first")
        second = grant(rt, context, argv=["second"], key="second")
        assert rt.execute(first, context=context).status == "succeeded"
        assert rt.execute(second, context=context).error_code == "task_calls"
        forged = TaskContext(context.root_task_id, "caller-invented")
        assert rt.execute(first, context=forged).error_code == "task_authority"
        now[0] += timedelta(seconds=11)
        assert rt.execute(first, context=context).error_code == "task_expired"


def test_total_cost_budget_survives_new_tasks_and_restart(tmp_path: Path) -> None:
    with runtime(tmp_path, settings=Settings(max_total_cost=1)) as rt:
        a = rt.control.issue_task()
        held = rt.ledger.reserve(a.root_task_id, .75)
        rt.ledger.release(held, billing_complete=False)
    with runtime(tmp_path, settings=Settings(max_total_cost=100)) as rt:
        b = rt.control.issue_task()
        with pytest.raises(ATRError, match="persistent total cost"):
            rt.ledger.reserve(b.root_task_id, .5)


def test_revoked_queued_operation_never_enters_execution_boundary(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[list[str]] = []
    class Blocking:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            calls.append(spec.argv)
            if spec.argv == ["first"]:
                entered.set()
                assert release.wait(5)
            return SandboxOutcome("succeeded", 0, b"null")
    with runtime(tmp_path, sandbox=Blocking(), settings=Settings(max_concurrency=1)) as rt:
        context = rt.control.issue_task()
        first = grant(rt, context, argv=["first"], key="first", background=True)
        second = grant(rt, context, argv=["second"], key="second", background=True)
        rt.execute(first, context=context)
        assert entered.wait(5)
        queued = rt.execute(second, context=context)
        rt.control.revoke_operation(second.operation_id)
        release.set()
        future = rt.futures[queued.run_id]
        assert future.result(timeout=5).error_code == "operation_authority"
        assert calls == [["first"]]


def test_independent_program_review_uses_host_cases_and_revocation(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        candidate = program(value=1)
        candidate.manifest.checks = [Check(expected=1)]
        context = rt.control.issue_task(scope=ResourceScope(
            tool_actions={"repair.value": ["create", "review"]}))
        rt.tool_create(CreateRequest(root_task_id=context.root_task_id, program=candidate), context=context)
        rt.control.approve_program(candidate, cases=[TrustedCase(rule=rule(2))])
        assert not rt.tool_review("repair.value", context=context).passed
        # Changing only self-authored checks cannot reuse the immutable host approval.
        changed = candidate.model_copy(deep=True)
        changed.manifest.checks = [Check(expected=2)]
        denied = grant(rt, context, candidate=changed, expected=1, key="changed-program")
        assert rt.execute(denied, context=context).error_code == "trusted_acceptance_required"


def test_unauthorized_review_cannot_quarantine_an_admitted_tool(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        candidate = program()
        owner = rt.control.issue_task(scope=ResourceScope(tool_actions={"repair.value": ["create", "review"]}))
        rt.control.approve_program(candidate, cases=[TrustedCase(rule=rule(1))])
        rt.tool_create(CreateRequest(root_task_id=owner.root_task_id, program=candidate), context=owner)
        assert rt.tool_review("repair.value", context=owner).passed
        stranger = rt.control.issue_task()
        assert not rt.tool_review("repair.value", context=stranger).passed
        assert rt.read("tool", tool_id="repair.value")["review"]["passed"]


def test_directory_read_grant_does_not_allow_parent_traversal(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        (rt.workspace / "docs").mkdir()
        (rt.workspace / "private.txt").write_text("private")
        context = rt.control.issue_task(scope=ResourceScope(read_paths=["docs/"]))
        agent = AgentAdapter(rt, context=context)
        response = agent.dispatch({"name": "read", "arguments": {
            "operation": "workspace_read", "arguments": {"path": "docs/../private.txt"}}})
        assert response["error"]["code"] == "permission_denied"


def test_revoked_program_cannot_publish_after_execution_started(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    class Waiting:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            (spec.workspace / "result.txt").write_text("changed")
            entered.set()
            assert release.wait(5)
            return SandboxOutcome("succeeded", 0, b"1")
    with runtime(tmp_path, sandbox=Waiting(), policy=Policy(allow_workspace_write=True)) as rt:
        candidate = program()
        candidate.manifest.permissions = Permissions(file_write=["workspace"])
        candidate.manifest.side_effect = "workspace_write"
        context = rt.control.issue_task(scope=ResourceScope(write_paths={"result.txt": ["create"]}))
        rt.control.approve_program(candidate, cases=[TrustedCase(rule=rule(1, changes={"result.txt": "changed"}))])
        request = grant(rt, context, candidate=candidate, expected=1,
            changes={"result.txt": "changed"}, background=True)
        result = rt.execute(request, context=context)
        assert entered.wait(5)
        future = rt.futures[result.run_id]
        rt.control.revoke_program(candidate)
        release.set()
        assert future.result(timeout=5).error_code == "trusted_acceptance_required"
        assert list(rt.workspace.iterdir()) == []


def test_sync_execution_close_waits_and_disallows_new_calls(tmp_path: Path) -> None:
    entered = threading.Event()
    class Waiting:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            entered.set()
            assert cancel.wait(5)
            return SandboxOutcome("cancelled")
    rt = runtime(tmp_path, sandbox=Waiting())
    context = rt.control.issue_task()
    request = grant(rt, context, argv=["blocking"])
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(rt.execute(request, context=context)))
    worker.start()
    assert entered.wait(5)
    rt.close()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert results[0].status == "cancelled"
    assert rt.execute(request, context=context).error_code == "runtime_closed"
    rt.close()


def test_business_identity_cannot_be_reapproved_with_changed_target(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        context = rt.control.issue_task()
        first = grant(rt, context, argv=["original"])
        with pytest.raises(ATRError, match="business identity"):
            grant(rt, context, argv=["changed"])
        assert rt.control.authorize_operation(context, first, business_key="business-operation",
            rule=rule(None)).operation_id == first.operation_id


def test_workflow_children_share_root_call_allowance(tmp_path: Path) -> None:
    class Counting:
        calls = 0
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            self.calls += 1
            return SandboxOutcome("succeeded", 0, b"1")
    boundary = Counting()
    with runtime(tmp_path, sandbox=boundary) as rt:
        child = program()
        setup = rt.control.issue_task(scope=ResourceScope(tool_actions={"repair.value": ["create", "review"]}))
        rt.control.approve_program(child, cases=[TrustedCase(rule=rule(1))])
        rt.tool_create(CreateRequest(root_task_id=setup.root_task_id, program=child), context=setup)
        assert rt.tool_review("repair.value", context=setup).passed
        boundary.calls = 0
        parent = Program(manifest=Manifest(id="repair.parent", project_key="repo:repair", description="Two steps",
            kind="workflow", dependencies=[Dependency(tool_id="repair.value", version="v1")],
            steps=[Step(name="first", tool_id="repair.value", version="v1"),
                   Step(name="second", tool_id="repair.value", version="v1")],
            output_schema={"type": "object"}))
        expected = {"first": 1, "second": 1}
        rt.control.approve_program(parent, cases=[TrustedCase(rule=rule(expected))])
        context = rt.control.issue_task(max_calls=2)
        request = grant(rt, context, candidate=parent, expected=expected)
        result = rt.execute(request, context=context)
        assert result.error_code == "task_calls", result
        assert boundary.calls == 1


def test_adapter_request_parameters_are_bound_before_provider_effect(tmp_path: Path) -> None:
    from atr.adapters import AdapterResponse
    from atr.models import Cost
    class Provider:
        max_cost_usd = .1
        required_secrets: list[str] = []
        submitted: list[Any] = []
        def call(self, inputs: Any, **kwargs: Any) -> AdapterResponse:
            self.submitted.append(inputs)
            return AdapterResponse({"sent": True}, [Cost(category="external_api", amount=.05)])
    provider = Provider()
    with runtime(tmp_path, policy=Policy(allow_external_write=True, network_capabilities=["send"]),
                 adapters={"send": provider}) as rt:
        candidate = Program(manifest=Manifest(id="repair.sender", project_key="repo:repair", description="Send data",
            kind="adapter", adapter="send", permissions=Permissions(network=["send"]), side_effect="external_write",
            output_schema={"type": "object"}))
        rt.control.approve_program(candidate, cases=[TrustedCase(rule=rule({"sent": True}))])
        context = rt.control.issue_task(scope=ResourceScope(adapters=["send"]))
        request = ExecuteRequest(root_task_id=context.root_task_id, program=candidate,
            inputs={"recipient": "approved", "body": "public"})
        request = rt.control.authorize_operation(context, request, business_key="send-one",
            rule=rule({"sent": True}))
        altered = request.model_copy(update={"inputs": {"recipient": "attacker", "body": "private"}})
        assert rt.execute(altered, context=context).error_code == "operation_authority"
        assert provider.submitted == []
        first = rt.execute(request, context=context)
        assert first.status == "succeeded", first
        assert rt.execute(request, context=context).run_id == first.run_id
        assert provider.submitted == [{"recipient": "approved", "body": "public"}]


def test_task_creation_limit_is_independent_of_agent_root_labels(tmp_path: Path) -> None:
    with runtime(tmp_path) as rt:
        context = rt.control.issue_task(max_creations=1, scope=ResourceScope(tool_actions={
            "repair.value": ["create"], "repair.other": ["create"]}))
        rt.tool_create(CreateRequest(root_task_id=context.root_task_id, program=program()), context=context)
        other = program(value=2)
        other.manifest.id = "repair.other"
        with pytest.raises(ATRError, match="creation budget"):
            rt.tool_create(CreateRequest(root_task_id=context.root_task_id, program=other), context=context)


def test_reissued_context_restores_same_task_and_does_not_reset_calls(tmp_path: Path) -> None:
    credential = tmp_path / "host-task.json"
    with runtime(tmp_path) as rt:
        context = rt.control.issue_task(max_calls=1)
        rt.control.save_context(context, credential)
        request = grant(rt, context, argv=["one"], key="one")
        assert rt.execute(request, context=context).status == "succeeded"
    with runtime(tmp_path) as rt:
        restored = TaskContext(**json.loads(credential.read_text()))
        request = grant(rt, restored, argv=["two"], key="two")
        assert rt.execute(request, context=restored).error_code == "task_calls"


def test_log_retention_does_not_refill_total_money_budget(tmp_path: Path) -> None:
    from atr.models import Cost
    now = [datetime(2026, 10, 22, tzinfo=UTC)]
    with runtime(tmp_path, settings=Settings(max_total_cost=1), clock=lambda: now[0]) as rt:
        context = rt.control.issue_task()
        rt.ledger.record(context.root_task_id, Cost(category="main_llm", amount=.75), entry_id="host-receipt")
        now[0] += timedelta(days=181)
        rt.lifecycle.tick()
        another = rt.control.issue_task()
        with pytest.raises(ATRError, match="persistent total cost"):
            rt.ledger.reserve(another.root_task_id, .5)


def test_workflow_network_step_cannot_expand_root_data_egress_authority(tmp_path: Path) -> None:
    from atr.adapters import AdapterResponse
    from atr.models import Cost
    class Provider:
        max_cost_usd = .1
        required_secrets: list[str] = []
        calls = 0
        def call(self, inputs: Any, **kwargs: Any) -> AdapterResponse:
            self.calls += 1
            return AdapterResponse({"sent": True}, [Cost(category="external_api", amount=0)])
    provider = Provider()
    with runtime(tmp_path, policy=Policy(network_capabilities=["send"]), adapters={"send": provider}) as rt:
        child = Program(manifest=Manifest(id="repair.egress", project_key="repo:repair", description="Network step",
            kind="adapter", adapter="send", permissions=Permissions(network=["send"]),
            output_schema={"type": "object"}))
        rt.control.approve_program(child, cases=[TrustedCase(rule=rule({"sent": True}))])
        created = rt.registry.create(rt._bind_program(child), "host-setup")
        # Set up an admitted immutable dependency without invoking a network effect.
        with rt.store.transaction() as db:
            db.execute("UPDATE tools SET reviewed=1 WHERE id=?", (created["tool_id"],))
        parent = Program(manifest=Manifest(id="repair.egress-parent", project_key="repo:repair",
            description="A workflow may not infer data disclosure authority", kind="workflow",
            permissions=Permissions(network=["send"]),
            dependencies=[Dependency(tool_id=child.manifest.id, version="v1")],
            steps=[Step(name="send", tool_id=child.manifest.id, version="v1", inputs={"text": "private-data"})],
            output_schema={"type": "object"}))
        rt.control.approve_program(parent, cases=[TrustedCase(rule=rule({"send": {"sent": True}}))])
        context = rt.control.issue_task(scope=ResourceScope(adapters=["send"]))
        request = grant(rt, context, candidate=parent, expected={"send": {"sent": True}})
        result = rt.execute(request, context=context)
        assert result.status == "failed"
        assert provider.calls == 0
        allowed_rule = rule({"send": {"sent": True}})
        allowed_rule.adapter_calls = [AdapterGrant(capability="send", inputs={"text": "private-data"})]
        authorized = rt.control.authorize_operation(context,
            ExecuteRequest(root_task_id=context.root_task_id, program=parent),
            business_key="explicit-disclosure", rule=allowed_rule)
        assert rt.execute(authorized, context=context).status == "succeeded"
        assert provider.calls == 1
        # Revocation after one permitted effect must fence the next workflow step.
        parent.manifest.steps.append(Step(name="send_again", tool_id=child.manifest.id,
            version="v1", inputs={"text": "private-data"}))
        expected_two = {"send": {"sent": True}, "send_again": {"sent": True}}
        rt.control.approve_program(parent, cases=[TrustedCase(rule=rule(expected_two))])
        two_rule = rule(expected_two)
        two_rule.adapter_calls = [AdapterGrant(capability="send", inputs={"text": "private-data"}, max_calls=2)]
        two = rt.control.authorize_operation(context, ExecuteRequest(root_task_id=context.root_task_id,
            program=parent), business_key="revoked-between-steps", rule=two_rule)
        previous_call = provider.call
        def revoke_after_call(inputs: Any, **kwargs: Any) -> AdapterResponse:
            response = previous_call(inputs, **kwargs)
            rt.control.revoke_operation(two.operation_id)
            return response
        provider.call = revoke_after_call  # type: ignore[method-assign]
        assert rt.execute(two, context=context).status == "failed"
        assert provider.calls == 2


def test_routing_model_cannot_receive_unapproved_agent_text(tmp_path: Path) -> None:
    from atr.models import Cost, Intent
    class RoutingModel:
        max_cost_usd = .01
        calls = 0
        def decide(self, summary: str, matches: Any) -> Any:
            self.calls += 1
            return "DEFER", Cost(category="router_llm", amount=0)
    model = RoutingModel()
    with runtime(tmp_path, route_model=model) as rt:
        context = rt.control.issue_task()
        with pytest.raises(ATRError, match="routing.*approval"):
            rt.route(Intent(root_task_id=context.root_task_id, summary="private context"), context=context)
        assert model.calls == 0
        approved = rt.control.authorize_route(context,
            Intent(root_task_id=context.root_task_id, summary="host-approved context"), business_key="route-one")
        assert rt.route(approved, context=context).model_calls == 1
        assert rt.route(approved, context=context).model_calls == 1
        assert model.calls == 1


def test_tool_operation_approval_reopens_the_reviewed_environment(tmp_path: Path) -> None:
    from test_review_repairs import Output
    class Fingerprinted(Output):
        def fingerprint(self) -> str:
            return "oci:immutable-reviewed-image"
    with runtime(tmp_path, sandbox=Fingerprinted()) as rt:
        context = rt.control.issue_task(scope=ResourceScope(tool_actions={"repair.value": ["create", "review"]}))
        candidate = program()
        rt.control.approve_program(candidate, cases=[TrustedCase(rule=rule(1))])
        rt.tool_create(CreateRequest(root_task_id=context.root_task_id, program=candidate), context=context)
        assert rt.tool_review("repair.value", context=context).passed
    with runtime(tmp_path, sandbox=Fingerprinted()) as rt:
        request = rt.control.authorize_operation(context, ExecuteRequest(root_task_id=context.root_task_id,
            mode="tool", tool_id="repair.value"), business_key="reopened-tool", rule=rule(1))
        result = rt.execute(request, context=context)
        assert result.status == "succeeded", result
