from pathlib import Path

from test_acceptance import ArithmeticSandbox, runtime, sample

from atr.models import CreateRequest, ExecuteRequest


def test_changed_execution_environment_is_rejected_before_reuse(tmp_path: Path) -> None:
    class MutableEnvironment(ArithmeticSandbox):
        identity = "oci:immutable-image-one"

        def fingerprint(self) -> str:
            return self.identity
    rt = runtime(tmp_path)
    backend = MutableEnvironment()
    rt.sandbox = backend
    tool = rt.tool_create(CreateRequest(root_task_id="a", program=sample()))["tool_id"]
    assert rt.tool_review(tool).passed
    backend.identity = "oci:immutable-image-two"
    result = rt.execute(ExecuteRequest(mode="tool", root_task_id="b", tool_id=tool,
                                      inputs={"numbers": [1, 3]}))
    assert result.error_code == "environment_mismatch"
