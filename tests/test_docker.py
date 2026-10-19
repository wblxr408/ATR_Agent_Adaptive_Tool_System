"""Optional real isolation acceptance. No fake backend is used in this file."""

import os
from pathlib import Path

import pytest

from atr.models import ExecuteRequest, Limits, Manifest, Permissions, Program
from atr.service import Runtime

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(os.getenv("ATR_TEST_DOCKER") != "1",
    reason="opt-in Docker boundary verification requires the prepared Linux image")]


def docker_runtime(tmp_path: Path) -> Runtime:
    work = tmp_path / "work"
    work.mkdir()
    return Runtime(workspace=work, home=tmp_path / "control", project_key="repo:docker")


def test_real_direct_program_captures_actual_numeric_result(tmp_path: Path) -> None:
    from test_acceptance import sample
    with docker_runtime(tmp_path) as rt:
        program = sample("repo:docker")
        result = rt.execute(ExecuteRequest(root_task_id="numeric", program=program,
            inputs={"numbers": [2, 8, -3]}, generated_independent=True))
        assert result.status == "succeeded", result
        assert rt.output(result) == {"sum": 7}
        assert result.candidate_id


def test_actual_control_read_workspace_write_and_raw_network_are_blocked(tmp_path: Path) -> None:
    with docker_runtime(tmp_path) as rt:
        (rt.workspace / "input.txt").write_text("original")
        source = """import json, pathlib, socket
blocked = []
for path, mode in [('/control/registry.sqlite3','r'),('/workspace/input.txt','w')]:
    try:
        with open(path, mode) as stream:
            stream.read() if mode == 'r' else stream.write('unauthorized')
        blocked.append(False)
    except OSError:
        blocked.append(True)
try:
    socket.create_connection(('1.1.1.1',443),timeout=1).close()
    blocked.append(False)
except OSError:
    blocked.append(True)
print(json.dumps({'blocked':blocked}))
"""
        program = Program(manifest=Manifest(id="project.isolation", project_key="repo:docker",
            description="Attempt reads, writes and raw network across the boundary",
            permissions=Permissions(file_read=["workspace"]),
            output_schema={"type": "object", "required": ["blocked"]}), files={"main.py": source})
        result = rt.execute(ExecuteRequest(root_task_id="boundary", program=program))
        assert result.status == "succeeded", result
        assert rt.output(result) == {"blocked": [True, True, True]}
        assert (rt.workspace / "input.txt").read_text() == "original"


def test_actual_timeout_stops_unfinished_program(tmp_path: Path) -> None:
    with docker_runtime(tmp_path) as rt:
        result = rt.execute(ExecuteRequest(root_task_id="deadline", argv=["python", "-c",
            "import time; time.sleep(20)"], limits=Limits(timeout_seconds=2)))
        assert result.status == "timed_out", result
        assert result.validation_status == "unknown"


def test_actual_output_flood_is_bounded(tmp_path: Path) -> None:
    with docker_runtime(tmp_path) as rt:
        result = rt.execute(ExecuteRequest(root_task_id="flood", argv=["python", "-c",
            "import sys; sys.stdout.write('x'*2000000)"], limits=Limits(output_bytes=4096)))
        assert result.status == "failed", result
        assert result.error_code == "output_limit"
        assert result.truncated
        assert sum(artifact.size_bytes for artifact in result.artifacts) <= 4096
