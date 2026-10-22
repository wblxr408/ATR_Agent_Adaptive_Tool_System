"""Operator issuance and bound Agent submission through real CLI parsing/storage."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_review_repairs import runtime

from atr import cli


def test_cli_issues_approves_executes_and_rejects_changed_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    monkeypatch.setattr(cli, "configured", lambda args: runtime(tmp_path))
    grant_file = tmp_path / "host-grant.json"
    grant_file.write_text(json.dumps({"max_calls": 3, "scope": {}}))
    credential = tmp_path / "host-credential.json"
    assert cli.main(["issue-task", str(grant_file), "--output", str(credential)]) == 0
    issued = json.loads(capsys.readouterr().out)
    assert "credential" not in issued
    request, acceptance = tmp_path / "request.json", tmp_path / "acceptance.json"
    request.write_text(json.dumps({"argv": ["return-null"]}))
    acceptance.write_text(json.dumps({"checks": [{"expected": None}]}))
    host = ["--task-context", str(credential)]
    assert cli.main([*host, "approve-operation", str(request), "--business-key", "one",
        "--acceptance", str(acceptance)]) == 0
    approved = json.loads(capsys.readouterr().out)
    request.write_text(json.dumps(approved))
    assert cli.main([*host, "execute", str(request)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "succeeded"
    assert result["output_trust"] == "untrusted"
    approved["argv"] = ["changed"]
    request.write_text(json.dumps(approved))
    assert cli.main([*host, "execute", str(request)]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "operation_authority"


def test_cli_rejects_acceptance_or_credentials_in_tool_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    monkeypatch.setattr(cli, "configured", lambda args: runtime(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    grant_file = tmp_path / "grant.json"
    grant_file.write_text("{}")
    assert cli.main(["issue-task", str(grant_file), "--output", str(workspace / "task.json")]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unsafe_config"
