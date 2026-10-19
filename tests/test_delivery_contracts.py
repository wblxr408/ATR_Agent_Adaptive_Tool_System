import base64
from pathlib import Path
from typing import Any

from test_acceptance import runtime, sample
from test_proxies import EvidenceAdapter, research_program

from atr.models import Check, CreateRequest, ExecuteRequest, Permissions, Program
from atr.policy import Policy
from atr.runtime.sandbox import SandboxOutcome


def test_binary_bundle_reaches_execution_unchanged_and_survives_registration(tmp_path: Path) -> None:
    payload = b'{"sum":6}'
    program = Program(manifest=sample().manifest,
                      binary_files={"payload.bin": base64.b64encode(payload).decode()})
    class FileConsumer:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            return SandboxOutcome("succeeded", 0, stdout=spec.files["payload.bin"])
    rt = runtime(tmp_path)
    rt.sandbox = FileConsumer()
    program.manifest.checks = [Check(inputs={"numbers": [1]}, expected={"sum": 6})]
    tool = rt.tool_create(CreateRequest(root_task_id="a", program=program))["tool_id"]
    assert rt.tool_review(tool).passed
    result = rt.execute(ExecuteRequest(root_task_id="b", mode="tool", tool_id=tool, inputs={"numbers": [2]}))
    assert rt.output(result) == {"sum": 6}


def test_model_secret_capability_cannot_be_omitted(tmp_path: Path) -> None:
    class Credentialed(EvidenceAdapter):
        required_secrets = ["provider-key"]
    rt = runtime(tmp_path, policy=Policy(network_capabilities=["research"], secret_capabilities=["provider-key"]),
        adapters={"research": Credentialed()})
    result = rt.execute(ExecuteRequest(root_task_id="a", program=research_program()))
    assert result.error_code == "permission_denied"


def test_review_writes_use_disposable_workspace(tmp_path: Path) -> None:
    rt = runtime(tmp_path, policy=Policy(allow_workspace_write=True))
    (rt.workspace / "value.txt").write_text("original")
    class Writer:
        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            (spec.workspace / "value.txt").write_text("changed")
            return SandboxOutcome("succeeded", 0, b'{"sum":5}')
    rt.sandbox = Writer()
    program = sample()
    program.manifest.permissions = Permissions(file_read=["workspace"], file_write=["workspace", "artifacts"])
    program.manifest.side_effect = "workspace_write"
    tool = rt.tool_create(CreateRequest(root_task_id="a", program=program))["tool_id"]
    assert rt.tool_review(tool).passed
    assert (rt.workspace / "value.txt").read_text() == "original"


def test_redaction_holds_when_reading_inside_credential_value(tmp_path: Path) -> None:
    rt = runtime(tmp_path, secrets=["long-sensitive-credential"])
    (rt.workspace / "report.txt").write_text("start long-sensitive-credential end")
    fragment = rt.read("workspace_read", path="report.txt", offset=10, limit=8)
    assert "-sensiti" not in fragment["text"]
    assert fragment["text"] == "ACTED] e"
