"""Host-only authority. None of these issuance methods are model tools.

The host and control home are trusted; tool text and Agent JSON are not. A task
credential is passed out of band and never grants a new root identity on demand.
"""

from __future__ import annotations

import hmac
import json
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from atr.errors import ATRError
from atr.models import (
    Check,
    Contract,
    ExecuteRequest,
    Intent,
    Json,
    Program,
    Route,
    Settings,
    canonical,
)
from atr.policy import Policy
from atr.storage import Store, digest


def safe_path(name: str, *, subtree: bool = False) -> str:
    candidate = name[:-1] if subtree and name.endswith("/") else name
    Program.safe_files({candidate: ""})
    return name


class ResourceScope(Contract):
    read_paths: list[str] = Field(default_factory=list)
    write_paths: dict[str, list[Literal["create", "update", "delete"]]] = Field(default_factory=dict)
    adapters: list[str] = Field(default_factory=list)
    tool_actions: dict[str, list[Literal["create", "review", "restore", "rollback", "archive", "purge"]]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def paths(self) -> ResourceScope:
        for name in self.read_paths:
            safe_path(name, subtree=True)
        for name in self.write_paths:
            safe_path(name)
        return self


class AdapterGrant(Contract):
    capability: str = Field(min_length=1)
    inputs: Json
    max_calls: int = Field(default=1, ge=1, le=128)


class BehaviorRule(Contract):
    checks: list[Check] = Field(min_length=1, max_length=64)
    output_format: Literal["json", "text"] = "json"
    expected_changes: dict[str, str | None] = Field(default_factory=dict)
    adapter_calls: list[AdapterGrant] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def paths(self) -> BehaviorRule:
        for name in self.expected_changes:
            safe_path(name)
        return self

    def validate_output(self, stdout: bytes) -> None:
        try:
            output = json.loads(stdout) if self.output_format == "json" else stdout.decode("utf-8")
            for check in self.checks:
                actual: Any = output
                for component in check.expected_path.split(".") if check.expected_path else []:
                    actual = actual[component]
                # JSON boolean and numeric equality must not be conflated.
                if canonical(actual) != canonical(check.expected):
                    raise ATRError("behavior_contract", "trusted output expectation was not satisfied")
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError) as exc:
            raise ATRError("behavior_contract", "trusted output path or format was not satisfied") from exc

    def validate_changes(self, before: dict[str, bytes], after: dict[str, bytes]) -> None:
        changes = {name: after.get(name) for name in set(before) | set(after)
                   if before.get(name) != after.get(name)}
        expected = {name: value.encode("utf-8") if value is not None else None
                    for name, value in self.expected_changes.items()}
        if changes != expected:
            raise ATRError("behavior_contract", "workspace effects differ from the trusted expectation")


class TrustedCase(Contract):
    inputs: Json = Field(default_factory=dict)
    rule: BehaviorRule
    name: str = "host acceptance"


@dataclass(frozen=True)
class TaskContext:
    root_task_id: str
    credential: str = field(repr=False)


