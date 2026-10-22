from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from atr.adapters import Adapter, JSONModelAdapter, ResearchAdapter
from atr.agent_adapter import AgentAdapter, definitions
from atr.control.authority import BehaviorRule, ResourceScope, TaskContext, TrustedCase
from atr.errors import ATRError
from atr.evals import Observation, compare
from atr.models import CreateRequest, ExecuteRequest, ExecutionResult, Intent, Program, Settings
from atr.policy import Policy
from atr.service import Runtime


def load(path: str) -> Any:
    if path == "-":
        raw = sys.stdin.read(10_485_761)
    else:
        if Path(path).stat().st_size > 10_485_760:
            raise ATRError("input_limit", "JSON request exceeds byte budget")
        raw = Path(path).read_text(encoding="utf-8")
    if len(raw.encode()) > 10_485_760:
        raise ATRError("input_limit", "JSON request exceeds byte budget")
    return json.loads(raw)


def emit(value: Any) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def configured(args: argparse.Namespace) -> Runtime:
    if args.config and Path(args.config).resolve().is_relative_to(Path(args.workspace).resolve()):
        raise ATRError("unsafe_config", "operator config must be outside the tool workspace")
    config = load(args.config) if args.config else {}
    if not isinstance(config, dict) or set(config) - {"settings", "policy", "research", "models", "project_key"}:
        raise ATRError("invalid_config", "operator config has unsupported sections")
    adapters: dict[str, Adapter] = {}
    secrets = []
    for name, options in config.get("research", {}).items():
        adapters[name] = ResearchAdapter(**options)
    for name, options in config.get("models", {}).items():
        options = dict(options)
        key_env = options.pop("api_key_env")
        secret = os.environ.get(key_env)
        if not secret:
            raise ATRError("missing_credential", "configured model credential is unavailable")
        secrets.append(secret)
        adapters[name] = JSONModelAdapter(api_key=secret, **options)
    return Runtime(workspace=Path(args.workspace), home=Path(args.home) if args.home else None,
                   project_key=config.get("project_key"), settings=Settings(**config.get("settings", {})),
                   policy=Policy(**config.get("policy", {})), adapters=adapters, secrets=secrets)


def host_context(args: argparse.Namespace, runtime: Runtime) -> TaskContext | None:
    if not args.task_context:
        return None
    path = Path(args.task_context).resolve(strict=True)
    if path.is_relative_to(runtime.workspace):
        raise ATRError("unsafe_config", "task credential must be outside the tool workspace")
    context = TaskContext(**load(str(path)))
    runtime.control.authenticate(context)
    return context


