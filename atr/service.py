"""Stable five-interface service; control metadata is never accessible to tool code."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from atr.accounting import Ledger
from atr.adapters import Adapter
from atr.control.lifecycle import Lifecycle
from atr.control.registry import ACTIVE, RESTRICTED, Registry, signature
from atr.errors import ATRError
from atr.models import (
    Cost,
    CreateRequest,
    Dependency,
    ExecuteRequest,
    ExecutionResult,
    Intent,
    Json,
    Program,
    ReviewReport,
    Settings,
    State,
    canonical,
    utcnow,
)
from atr.policy import Policy, default_home, ensure_separate
from atr.router import RouteModel, Router
from atr.runtime.cancellation import Cancellation
from atr.runtime.sandbox import DockerSandbox, Sandbox, SandboxOutcome, SandboxSpec, snapshot
from atr.storage import Redactor, Store, digest


class Runtime:
    def __init__(self, *, workspace: Path, home: Path | None = None,
                 project_key: str | None = None, settings: Settings | None = None,
                 policy: Policy | None = None, sandbox: Sandbox | None = None,
                 clock: Callable[[], datetime] = utcnow, environment: str | None = None,
                 adapters: dict[str, Adapter] | None = None, secrets: list[str] | None = None,
                 route_model: RouteModel | None = None) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.home = (home or default_home() / digest(str(self.workspace))[:16]).resolve()
        ensure_separate(self.home, self.workspace)
        self.settings, self.policy, self.clock = settings or Settings(), (policy or Policy()).model_copy(deep=True), clock
        package = Path(__file__).resolve().parent
        if package.is_relative_to(self.workspace):
            self.policy.protected_relative_paths.append(package.relative_to(self.workspace).as_posix())
        self.project_key = project_key or "repo:" + digest(str(self.workspace))[:24]
        self.environment = environment or "oci:" + self.settings.image
        self.environment_override = environment
        redacted_secrets = list(secrets or [])
        for adapter in (adapters or {}).values():
            redacted_secrets.extend(getattr(adapter, "secrets_for_redaction", []))
        self.redactor = Redactor(redacted_secrets)
        self.store = Store(self.home, self.redactor)
        self.registry = Registry(self.store, self.policy, self.settings,
                                 self.project_key, self.environment, clock)
        self.lifecycle = Lifecycle(self.registry)
        self.ledger = Ledger(self.store, clock, self.settings.max_task_cost)
        self.router = Router(self.registry, route_model, self.ledger)
        self.sandbox, self.adapters = sandbox or DockerSandbox(self.settings.image), adapters or {}
        self.environment_resolved = environment is not None or not hasattr(self.sandbox, "fingerprint")
        self.executor = ThreadPoolExecutor(max_workers=self.settings.max_concurrency,
                                           thread_name_prefix="atr-run")
        self.cancel_flags: dict[str, threading.Event] = {}
        self.flag_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(self.settings.max_concurrency)
        self.maintenance_stop = threading.Event()
        self.maintenance_thread: threading.Thread | None = None

    def close(self) -> None:
        self.maintenance_stop.set()
        if self.maintenance_thread:
            self.maintenance_thread.join(timeout=10)
        with self.flag_lock:
            for flag in self.cancel_flags.values():
                flag.set()
        self.executor.shutdown(wait=True)
        self.store.close()

    def start_maintenance(self) -> None:
        if self.maintenance_thread:
            return

        def work() -> None:
            while not self.maintenance_stop.is_set():
                try:
                    self.lifecycle.tick()
                except Exception as exc:
                    self.store.emit("maintenance_failed", self.clock(), error=type(exc).__name__)
                self.maintenance_stop.wait(60)
        self.maintenance_thread = threading.Thread(target=work, name="atr-lifecycle", daemon=True)
        self.maintenance_thread.start()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _begin(self, request: ExecuteRequest, *, parent: str | None = None,
               review: bool = False) -> tuple[str, ExecutionResult | None]:
        now, run_id = self.clock(), "run_" + uuid4().hex
        result = ExecutionResult(run_id=run_id, mode=request.mode, tool_id=request.tool_id,
                                 version=request.version, status="running")
        payload = digest(canonical(request.model_dump(mode="json", exclude={"background"})))
        with self.store.transaction() as db:
            if request.operation_id:
                previous = db.execute("SELECT * FROM runs WHERE task=? AND operation=?",
                                      (request.root_task_id, request.operation_id)).fetchone()
                if previous:
                    if previous["payload"] != payload:
                        raise ATRError("operation_conflict", "operation_id is bound to another request")
                    return previous["id"], ExecutionResult.model_validate_json(previous["result"])
            if request.retry_of:
                previous = db.execute("SELECT * FROM runs WHERE id=?", (request.retry_of,)).fetchone()
                if not previous or previous["task"] != request.root_task_id:
                    raise ATRError("invalid_retry", "retry target must belong to the same root task")
            db.execute("""INSERT INTO runs
                (id,task,operation,payload,tool_id,version,parent,input_key,retry_of,
                 cache_hit,review_run,started,result,dynamic) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    run_id, request.root_task_id, request.operation_id, payload,
                    request.tool_id, request.version, parent, digest(canonical(request.inputs)),
                    request.retry_of, int(request.cache_hit), int(review), now.timestamp(), canonical(result),
                    int(request.mode == "tool" or request.program is not None)))
            new_task = db.execute("INSERT OR IGNORE INTO tasks VALUES (?,?)", (request.root_task_id, now.timestamp())).rowcount
        if new_task:
            self.store.emit("task_started", now, task=request.root_task_id)
        self.store.emit("execution_started", now, task=request.root_task_id, run_id=run_id,
                        tool_id=request.tool_id, version=request.version, parent_run_id=parent)
        return run_id, None

    def _refresh_environment(self, *, required: bool = False) -> None:
        if self.environment_override is not None:
            return
        identify = getattr(self.sandbox, "fingerprint", None)
        if identify:
            try:
                self.environment = identify()
                self.registry.environment = self.environment
                self.environment_resolved = True
            except ATRError:
                if required:
                    raise

    def _bind_program(self, program: Program) -> Program:
        if program.manifest.environment is None and self.environment_resolved:
            program = program.model_copy(deep=True)
            program.manifest.environment = self.environment
        return program

    def execute(self, request: ExecuteRequest | Json) -> ExecutionResult:
        request = ExecuteRequest.model_validate(request.model_dump(mode="json") if isinstance(request, ExecuteRequest) else request)
        self.lifecycle.tick()
        try:
            run_id, previous = self._begin(request)
        except ATRError as exc:
            return ExecutionResult(run_id="", mode=request.mode, status="failed",
                                   error_code=exc.code, summary=str(exc))
        if previous:
            return previous
        if request.background:
            self.executor.submit(self._run, request, run_id)
            return ExecutionResult(run_id=run_id, mode=request.mode, tool_id=request.tool_id,
                                   version=request.version, status="running")
        return self._run(request, run_id)

    def _run(self, request: ExecuteRequest, run_id: str, *, parent: str | None = None,
             review: bool = False, supplied_program: Program | None = None,
             depth: int = 0, workspace_override: Path | None = None,
             deadline: float | None = None) -> ExecutionResult:
        started = time.monotonic()
        result = ExecutionResult(run_id=run_id, mode=request.mode, tool_id=request.tool_id,
                                 version=request.version, status="running")
        flag = Cancellation(self.store, run_id)
        with self.flag_lock:
            self.cancel_flags[run_id] = flag
        acquired = project_locked = False
        program: Program | None = None
        row: dict[str, Any] | None = None
        review_area: tempfile.TemporaryDirectory[str] | None = None
        execution_workspace = workspace_override or self.workspace
        try:
            if depth > 16:
                raise ATRError("dependency_depth", "maximum workflow depth exceeded")
            if self.store.one("SELECT id FROM runs WHERE id=? AND cancel_requested=1", (run_id,)):
                flag.set()
            if (request.mode == "tool" or request.program or supplied_program) and not self.store.enabled("dynamic_execute"):
                raise ATRError("dynamic_disabled", "dynamic execution has been disabled")
            preview = supplied_program or request.program
            if request.mode == "tool":
                assert request.tool_id
                preview, _ = self.registry.program(request.tool_id, request.version)
            if preview is None or preview.manifest.kind == "script":
                self._refresh_environment(required=True)
            if request.program:
                request = request.model_copy(update={"program": self._bind_program(request.program)})
            if not review and request.mode == "direct":
                request = self._reuse_or_observe(request)
                result.mode = request.mode
            if not parent:
                acquired = self.slots.acquire(blocking=False)
                if not acquired:
                    raise ATRError("concurrency_limit", "concurrent execution limit reached")
            if supplied_program:
                program = supplied_program
            elif request.mode == "tool":
                assert request.tool_id
                program, row = self.registry.program(request.tool_id, request.version)
                if row["state"] in RESTRICTED or (row["state"] not in ACTIVE and not parent) or not row["reviewed"]:
                    raise ATRError("review_required", "inactive or unreviewed tool requires review")
                review_environment = json.loads(row["review"]).get("environment") if row["review"] else None
                if review_environment is not None and review_environment != self.environment:
                    raise ATRError("environment_mismatch", "execution environment differs from reviewed environment")
            else:
                program = request.program
            manifest = program.manifest if program else None
            permissions = manifest.permissions if manifest else request.permissions
            limits = manifest.runtime if manifest else request.limits
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ATRError("timed_out", "parent workflow deadline exceeded")
                limits = limits.model_copy(update={"timeout_seconds": min(limits.timeout_seconds, remaining)})
            deadline = time.monotonic() + limits.timeout_seconds
            side_effect = manifest.side_effect if manifest else request.side_effect
            if manifest:
                self.policy.validate_manifest(manifest, self.project_key, self.environment)
                Draft202012Validator(manifest.input_schema).validate(request.inputs)
                result.tool_id, result.version = manifest.id, manifest.version
                with self.store.transaction() as db:
                    db.execute("UPDATE runs SET tool_id=?,version=?,epoch=? WHERE id=?",
                               (manifest.id if request.mode == "tool" or review else None, manifest.version,
                                row["epoch"] if row else 0, run_id))
            else:
                self.policy.authorize(permissions, side_effect, limits)
            if side_effect in ("workspace_write", "external_write") and not request.operation_id:
                raise ATRError("operation_required", "side effects require operation_id")
            if request.retry_of and side_effect in ("workspace_write", "external_write"):
                raise ATRError("unsafe_retry", "side effects cannot be replayed as an automatic retry")
            if review and side_effect == "external_write" and not self.policy.allow_external_review:
                raise ATRError("external_review_denied", "external side-effect review requires a distinct operator grant")
            if review and not parent and side_effect == "workspace_write":
                review_area = tempfile.TemporaryDirectory(prefix="atr-review-")
                execution_workspace = Path(review_area.name)
                snapshot(self.workspace, execution_workspace, self.policy, self.settings.max_workspace_bytes)
            if side_effect == "workspace_write" and not parent:
                with self.store.transaction() as db:
                    if db.execute("SELECT 1 FROM project_locks WHERE project=?", (self.project_key,)).fetchone():
                        raise ATRError("workspace_busy", "another execution owns the project write lock")
                    db.execute("INSERT INTO project_locks VALUES (?,?)", (self.project_key, run_id))
                project_locked = True
            if flag.is_set():
                raise ATRError("cancelled", "execution cancelled")
            if manifest and manifest.kind == "workflow":
                assert program
                output = self._workflow(program, request, run_id, flag, depth, review, execution_workspace, deadline)
                outcome = SandboxOutcome("succeeded", 0, canonical(output).encode())
            elif manifest and manifest.kind in ("adapter", "model_pipeline"):
                output = self._adapter(program, request, run_id, flag)
                outcome = SandboxOutcome("succeeded", 0, canonical(output).encode())
            else:
                argv = manifest.entrypoint if manifest else request.argv
                assert argv is not None
                outcome = self.sandbox.run(SandboxSpec(
                    argv, canonical(request.inputs).encode(), program.sandbox_files() if program else {},
                    execution_workspace, permissions, limits, self.policy, self.settings.max_workspace_bytes,
                    self.environment if hasattr(self.sandbox, "fingerprint") and not self.environment_override else None), flag)
            if time.monotonic() > deadline:
                outcome.status, outcome.error_code = "timed_out", "timed_out"
            if flag.is_set():
                outcome.status = "cancelled"
            if len(outcome.stdout) + len(outcome.stderr) > limits.output_bytes:
                outcome.status, outcome.error_code, outcome.truncated = "failed", "output_limit", True
                outcome.stdout = outcome.stdout[:limits.output_bytes]
                outcome.stderr = outcome.stderr[:max(0, limits.output_bytes - len(outcome.stdout))]
            if sum(len(v) for v in outcome.files.values()) > limits.artifact_bytes:
                raise ATRError("artifact_limit", "run artifacts exceed configured byte budget")
            result.status, result.exit_code = outcome.status, outcome.exit_code
            result.error_code, result.truncated = outcome.error_code, outcome.truncated
            stdout = self.redactor.text(outcome.stdout.decode("utf-8", errors="replace"))
            result.stdout_excerpt = stdout[:self.settings.excerpt_chars]
            result.truncated |= len(stdout) > self.settings.excerpt_chars
            if outcome.stdout:
                result.artifacts.append(self.store.artifact(run_id, "stdout", outcome.stdout, self.clock()))
            if outcome.stderr:
                result.artifacts.append(self.store.artifact(run_id, "stderr", outcome.stderr, self.clock()))
            for name, content in outcome.files.items():
                result.artifacts.append(self.store.artifact(run_id, name, content, self.clock()))
            if outcome.status == "succeeded" and manifest and manifest.output_schema:
                try:
                    Draft202012Validator(manifest.output_schema).validate(json.loads(outcome.stdout))
                    result.validation_status = "passed"
                except (ValidationError, ValueError, UnicodeError):
                    result.validation_status, result.status = "failed", "failed"
                    result.error_code = "output_contract"
            result.summary = ("execution completed; business correctness requires task-level evidence"
                              if result.status == "succeeded" else "execution did not complete successfully")
            if (request.generated_independent and result.status == "succeeded" and program
                    and not review and not request.retry_of and not request.cache_hit):
                result.candidate_id = self.registry.capture(program, request.root_task_id, run_id)
        except ValidationError:
            result.status, result.error_code = "failed", "input_contract"
            result.summary = "inputs do not satisfy tool schema"
        except ATRError as exc:
            result.status = "cancelled" if exc.code == "cancelled" else ("timed_out" if exc.code == "timed_out" else "failed")
            result.error_code, result.summary = exc.code, self.redactor.text(str(exc))
            if exc.code == "permission_denied":
                self.store.emit("permission_denied", self.clock(), task=request.root_task_id, run_id=run_id)
        except Exception as exc:
            result.status, result.error_code = "failed", "internal_error"
            result.summary = "runtime failure: " + type(exc).__name__
        finally:
            result.duration_ms = int((time.monotonic() - started) * 1000)
            finished, eligible = self.clock(), False
            if row and result.error_code == "output_contract" and not review:
                with self.store.transaction() as db:
                    db.execute("UPDATE tools SET state='quarantined',reviewed=0 WHERE id=? AND version=?",
                               (row["id"], row["version"]))
                    db.execute("DELETE FROM search WHERE entity='tool' AND id=? AND version=?", (row["id"], row["version"]))
                alternatives = self.store.rows("SELECT version FROM tools WHERE id=? AND reviewed=1 "
                    "AND state IN ('temporary','persistent')", (row["id"],))
                older = sorted([r["version"] for r in alternatives if int(r["version"][1:]) < int(row["version"][1:])],
                               key=lambda value: int(value[1:]), reverse=True)
                if older:
                    try:
                        self.registry.rollback(row["id"], older[0])
                        result.suggested_fallback = Dependency(tool_id=row["id"], version=older[0])
                    except ATRError:
                        self.store.emit("fallback_unavailable", self.clock(), task=request.root_task_id,
                                        run_id=run_id, tool_id=row["id"], version=older[0])
            if row and program and not review:
                window_end = row["window_start"] + timedelta(days=self.settings.temporary_days).total_seconds()
                eligible = (result.status == "succeeded" and result.validation_status == "passed"
                            and not request.retry_of and not request.cache_hit
                            and (row["state"] == State.PERSISTENT or finished.timestamp() <= window_end))
            with self.store.transaction() as db:
                cancelled = db.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
                if cancelled and cancelled[0]:
                    result.status, eligible = "cancelled", False
                db.execute("UPDATE runs SET result=?,finished=?,eligible=? WHERE id=?",
                           (canonical(result), finished.timestamp(), int(eligible), run_id))
                if eligible and row:
                    db.execute("UPDATE tools SET last_used=? WHERE id=? AND version=?",
                               (finished.timestamp(), row["id"], row["version"]))
                if project_locked:
                    db.execute("DELETE FROM project_locks WHERE project=? AND run_id=?", (self.project_key, run_id))
            if acquired:
                self.slots.release()
            if review_area:
                review_area.cleanup()
            with self.flag_lock:
                self.cancel_flags.pop(run_id, None)
            self.ledger.record(request.root_task_id, Cost(category="compute", amount=(
                (time.monotonic() - started) * self.settings.compute_usd_per_second
                if self.settings.compute_usd_per_second is not None else None),
                estimated=self.settings.compute_usd_per_second is not None), run_id)
            retained_bytes = sum(artifact.size_bytes for artifact in result.artifacts)
            self.ledger.record(request.root_task_id, Cost(category="storage", amount=(
                retained_bytes / 1_048_576 * self.settings.storage_usd_per_mb
                if self.settings.storage_usd_per_mb is not None else None),
                estimated=self.settings.storage_usd_per_mb is not None), run_id)
            self.store.emit("execution_finished", finished, task=request.root_task_id,
                            run_id=run_id, tool_id=result.tool_id, version=result.version,
                            status=result.status, validation_status=result.validation_status,
                            duration_ms=result.duration_ms, eligible=eligible, parent_run_id=parent)
        if row and not review:
            self.lifecycle.tick()
        return result

    def _adapter(self, program: Program | None, request: ExecuteRequest, run_id: str,
                 flag: threading.Event) -> Any:
        assert program
        manifest = program.manifest
        capability = manifest.adapter
        adapter = self.adapters.get(capability or "")
        if not adapter or capability not in manifest.permissions.network:
            raise ATRError("adapter_unavailable", "tool requires an operator-configured network capability")
        if not set(adapter.required_secrets) <= set(manifest.permissions.secrets):
            raise ATRError("permission_denied", "tool must declare the adapter's secret capabilities")
        maximum = adapter.max_cost_usd
        reservation = self.ledger.reserve(request.root_task_id, maximum, run_id)
        recorded = False
        complete = False
        try:
            response = adapter.call(request.inputs, timeout=manifest.runtime.timeout_seconds,
                                    cancel=flag, now=self.clock())
            total = 0.0
            for index, cost in enumerate(response.costs):
                self.ledger.record(request.root_task_id, cost, run_id, entry_id=f"{reservation}:{index}")
                if cost.currency != "USD":
                    raise ATRError("billing_currency", "budgeted adapters must report USD")
                if cost.amount is not None:
                    total += cost.amount
            recorded = bool(response.costs)
            complete = recorded and all(c.amount is not None and not c.estimated for c in response.costs)
            if total > maximum:
                raise ATRError("adapter_budget", "adapter exceeded its declared maximum charge")
            return response.output
        finally:
            if not recorded:
                self.ledger.record(request.root_task_id, Cost(category=(
                    "nested_llm" if manifest.kind == "model_pipeline" else "external_api"),
                    provider=capability), run_id, entry_id=reservation + ":unknown")
            self.ledger.release(reservation, billing_complete=complete)

    def _reuse_or_observe(self, request: ExecuteRequest) -> ExecuteRequest:
        program = request.program
        if not program or not request.generated_independent:
            self.store.emit("route_decided", self.clock(), task=request.root_task_id,
                            decision="DIRECT", reason="ordinary command or explicit task program", model_calls=0)
            return request
        matches = self.registry.search(program.manifest.id, inputs=request.inputs,
                                       side_effect=program.manifest.side_effect)
        selected = None
        for match in matches:
            other, _ = (self.registry.candidate(match["id"]) if match["entity"] == "candidate"
                        else self.registry.program(match["id"], match["version"]))
            if signature(other) != signature(program):
                continue
            if match["entity"] == "candidate":
                created = self.tool_create(CreateRequest(root_task_id=request.root_task_id,
                                                        candidate_id=match["id"]))
                report = self.tool_review(created["tool_id"], created["version"], root_task_id=request.root_task_id)
                if not isinstance(report, ReviewReport) or not report.passed:
                    # Original program is still available for a controlled Direct attempt.
                    break
                selected = created
            else:
                selected = {"tool_id": match["id"], "version": match["version"]}
            break
        self.store.emit("route_decided", self.clock(), task=request.root_task_id,
                        decision="REUSE" if selected else "EXECUTE_AND_OBSERVE", model_calls=0,
                        retrieved=[m["id"] for m in matches], selected=selected)
        if selected:
            return request.model_copy(update={"mode": "tool", "tool_id": selected["tool_id"],
                "version": selected["version"], "program": None, "generated_independent": False})
        return request

    @staticmethod
    def _resolve(value: Any, inputs: Json, outputs: Json) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$input"}:
                selected: Any = inputs
                path = value["$input"]
            elif set(value) == {"$step"}:
                selected, path = outputs, value["$step"]
            else:
                return {k: Runtime._resolve(v, inputs, outputs) for k, v in value.items()}
            if not isinstance(path, str):
                raise ATRError("workflow_input", "workflow references must be dotted paths")
            try:
                for component in path.split(".") if path else []:
                    selected = selected[component]
            except (KeyError, TypeError) as exc:
                raise ATRError("workflow_input", "workflow reference is unavailable") from exc
            return selected
        if isinstance(value, list):
            return [Runtime._resolve(v, inputs, outputs) for v in value]
        return value

    def _workflow(self, program: Program, request: ExecuteRequest, run_id: str,
                  flag: threading.Event, depth: int, review: bool, workspace: Path,
                  deadline: float) -> Json:
        outputs: Json = {}
        started = time.monotonic()
        for step in program.manifest.steps:
            if flag.is_set():
                raise ATRError("cancelled", "workflow cancelled")
            if time.monotonic() - started > program.manifest.runtime.timeout_seconds:
                raise ATRError("timed_out", "workflow deadline exceeded")
            child = ExecuteRequest(root_task_id=request.root_task_id, mode="tool",
                                   tool_id=step.tool_id, version=step.version,
                                   inputs=self._resolve(step.inputs, request.inputs, outputs),
                                   operation_id=f"{run_id}:{step.name}")
            child_id, previous = self._begin(child, parent=run_id, review=review)
            result = previous or self._run(child, child_id, parent=run_id, depth=depth + 1,
                review=review, workspace_override=workspace, deadline=deadline)
            if result.status != "succeeded" or result.validation_status != "passed":
                raise ATRError("dependency_failed", "workflow step failed its execution contract")
            outputs[step.name] = self.output(result)
        return outputs

    def output(self, result: ExecutionResult) -> Any:
        artifact = next((a for a in result.artifacts if a.name == "stdout"), None)
        return json.loads(self.store.get(artifact.ref)) if artifact else None

    def read(self, operation: str, **kwargs: Any) -> Any:
        if operation == "registry_search":
            self.lifecycle.tick()
            self._refresh_environment()
            return self.registry.search(**kwargs)
        if operation == "tool_source":
            program, _ = self.registry.program(kwargs["tool_id"], kwargs.get("version"))
            name, offset, limit = kwargs["path"], kwargs.get("offset", 0), kwargs.get("limit", 4096)
            if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 16000:
                raise ATRError("invalid_range", "invalid source read range")
            if name not in program.files:
                raise ATRError("not_found", "text source file is unavailable")
            content = self.redactor.text(program.files[name]).encode("utf-8")
            return {"text": content[offset:offset + limit].decode("utf-8", errors="replace"),
                    "next_offset": offset + min(limit, max(0, len(content) - offset)),
                    "truncated": offset + limit < len(content)}
        if operation == "workspace_read":
            path = self.policy.readable_path(self.workspace, kwargs["path"])
            offset, limit = kwargs.get("offset", 0), kwargs.get("limit", 4096)
            if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 16000:
                raise ATRError("invalid_range", "invalid read range")
            if not path.is_file():
                raise ATRError("not_found", "workspace file is unavailable")
            if path.stat().st_size > self.settings.max_workspace_bytes:
                raise ATRError("read_limit", "file exceeds the configured safe-read byte budget")
            redacted = self.redactor.text(path.read_bytes().decode("utf-8", errors="replace")).encode("utf-8")
            content = redacted[offset:offset + limit]
            return {"text": content.decode("utf-8", errors="replace"),
                    "next_offset": offset + len(content), "truncated": offset + len(content) < len(redacted)}
        if operation == "run_output":
            return self.store.read_artifact(**kwargs)
        if operation == "run":
            row = self.store.one("SELECT result FROM runs WHERE id=?", (kwargs["run_id"],))
            if not row:
                raise ATRError("not_found", "run not found")
            return json.loads(row["result"])
        if operation == "tool":
            program, row = self.registry.program(kwargs["tool_id"], kwargs.get("version"))
            return {"manifest": program.manifest.model_dump(mode="json"), "state": row["state"],
                    "review": json.loads(row["review"]) if row["review"] else None,
                    "stats": self.registry.stats(row["id"], row["version"])}
        if operation == "candidate":
            program, row = self.registry.candidate(kwargs["candidate_id"])
            return {"manifest": program.manifest.model_dump(mode="json"), "state": row["state"]}
        raise ATRError("invalid_operation", "unknown read operation")

    def tool_create(self, request: CreateRequest | Json) -> Json:
        request = CreateRequest.model_validate(request.model_dump(mode="json") if isinstance(request, CreateRequest) else request)
        if not self.store.enabled("auto_create"):
            raise ATRError("creation_disabled", "tool creation is paused")
        if request.candidate_id:
            program, _ = self.registry.candidate(request.candidate_id)
        else:
            assert request.program
            program = request.program
        self._refresh_environment()
        program = self._bind_program(program)
        matches = self.registry.search(program.manifest.id, include_inactive=True)
        self.store.emit("creation_retrieved", self.clock(), task=request.root_task_id,
                        retrieved=[m["id"] for m in matches])
        return self.registry.create(program, request.root_task_id, request.base_version)

    def tool_review(self, tool_id: str, version: str | None = None, *,
                    root_task_id: str | None = None, action: str = "review") -> ReviewReport | Json:
        if action == "rollback":
            if not version:
                raise ATRError("version_required", "rollback requires a pinned version")
            return self.registry.rollback(tool_id, version)
        program, row = self.registry.program(tool_id, version)
        if action == "evidence":
            return json.loads(row["review"]) if row["review"] else {"passed": False, "errors": ["not reviewed"]}
        if action not in ("review", "restore"):
            raise ATRError("invalid_operation", "unknown review action")
        manifest, task = program.manifest, root_task_id or "review:" + tool_id
        report = ReviewReport(tool_id=tool_id, version=manifest.version, checked_at=self.clock(), passed=False)
        try:
            if not self.store.enabled("dynamic_execute"):
                raise ATRError("dynamic_disabled", "review execution is disabled")
            self.policy.validate_manifest(manifest, self.project_key, self.environment)
            self.registry._dependencies(manifest)
            if not manifest.checks:
                raise ATRError("checks_required", "at least one expected-output contract case is required")
            for check in manifest.checks:
                request = ExecuteRequest(root_task_id=task, program=program,
                                         inputs=check.inputs, operation_id="review:" + uuid4().hex)
                run_id, _ = self._begin(request, review=True)
                result = self._run(request, run_id, review=True, supplied_program=program)
                actual = self.output(result) if result.status == "succeeded" else None
                found = True
                if check.expected_path and actual is not None:
                    try:
                        for component in check.expected_path.split("."):
                            actual = actual[component]
                    except (KeyError, TypeError):
                        found = False
                passed = result.status == "succeeded" and found and actual == check.expected
                report.evidence.append({"name": check.name, "run_id": run_id, "passed": passed,
                                        "status": result.status, "validation_status": result.validation_status})
                if not passed:
                    report.errors.append(f"contract case failed: {check.name}")
            report.passed = not report.errors
        except (ATRError, ValueError) as exc:
            report.errors.append(self.redactor.text(str(exc)))
        now = self.clock()
        report.environment = self.environment
        with self.store.transaction() as db:
            state = row["state"]
            restoring = state in (State.ARCHIVED, State.DORMANT, State.DISABLED, State.QUARANTINED)
            if report.passed:
                state = State.PERSISTENT if row["state"] == State.DORMANT else (State.TEMPORARY if restoring else state)
                if restoring:
                    db.execute("UPDATE tools SET epoch=epoch+1,window_start=?,last_used=?,archived=NULL "
                               "WHERE id=? AND version=?", (now.timestamp(), now.timestamp(), tool_id, manifest.version))
                active = db.execute("SELECT version FROM active WHERE id=?", (tool_id,)).fetchone()
                if not active or int(manifest.version[1:]) > int(active["version"][1:]) or action == "restore":
                    db.execute("INSERT INTO active VALUES (?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                               (tool_id, manifest.version))
                db.execute("DELETE FROM search WHERE entity='tool' AND id=? AND version=?", (tool_id, manifest.version))
                db.execute("INSERT INTO search VALUES (?,?,?,?)", ("tool", tool_id, manifest.version, self.registry.body(manifest)))
            else:
                state = State.QUARANTINED
                db.execute("DELETE FROM search WHERE entity='tool' AND id=? AND version=?", (tool_id, manifest.version))
            db.execute("UPDATE tools SET reviewed=?,review=?,state=? WHERE id=? AND version=?", (
                int(report.passed), canonical(report), state, tool_id, manifest.version))
        self.store.emit("review_completed", now, task=task, tool_id=tool_id,
                        version=manifest.version, passed=report.passed, evidence=report.evidence)
        return report

    def tool_delete(self, tool_id: str, version: str | None = None, *, purge: bool = False) -> Json:
        return self.registry.delete(tool_id, version, purge=purge)

    def route(self, intent: Intent) -> Any:
        return self.router.route(intent)

    def cancel(self, run_id: str) -> Json:
        with self.store.transaction() as db:
            rows = db.execute("""WITH RECURSIVE chain(id) AS (
                SELECT id FROM runs WHERE id=? UNION ALL
                SELECT r.id FROM runs r JOIN chain c ON r.parent=c.id) SELECT id FROM chain""", (run_id,)).fetchall()
            if not rows:
                raise ATRError("not_found", "run not found")
            ids = [r["id"] for r in rows]
            db.executemany("UPDATE runs SET cancel_requested=1 WHERE id=? AND finished IS NULL", [(x,) for x in ids])
        with self.flag_lock:
            for child_id in ids:
                if child_id in self.cancel_flags:
                    self.cancel_flags[child_id].set()
        return {"run_id": run_id, "cancel_requested": True}

    def set_switch(self, name: str, enabled: bool) -> Json:
        self.store.enabled(name)
        with self.store.transaction() as db:
            db.execute("UPDATE switches SET value=? WHERE name=?", (int(enabled), name))
        if name == "dynamic_execute" and not enabled:
            for row in self.store.rows("SELECT id,dynamic FROM runs WHERE finished IS NULL"):
                if row["dynamic"]:
                    self.cancel(row["id"])
        self.store.emit("switch_changed", self.clock(), name=name, enabled=enabled)
        return {name: enabled}

    def clear_candidates(self) -> Json:
        with self.store.transaction() as db:
            count = db.execute("SELECT COUNT(*) FROM candidates WHERE state!='deleted'").fetchone()[0]
            db.execute("UPDATE candidates SET state='deleted'")
            db.execute("DELETE FROM search WHERE entity='candidate'")
        self.lifecycle.collect_garbage()
        return {"cleared": count}

    def recover_run(self, run_id: str) -> Json:
        """Operator recovery after the worker stopped; effects remain uncertain and are not replayed."""
        row = self.store.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            raise ATRError("not_found", "run not found")
        result = ExecutionResult.model_validate_json(row["result"])
        if result.status == "running":
            result.status, result.error_code = "failed", "interrupted_unknown_effects"
            result.summary = "interrupted execution recovered; effects are uncertain"
            with self.store.transaction() as db:
                db.execute("UPDATE runs SET result=?,finished=?,eligible=0 WHERE id=?",
                           (canonical(result), self.clock().timestamp(), run_id))
                db.execute("DELETE FROM project_locks WHERE run_id=?", (run_id,))
        return result.model_dump(mode="json")

    def status(self) -> Json:
        candidates = self.store.one("SELECT COUNT(*) AS n FROM candidates WHERE state='candidate'")
        return {"project_key": self.project_key, "environment": self.environment,
                "home": str(self.home), "workspace": str(self.workspace),
                "switches": {r["name"]: bool(r["value"]) for r in self.store.rows("SELECT * FROM switches")},
                "tool_states": self.store.rows("SELECT state,COUNT(*) AS count FROM tools GROUP BY state"),
                "candidate_count": candidates["n"] if candidates else 0,
                "active_runs": len(self.store.rows("SELECT id FROM runs WHERE finished IS NULL"))}
