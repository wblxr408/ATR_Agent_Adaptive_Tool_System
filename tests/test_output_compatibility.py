from pathlib import Path

from test_acceptance import reviewed, runtime, sample

from atr.models import Intent


def test_router_filters_incompatible_output_contract(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    reviewed(rt)
    common = {"root_task_id": "a", "tool_id": "project.sum", "summary": "sum", "inputs": {"numbers": [1]}}
    assert rt.route(Intent(**common, output_schema=sample().manifest.output_schema)).decision == "REUSE"
    route = rt.route(Intent(**common, output_schema={"type": "array"}))
    assert route.decision == "DEFER"
    assert route.selected is None