def trusted_load(path: str, runtime: Runtime) -> Any:
    if path != "-" and Path(path).resolve().is_relative_to(runtime.workspace):
        raise ATRError("unsafe_config", "host grants and acceptance must be outside the tool workspace")
    return load(path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="atr", description="Adaptive Tool Runtime")
    result.add_argument("--workspace", default=".")
    result.add_argument("--home", help="operator-owned control directory outside the workspace")
    result.add_argument("--config", help="trusted operator JSON config (never supplied by a tool)")
    result.add_argument("--task-context", help="host-held task credential JSON outside the workspace")
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "definitions", "tools", "runs", "serve", "clear-candidates"):
        sub.add_parser(name)
    sub.add_parser("disable")
    issue = sub.add_parser("issue-task", help="operator only: issue a bounded task credential")
    issue.add_argument("grant", help="trusted JSON with scope, limits and fixed task lifetime")
    issue.add_argument("--output", required=True, help="new credential file outside the workspace")
    approve = sub.add_parser("approve-operation", help="operator only: bind one business operation")
    approve.add_argument("request")
    approve.add_argument("--business-key", required=True)
    approve.add_argument("--acceptance", required=True, help="host-authored BehaviorRule JSON")
    approve_program = sub.add_parser("approve-program", help="operator only: register independent cases")
    approve_program.add_argument("program")
    approve_program.add_argument("--acceptance", required=True, help="host-authored TrustedCase JSON array")
    approve_route = sub.add_parser("approve-route", help="operator only: approve text for model routing")
    approve_route.add_argument("intent")
    approve_route.add_argument("--business-key", required=True)
    sub.add_parser("revoke-task")
    sub.add_parser("revoke-operation").add_argument("operation_id")
    sub.add_parser("revoke-program").add_argument("program")
    sub.add_parser("set-total-budget", help="operator only: explicitly change the persistent allowance").add_argument("maximum", type=float)
    for name in ("execute", "create", "route"):
        sub.add_parser(name).add_argument("request", help="JSON request filename or - for stdin")
    sub.add_parser("record-cost").add_argument("receipt", help="host Agent cost receipt JSON")
    settle = sub.add_parser("settle-cost")
    settle.add_argument("reservation")
    settle.add_argument("receipt")
    read = sub.add_parser("read")
    read.add_argument("operation")
    read.add_argument("arguments", help="JSON arguments file or -")
    search = sub.add_parser("search")
    search.add_argument("query")
    review = sub.add_parser("review")
    review.add_argument("tool_id")
    review.add_argument("--version")
    review.add_argument("--action", choices=["review", "restore", "rollback", "evidence"], default="review")
    delete = sub.add_parser("delete")
    delete.add_argument("tool_id")
    delete.add_argument("--version")
    delete.add_argument("--purge", action="store_true")
    for name in ("cancel", "recover"):
        sub.add_parser(name).add_argument("run_id")
    cost = sub.add_parser("cost")
    cost.add_argument("--task")
    sub.add_parser("metrics")
    switch = sub.add_parser("switch")
    switch.add_argument("name", choices=["auto_create", "dynamic_execute"])
    switch.add_argument("value", choices=["on", "off"])
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("--watch", action="store_true")
    events = sub.add_parser("events")
    events.add_argument("--export")
    schema = sub.add_parser("schema")
    schema.add_argument("name", choices=["manifest", "execution-result", "event", "interfaces"])
    evaluation = sub.add_parser("eval-report")
    evaluation.add_argument("observations", help="existing observation JSONL; does not execute tasks")
    return result


