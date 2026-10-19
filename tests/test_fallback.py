from pathlib import Path
from typing import Any

from test_acceptance import ArithmeticSandbox, reviewed, runtime, sample

from atr.models import CreateRequest, ExecuteRequest
from atr.runtime.sandbox import SandboxOutcome


def test_observed_contract_failure_quarantines_version_and_selects_previous_without_replay(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    tool = reviewed(rt)
    update = sample()
    update.manifest.version = "v2"
    rt.tool_create(CreateRequest(root_task_id="upgrade", program=update, base_version="v1"))
    assert rt.tool_review(tool, "v2").passed
    class BrokenOnNewInput(ArithmeticSandbox):
        calls = 0

        def run(self, spec: Any, cancel: Any) -> SandboxOutcome:
            self.calls += 1
            return SandboxOutcome("succeeded", 0, b'{"sum":"incorrect type"}')
    backend = BrokenOnNewInput()
    rt.sandbox = backend
    result = rt.execute(ExecuteRequest(root_task_id="task", mode="tool", tool_id=tool, inputs={"numbers": [9]}))
    assert result.error_code == "output_contract"
    assert result.suggested_fallback.version == "v1"
    assert backend.calls == 1
    assert rt.read("tool", tool_id=tool)["manifest"]["version"] == "v1"
    assert rt.read("tool", tool_id=tool, version="v2")["state"] == "quarantined"
