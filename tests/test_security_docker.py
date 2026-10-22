"""Current security changes at the actual Docker filesystem boundary."""

import os
from pathlib import Path
from typing import Any

import pytest

from atr.control.authority import BehaviorRule, ResourceScope, TrustedCase
from atr.models import Check, ExecuteRequest, Manifest, Permissions, Program, Settings
from atr.policy import Policy
from atr.service import Runtime

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(os.getenv("ATR_TEST_DOCKER") != "1",
    reason="requires a prepared Linux sandbox image")]


def run(rt: Runtime, candidate: Program, scope: ResourceScope, expected: Any,
        changes: dict[str, str | None] | None = None) -> Any:
    context = rt.control.issue_task(scope=scope)
    rule = BehaviorRule(checks=[Check(expected=expected)], expected_changes=changes or {})
    rt.control.approve_program(candidate, cases=[TrustedCase(rule=rule)])
    request = rt.control.authorize_operation(context,
        ExecuteRequest(root_task_id=context.root_task_id, program=candidate),
        business_key="real-boundary", rule=rule)
    return rt.execute(request, context=context)


def docker_runtime(tmp_path: Path) -> Runtime:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Runtime(workspace=workspace, home=tmp_path / "control", project_key="repo:security",
        settings=Settings(image=os.getenv("ATR_TEST_IMAGE", "atr-sandbox:0.1")),
        policy=Policy(allow_workspace_write=True))


def test_actual_task_read_scope_hides_unrelated_file(tmp_path: Path) -> None:
    with docker_runtime(tmp_path) as rt:
        (rt.workspace / "public.txt").write_text("public")
        (rt.workspace / "business.txt").write_text("private")
        candidate = Program(manifest=Manifest(id="security.read", project_key="repo:security",
            description="Read only task-approved data", side_effect="read_only",
            permissions=Permissions(file_read=["workspace"]), output_schema={"type": "object"}),
            files={"main.py": "import json,pathlib; p=pathlib.Path('.'); print(json.dumps({'text':(p/'public.txt').read_text(),'hidden':not (p/'business.txt').exists()}))"})
        result = run(rt, candidate, ResourceScope(read_paths=["public.txt"]), {"text": "public", "hidden": True})
        assert result.status == "succeeded", result
        assert rt.output(result) == {"text": "public", "hidden": True}
        assert result.output_trust == "untrusted"


def test_actual_malicious_delete_discards_all_staged_changes(tmp_path: Path) -> None:
    with docker_runtime(tmp_path) as rt:
        (rt.workspace / "allowed.txt").write_text("old")
        (rt.workspace / "business.txt").write_text("valuable")
        candidate = Program(manifest=Manifest(id="security.write", project_key="repo:security",
            description="Attempt an unauthorized deletion", side_effect="workspace_write",
            permissions=Permissions(file_read=["workspace"], file_write=["workspace"]),
            output_schema={"type": "object"}), files={"main.py": "import json,pathlib; pathlib.Path('allowed.txt').write_text('new'); pathlib.Path('business.txt').unlink(); print(json.dumps({'ok':True}))"})
        result = run(rt, candidate, ResourceScope(read_paths=["allowed.txt", "business.txt"],
            write_paths={"allowed.txt": ["update"]}), {"ok": True}, {"allowed.txt": "new"})
        assert result.error_code == "permission_denied", result
        assert (rt.workspace / "allowed.txt").read_text() == "old"
        assert (rt.workspace / "business.txt").read_text() == "valuable"


def test_actual_existing_129_file_workspace_can_create_an_authorized_file(tmp_path: Path) -> None:
    with docker_runtime(tmp_path) as rt:
        folder = rt.workspace / "project"
        folder.mkdir()
        for n in range(129):
            (folder / f"{n}.txt").write_text("data")
        candidate = Program(manifest=Manifest(id="security.large", project_key="repo:security",
            description="Edit a normal project larger than artifact file limits", side_effect="workspace_write",
            permissions=Permissions(file_read=["workspace"], file_write=["workspace"]),
            output_schema={"type": "object"}), files={"main.py": "import json,pathlib; p=pathlib.Path('project'); count=len(list(p.iterdir())); (p/'new.txt').write_text('created'); print(json.dumps({'existing':count}))"})
        result = run(rt, candidate, ResourceScope(read_paths=["project/"],
            write_paths={"project/new.txt": ["create"]}), {"existing": 129}, {"project/new.txt": "created"})
        assert result.status == "succeeded", result
        assert (folder / "new.txt").read_text() == "created"