def serve(runtime: Runtime, context: TaskContext | None = None) -> None:
    runtime.control.authenticate(context)
    runtime.start_maintenance()
    adapter = AgentAdapter(runtime, context=context)
    while line := sys.stdin.readline(10_485_761):
        try:
            if len(line.encode()) > 10_485_760:
                while not line.endswith("\n"):
                    line = sys.stdin.readline(65536)
                    if not line:
                        break
                raise ValueError("request too large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            emit(adapter.dispatch(request))
        except (ValueError, RecursionError):
            emit({"id": None, "error": {"code": "invalid_json", "message": "expected bounded JSON object"}})
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "definitions":
            emit(definitions())
            return 0
        if args.command == "schema":
            from atr.models import Event, Manifest
            schemas = {"manifest": Manifest.model_json_schema(), "execution-result": ExecutionResult.model_json_schema(),
                       "event": Event.model_json_schema(), "interfaces": definitions()}
            emit(schemas[args.name])
            return 0
        if args.command == "eval-report":
            observations = [Observation.model_validate_json(line) for line in
                            Path(args.observations).read_text(encoding="utf-8").splitlines() if line.strip()]
            emit(compare(observations))
            return 0
        with configured(args) as runtime:
            command = args.command
            context = host_context(args, runtime)
            value: Any
            if command in ("init", "status"):
                value = runtime.status()
            elif command == "issue-task":
                output = Path(args.output).resolve()
                if output.is_relative_to(runtime.workspace):
                    raise ATRError("unsafe_config", "task credential must be outside the workspace")
                grant = dict(trusted_load(args.grant, runtime))
                grant["scope"] = ResourceScope.model_validate(grant.get("scope", {}))
                issued = runtime.control.issue_task(**grant)
                runtime.control.save_context(issued, output)
                value = {"root_task_id": issued.root_task_id, "credential_file": str(output)}
            elif command == "approve-operation":
                runtime.control.authenticate(context)
                assert context
                request_data = dict(load(args.request))
                request_data.setdefault("root_task_id", context.root_task_id)
                value = runtime.control.authorize_operation(context, ExecuteRequest.model_validate(request_data),
                    business_key=args.business_key, rule=BehaviorRule.model_validate(trusted_load(args.acceptance, runtime)))
            elif command == "approve-program":
                value = {"acceptance_id": runtime.control.approve_program(Program.model_validate(load(args.program)),
                    cases=[TrustedCase.model_validate(case) for case in trusted_load(args.acceptance, runtime)])}
            elif command == "approve-route":
                runtime.control.authenticate(context)
                assert context
                data = dict(load(args.intent))
                data.setdefault("root_task_id", context.root_task_id)
                value = runtime.control.authorize_route(context, Intent.model_validate(data), business_key=args.business_key)
            elif command == "revoke-task":
                runtime.control.authenticate(context)
                assert context
                runtime.control.revoke_task(context)
                value = {"revoked": True}
            elif command == "revoke-operation":
                runtime.control.revoke_operation(args.operation_id)
                value = {"revoked": True}
            elif command == "revoke-program":
                runtime.control.revoke_program(Program.model_validate(load(args.program)))
                value = {"revoked": True}
            elif command == "set-total-budget":
                runtime.control.set_total_budget(args.maximum)
                value = {"maximum_usd": args.maximum}
            elif command == "execute":
                request = ExecuteRequest.model_validate(load(args.request))
                if request.background:
                    raise ATRError("background_requires_serve", "use serve for background executions")
                value = runtime.execute(request, context=context)
            elif command == "create":
                data = dict(load(args.request))
                if context:
                    data.setdefault("root_task_id", context.root_task_id)
                value = runtime.tool_create(CreateRequest.model_validate(data), context=context)
            elif command == "review":
                value = runtime.tool_review(args.tool_id, args.version, action=args.action, context=context)
            elif command == "delete":
                value = runtime.tool_delete(args.tool_id, args.version, purge=args.purge, context=context)
            elif command == "read":
                value = runtime.read(args.operation, context=context, **load(args.arguments))
            elif command == "search":
                value = runtime.read("registry_search", query=args.query)
            elif command == "route":
                data = dict(load(args.request))
                if context:
                    data.setdefault("root_task_id", context.root_task_id)
                value = runtime.route(Intent.model_validate(data), context=context)
            elif command == "tools":
                value = runtime.store.rows("SELECT id,version,state,reviewed,created,last_used FROM tools ORDER BY id,version")
            elif command == "runs":
                value = [json.loads(r["result"]) for r in runtime.store.rows("SELECT result FROM runs ORDER BY started DESC LIMIT 100")]
            elif command == "cost":
                value = runtime.ledger.report(args.task)
            elif command == "record-cost":
                from atr.models import Cost
                receipt = load(args.receipt)
                runtime.ledger.record(receipt["root_task_id"], Cost.model_validate(receipt["cost"]),
                    receipt.get("run_id"), entry_id=receipt.get("receipt_id"))
                value = {"recorded": True}
            elif command == "settle-cost":
                from atr.models import Cost
                value = runtime.ledger.settle(args.reservation, Cost.model_validate(load(args.receipt)["cost"]))
            elif command == "metrics":
                value = runtime.ledger.metrics()
            elif command == "switch":
                value = runtime.set_switch(args.name, args.value == "on")
            elif command == "disable":
                runtime.set_switch("auto_create", False)
                value = runtime.set_switch("dynamic_execute", False)
            elif command == "clear-candidates":
                value = runtime.clear_candidates()
            elif command == "cancel":
                value = runtime.cancel(args.run_id, context=context)
            elif command == "recover":
                value = runtime.recover_run(args.run_id)
            elif command == "events":
                value = {"exported": runtime.store.export_events(Path(args.export))} if args.export else [
                    json.loads(row["data"]) for row in runtime.store.rows("SELECT data FROM events ORDER BY timestamp DESC LIMIT 100")]
            elif command == "serve":
                serve(runtime, context)
                return 0
            elif command == "maintenance":
                stop = threading.Event()
                while True:
                    emit({"changes": runtime.lifecycle.tick()})
                    if not args.watch:
                        return 0
                    stop.wait(60)
            else:
                raise ATRError("invalid_command", "unknown command")
            emit(value)
            if isinstance(value, ExecutionResult) and value.status != "succeeded":
                return 1
            if hasattr(value, "passed") and not value.passed:
                return 1
        return 0
    except ATRError as exc:
        emit({"error": {"code": exc.code, "message": str(exc)}})
        return 1
    except (ValidationError, TypeError, ValueError, OSError, KeyError):
        emit({"error": {"code": "invalid_input", "message": "invalid request, operator config or file"}})
        return 2
    except KeyboardInterrupt:
        return 130
