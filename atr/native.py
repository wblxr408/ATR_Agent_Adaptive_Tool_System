"""Host-owned native sessions; model calls cannot grant execution trust."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from atr.control.authority import BehaviorRule, ResourceScope, TrustedCase
from atr.errors import ATRError
from atr.models import CreateRequest, ExecuteRequest, Program, Settings, canonical
from atr.native_tools import builtins
from atr.policy import Policy
from atr.runtime.native import NativeSandbox
from atr.runtime.sandbox import SandboxSpec
from atr.service import Runtime


@contextmanager
def admission_lock(home: Path) -> Iterator[None]:
    """Serialize first admission across Codex and CC, not their actual executions."""
    home.mkdir(parents=True, exist_ok=True)
    with (home / "native-admission.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 20
        while True:
            try:
                lock.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ATRError(
                        "native_busy", "native admission is busy; retry after operator setup"
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class NativeSession:
    def __init__(self, home: Path) -> None:
        self.home = home.resolve()
        with admission_lock(self.home):
            self._initialize()

    def _initialize(self) -> None:
        workspace = self.home.parent / (self.home.name + "-empty-workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        self.sandbox = NativeSandbox()
        self.runtime = Runtime(
            home=self.home,
            workspace=workspace,
            sandbox=self.sandbox,
            settings=Settings(max_task_cost=0, max_total_cost=0),
            environment=self.sandbox.fingerprint(),
        )
        self.entries = builtins()
        custom = self.home / "native-tools.json"
        if custom.exists():
            for entry in json.loads(custom.read_text(encoding="utf-8")):
                self.entries.append(
                    (
                        Program.model_validate(entry["program"]),
                        [TrustedCase.model_validate(c) for c in entry["cases"]],
                        BehaviorRule.model_validate(entry["call_rule"]),
                    )
                )
        self.rules: dict[str, tuple[str, BehaviorRule]] = {}
        setup = self.runtime.control.issue_task(
            scope=ResourceScope(
                tool_actions={p.manifest.id: ["create", "review"] for p, _, _ in self.entries}
            ),
            max_calls=10000,
            max_creations=8,
        )
        try:
            for program, cases, rule in self.entries:
                self.sandbox.approve(program)
                self.runtime.control.approve_program(program, cases=cases)
                try:
                    current, row = self.runtime.registry.program(
                        program.manifest.id, program.manifest.version
                    )
                    expected = program.model_copy(deep=True)
                    expected.manifest.environment = self.sandbox.fingerprint()
                    if canonical(current) != canonical(expected):
                        raise ATRError(
                            "immutable_version", "native bundle changed; register a new version"
                        )
                except ATRError as exc:
                    if exc.code != "not_found":
                        raise
                    active = self.runtime.store.one(
                        "SELECT version FROM active WHERE id=?", (program.manifest.id,)
                    )
                    self.runtime.tool_create(
                        CreateRequest(
                            root_task_id=setup.root_task_id,
                            program=program,
                            base_version=active["version"] if active else None,
                        ),
                        context=setup,
                    )
                    _, row = self.runtime.registry.program(
                        program.manifest.id, program.manifest.version
                    )
                if not row["reviewed"]:
                    report = self.runtime.tool_review(
                        program.manifest.id, program.manifest.version, context=setup
                    )
                    if not report.passed:  # type: ignore[union-attr]
                        raise ATRError("native_acceptance", "operator native acceptance failed")
                bound, row = self.runtime.registry.program(
                    program.manifest.id, program.manifest.version
                )
                self.runtime.control.acceptance(bound)
                if row["state"] in ("temporary", "persistent"):
                    self.rules[program.manifest.id] = (program.manifest.version, rule)
            self.context = self.runtime.control.issue_task(
                max_calls=512, ttl_seconds=28800, max_creations=0
            )
        except BaseException:
            self.runtime.close()
            raise
        finally:
            if not self.runtime.closed:
                self.runtime.control.revoke_task(setup)

    def __enter__(self) -> NativeSession:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self.runtime.closed:
            try:
                self.runtime.control.revoke_task(self.context)
            except ATRError as exc:
                if exc.code != "task_expired":
                    raise
            finally:
                self.runtime.close()

    def search(self, query: str) -> list[dict[str, Any]]:
        self.runtime.control.authenticate(self.context)
        self.runtime.control.consume_call(self.context)
        if not query or len(query) > 4096:
            return []
        matches = self.runtime.registry.search(query, limit=10, side_effect="pure")
        if "csv" in query.casefold():
            matches.sort(key=lambda m: m["id"] != "native.csv")
        result = []
        for match in matches:
            if match["entity"] != "tool" or match["id"] not in self.rules:
                continue
            program, _ = self.runtime.registry.program(match["id"], self.rules[match["id"]][0])
            result.append(
                {
                    "tool_id": match["id"],
                    "version": program.manifest.version,
                    "description": program.manifest.description[:180],
                    "input_schema": program.manifest.input_schema,
                }
            )
        return result[:3]

    def run(self, tool_id: str, inputs: dict[str, Any], business_key: str) -> dict[str, Any]:
        try:
            self.runtime.control.authenticate(self.context)
            if tool_id not in self.rules:
                raise ATRError(
                    "native_not_approved", "tool is not in the operator native allowlist"
                )
            if len(canonical(inputs).encode("utf-8")) > 131072:
                raise ATRError("input_limit", "native inline input exceeds 128 KiB")
            version, rule = self.rules[tool_id]
            request = ExecuteRequest(
                root_task_id=self.context.root_task_id,
                mode="tool",
                tool_id=tool_id,
                version=version,
                inputs=inputs,
            )
            approved = self.runtime.control.authorize_operation(
                self.context, request, business_key=business_key, rule=rule
            )
            result = self.runtime.execute(approved, context=self.context)
            response: dict[str, Any] = {
                "run_id": result.run_id,
                "status": result.status,
                "output_trust": "untrusted",
                "error": result.error_code,
            }
            if result.status == "succeeded":
                response["output"] = self.runtime.output(result)
            return response
        except (ATRError, ValueError) as exc:
            return {
                "status": "failed",
                "error": exc.code if isinstance(exc, ATRError) else "invalid_input",
            }


def register(home: Path, entry: dict[str, Any]) -> None:
    """Operator-only command. Registering code grants host-level trust."""
    with admission_lock(home):
        _register(home, entry)


def _register(home: Path, entry: dict[str, Any]) -> None:
    program = Program.model_validate(entry["program"])
    if program.manifest.id.startswith("native.") or program.manifest.scope != "global":
        raise ATRError("native_policy", "custom tools need a non-native ID and global scope")
    NativeSandbox().approve(program)
    cases = [TrustedCase.model_validate(case) for case in entry["cases"]]
    if not 1 <= len(cases) <= 64:
        raise ATRError("native_acceptance", "one to 64 independent operator cases required")
    rule = BehaviorRule.model_validate(entry["call_rule"])
    if (
        any(c.rule.expected_changes or c.rule.adapter_calls for c in cases)
        or rule.expected_changes
        or rule.adapter_calls
    ):
        raise ATRError("native_policy", "native tools cannot publish files or call adapters")
    # Validate independently supplied examples before altering the active allowlist.
    runner = NativeSandbox()
    runner.approve(program)
    with tempfile.TemporaryDirectory(prefix="atr-native-acceptance-") as temporary:
        for case in cases:
            try:
                Draft202012Validator(program.manifest.input_schema).validate(case.inputs)
                outcome = runner.run(
                    SandboxSpec(
                        program.manifest.entrypoint,
                        canonical(case.inputs).encode(),
                        program.sandbox_files(),
                        Path(temporary),
                        program.manifest.permissions,
                        program.manifest.runtime,
                        Policy(),
                        65536,
                    ),
                    threading.Event(),
                )
                if outcome.status != "succeeded":
                    raise ValueError("execution failed")
                Draft202012Validator(program.manifest.output_schema).validate(
                    json.loads(outcome.stdout)
                )
                case.rule.validate_output(outcome.stdout)
            except Exception as exc:
                raise ATRError(
                    "native_acceptance", "operator acceptance failed; allowlist unchanged"
                ) from exc
    home.mkdir(parents=True, exist_ok=True)
    path = home / "native-tools.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    proposed = {
        "program": program.model_dump(mode="json"),
        "cases": [case.model_dump(mode="json") for case in cases],
        "call_rule": rule.model_dump(mode="json"),
    }
    for saved in previous:
        if saved["program"]["manifest"]["id"] == program.manifest.id:
            if canonical(saved) == canonical(proposed):
                return
            if int(program.manifest.version[1:]) <= int(
                saved["program"]["manifest"]["version"][1:]
            ):
                raise ATRError(
                    "immutable_version", "changed native tools require an increased version"
                )
    updated = [
        item for item in previous if item["program"]["manifest"]["id"] != program.manifest.id
    ]
    updated.append(proposed)
    from atr.client_install import write_file

    write_file(path, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