class ControlPlane:
    def __init__(self, store: Store, settings: Settings, policy: Policy,
                 clock: Callable[[], datetime], prepare: Callable[[ExecuteRequest], ExecuteRequest],
                 *, workspace: Path) -> None:
        self.store, self.settings, self.policy, self.clock, self.prepare = store, settings, policy, clock, prepare
        self.workspace = workspace
        with store.transaction() as db:
            # Reopening never silently raises the existing persistent total allowance.
            db.execute("INSERT OR IGNORE INTO control_budget VALUES (1,?)", (settings.max_total_cost,))
            db.execute("UPDATE control_budget SET maximum=MIN(maximum,?) WHERE id=1", (settings.max_total_cost,))

    def issue_task(self, *, scope: ResourceScope | None = None, budget_usd: float | None = None,
                   max_calls: int = 128, ttl_seconds: float = 3600,
                   max_creations: int | None = None) -> TaskContext:
        budget = self.settings.max_task_cost if budget_usd is None else budget_usd
        if (not 0 <= budget <= self.settings.max_task_cost or not 1 <= max_calls <= 100000
                or not 0 < ttl_seconds <= 86400):
            raise ATRError("invalid_grant", "task limits must narrow operator limits")
        creations = self.settings.max_creations_per_task if max_creations is None else max_creations
        if not 0 <= creations <= self.settings.max_creations_per_task:
            raise ATRError("invalid_grant", "creation limit must narrow operator limits")
        resources = ResourceScope.model_validate((scope or ResourceScope()).model_dump())
        for name in [*resources.read_paths, *resources.write_paths]:
            if self.policy.excluded(Path(name)):
                raise ATRError("permission_denied", "task grants cannot include protected paths")
        if not set(resources.adapters) <= set(self.policy.network_capabilities):
            raise ATRError("permission_denied", "task adapter grant exceeds operator policy")
        context = TaskContext("task_" + uuid4().hex, secrets.token_urlsafe(32))
        self.store.redactor.secrets.append(context.credential)
        now = self.clock().timestamp()
        with self.store.transaction() as db:
            db.execute("INSERT INTO task_authority(id,credential,scope,expires,max_calls,cost_limit,max_creations) "
                       "VALUES (?,?,?,?,?,?,?)", (context.root_task_id, digest(context.credential), canonical(resources),
                                                 now + ttl_seconds, max_calls, budget, creations))
            db.execute("INSERT INTO tasks VALUES (?,?)", (context.root_task_id, now))
        self.store.emit("task_issued", self.clock(), task=context.root_task_id,
                        expires=now + ttl_seconds, max_calls=max_calls, budget_usd=budget)
        return context

    def authenticate(self, context: TaskContext | None, task: str | None = None) -> dict[str, Any]:
        if context is None or (task is not None and task != context.root_task_id):
            raise ATRError("task_authority", "a matching host-issued task context is required")
        row = self.store.one("SELECT * FROM task_authority WHERE id=?", (context.root_task_id,))
        if not row or not hmac.compare_digest(row["credential"], digest(context.credential)):
            raise ATRError("task_authority", "task credential is invalid")
        if context.credential not in self.store.redactor.secrets:
            self.store.redactor.secrets.append(context.credential)
        if row["revoked"] or self.clock().timestamp() >= row["expires"]:
            raise ATRError("task_expired", "task is revoked or its fixed deadline expired")
        return row

    def scope(self, context: TaskContext) -> ResourceScope:
        return ResourceScope.model_validate_json(self.authenticate(context)["scope"])

    def scoped_policy(self, context: TaskContext) -> Policy:
        scope = self.scope(context)
        policy = self.policy.model_copy(deep=True)
        policy.read_paths = scope.read_paths
        policy.write_rules = {name: list(actions) for name, actions in scope.write_paths.items()}
        return policy

    def consume_call(self, context: TaskContext) -> None:
        with self.store.transaction() as db:
            row = self.authenticate(context)
            if not db.execute("UPDATE task_authority SET calls=calls+1 WHERE id=? AND calls<max_calls",
                              (row["id"],)).rowcount:
                raise ATRError("task_calls", "trusted root task call budget exhausted")

    def authorize_operation(self, context: TaskContext, request: ExecuteRequest, *,
                            business_key: str, rule: BehaviorRule) -> ExecuteRequest:
        self.authenticate(context, request.root_task_id)
        if not business_key or len(business_key) > 200:
            raise ATRError("invalid_grant", "operation needs a stable business identity")
        request = self.prepare(ExecuteRequest.model_validate(request.model_dump(mode="json")))
        checked_rule = BehaviorRule.model_validate(rule.model_dump(mode="json"))
        target = request.program
        if target is None and request.mode == "tool":
            saved = self.store.one("SELECT program_ref FROM tools WHERE id=? AND version=?",
                                   (request.tool_id, request.version))
            if saved:
                target = Program.model_validate_json(self.store.get(saved["program_ref"]))
        # A direct adapter's payload is already explicitly approved. Workflows
        # require a host-authored list, because earlier outputs can change data.
        if target and target.manifest.kind in ("adapter", "model_pipeline") and not checked_rule.adapter_calls:
            checked_rule.adapter_calls = [AdapterGrant(capability=target.manifest.adapter or "", inputs=request.inputs)]
        identity = self.payload(request)
        with self.store.transaction() as db:
            previous = db.execute("SELECT * FROM operation_authority WHERE task=? AND business_key=?",
                                  (context.root_task_id, business_key)).fetchone()
            if previous:
                if previous["payload"] != identity or previous["rule"] != canonical(checked_rule):
                    raise ATRError("operation_conflict", "business identity already binds another operation")
                operation = previous["id"]
            else:
                operation = "op_" + uuid4().hex
                db.execute("INSERT INTO operation_authority VALUES (?,?,?,?,?,0)",
                           (operation, context.root_task_id, business_key, identity, canonical(checked_rule)))
        return request.model_copy(update={"operation_id": operation})

    @staticmethod
    def payload(request: ExecuteRequest) -> str:
        return digest(canonical(request.model_dump(mode="json", exclude={"operation_id", "background"})))

    def operation(self, context: TaskContext, request: ExecuteRequest) -> BehaviorRule:
        self.authenticate(context, request.root_task_id)
        row = self.store.one("SELECT * FROM operation_authority WHERE id=? AND task=?",
                             (request.operation_id, context.root_task_id))
        if not row or row["revoked"] or row["payload"] != self.payload(request):
            raise ATRError("operation_authority", "operation is absent, revoked or differs from host approval")
        return BehaviorRule.model_validate_json(row["rule"])

    def approve_program(self, program: Program, *, cases: list[TrustedCase]) -> str:
        if not cases or len(cases) > 64:
            raise ATRError("trusted_acceptance_required", "one to 64 independent cases are required")
        prepared = self.prepare(ExecuteRequest(root_task_id="host-approval", program=program))
        key = digest(canonical(prepared.program))
        data = canonical([TrustedCase.model_validate(case.model_dump(mode="json")).model_dump(mode="json")
                          for case in cases])
        with self.store.transaction() as db:
            previous = db.execute("SELECT cases FROM trusted_acceptance WHERE program=?", (key,)).fetchone()
            if previous and previous["cases"] != data:
                raise ATRError("acceptance_conflict", "acceptance is immutable; revoke before changing the program")
            db.execute("INSERT OR IGNORE INTO trusted_acceptance VALUES (?,?,0)", (key, data))
        return key

    def acceptance(self, program: Program) -> list[TrustedCase]:
        row = self.store.one("SELECT cases FROM trusted_acceptance WHERE program=? AND revoked=0",
                            (digest(canonical(program)),))
        if not row:
            raise ATRError("trusted_acceptance_required", "trusted_acceptance_required: host-authored cases are missing")
        return [TrustedCase.model_validate(case) for case in json.loads(row["cases"])]

    def authorize_tool(self, context: TaskContext, tool_id: str, action: str) -> None:
        if action not in self.scope(context).tool_actions.get(tool_id, []):
            raise ATRError("permission_denied", "tool management action is outside this task's grant")

    def consume_adapter(self, root_run: str, rule: BehaviorRule, capability: str, inputs: Json) -> None:
        with self.store.transaction() as db:
            for index, grant in enumerate(rule.adapter_calls):
                if grant.capability != capability or canonical(grant.inputs) != canonical(inputs):
                    continue
                db.execute("INSERT OR IGNORE INTO adapter_uses VALUES (?,?,0)", (root_run, index))
                if db.execute("UPDATE adapter_uses SET uses=uses+1 WHERE root_run=? AND grant_index=? AND uses<?",
                              (root_run, index, grant.max_calls)).rowcount:
                    return
        raise ATRError("permission_denied", "provider data or call count exceeds root operation approval")

    @staticmethod
    def route_payload(intent: Intent) -> str:
        return digest(canonical({"route": intent.model_dump(mode="json", exclude={"operation_id"})}))

    def authorize_route(self, context: TaskContext, intent: Intent, *, business_key: str) -> Intent:
        self.authenticate(context, intent.root_task_id)
        if not business_key or len(business_key) > 200:
            raise ATRError("invalid_grant", "routing approval needs a stable business identity")
        payload = self.route_payload(intent)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM operation_authority WHERE task=? AND business_key=?",
                             (context.root_task_id, business_key)).fetchone()
            if row and (row["payload"] != payload or row["rule"] != '{"kind":"route"}'):
                raise ATRError("operation_conflict", "business identity already binds another operation")
            operation = row["id"] if row else "route_" + uuid4().hex
            if not row:
                db.execute("INSERT INTO operation_authority VALUES (?,?,?,?,?,0)",
                           (operation, context.root_task_id, business_key, payload, '{"kind":"route"}'))
        return intent.model_copy(update={"operation_id": operation})

    def begin_route(self, context: TaskContext, intent: Intent) -> Route | None:
        with self.store.transaction() as db:
            self.authenticate(context, intent.root_task_id)
            approval = db.execute("SELECT * FROM operation_authority WHERE id=? AND task=?",
                                  (intent.operation_id, context.root_task_id)).fetchone()
            if (not approval or approval["revoked"] or approval["payload"] != self.route_payload(intent)
                    or approval["rule"] != '{"kind":"route"}'):
                raise ATRError("operation_authority", "routing requires host approval of the exact intent")
            previous = db.execute("SELECT result FROM route_results WHERE operation=?", (intent.operation_id,)).fetchone()
            if previous:
                return Route.model_validate_json(previous["result"])
            if not db.execute("UPDATE task_authority SET calls=calls+1 WHERE id=? AND calls<max_calls",
                              (context.root_task_id,)).rowcount:
                raise ATRError("task_calls", "trusted root task call budget exhausted")
            pending = Route(decision="DEFER", reason="approved routing pending or interrupted; not replayed")
            db.execute("INSERT INTO route_results VALUES (?,?)", (intent.operation_id, canonical(pending)))
        return None

    def revoke_task(self, context: TaskContext) -> None:
        self.authenticate(context)
        with self.store.transaction() as db:
            db.execute("UPDATE task_authority SET revoked=1 WHERE id=?", (context.root_task_id,))
            db.execute("UPDATE runs SET cancel_requested=1 WHERE task=? AND finished IS NULL", (context.root_task_id,))

    def revoke_operation(self, operation_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE operation_authority SET revoked=1 WHERE id=?", (operation_id,))

    def set_total_budget(self, maximum: float) -> None:
        if not math.isfinite(maximum) or maximum < 0:
            raise ATRError("invalid_budget", "total allowance must be a finite nonnegative USD value")
        with self.store.transaction() as db:
            db.execute("UPDATE control_budget SET maximum=? WHERE id=1", (maximum,))
        self.store.emit("total_budget_changed", self.clock(), maximum=maximum)

    def revoke_program(self, program: Program) -> None:
        prepared = self.prepare(ExecuteRequest(root_task_id="host-approval", program=program))
        with self.store.transaction() as db:
            db.execute("UPDATE trusted_acceptance SET revoked=1 WHERE program=?", (digest(canonical(prepared.program)),))

    def save_context(self, context: TaskContext, path: Path) -> None:
        self.authenticate(context)
        if path.resolve().is_relative_to(self.workspace):
            raise ATRError("unsafe_config", "task credentials cannot be saved in the tool workspace")
        with path.open("x", encoding="utf-8") as stream:
            stream.write(canonical({"root_task_id": context.root_task_id, "credential": context.credential}))
        path.chmod(0o600)
