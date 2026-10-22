"""Fixed tool definitions and a JSONL adapter for any host Agent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from atr.control.authority import TaskContext
from atr.errors import ATRError
from atr.models import Contract, CreateRequest, ExecuteRequest, Intent, Json
from atr.service import Runtime


class ReadRequest(Contract):
    operation: str
    arguments: Json = Field(default_factory=dict)


class ReviewRequest(Contract):
    tool_id: str
    version: str | None = None
    root_task_id: str | None = None
    action: str = "review"


class DeleteRequest(Contract):
    tool_id: str
    version: str | None = None
    purge: bool = False


class ExecuteAction(Contract):
    action: str
    run_id: str | None = None
    intent: Intent | None = None


class ToolCall(Contract):
    id: str | int | None = None
    name: str
    arguments: Json


DESCRIPTIONS = {
    "read": "Read bounded workspace fragments, registry matches, manifests and run artifacts.",
    "execute": "Execute argv or a pinned tool under runtime policy; route an intent or cancel a run.",
    "tool_create": "Register a candidate or propose an immutable tool version; review is required.",
    "tool_review": "Run independent host-approved behavior cases, inspect evidence, restore or roll back a granted tool version.",
    "tool_delete": "Archive or purge managed tool copies subject to dependency protection.",
}


def definitions() -> list[Json]:
    execute_contract: TypeAdapter[Any] = TypeAdapter(ExecuteRequest | ExecuteAction)  # type: ignore[arg-type]
    schemas = {"read": ReadRequest.model_json_schema(),
               "execute": execute_contract.json_schema(),
               "tool_create": CreateRequest.model_json_schema(),
               "tool_review": ReviewRequest.model_json_schema(),
               "tool_delete": DeleteRequest.model_json_schema()}
    def host_bound(schema: Any) -> None:
        if not isinstance(schema, dict):
            return
        if "properties" in schema:
            schema["properties"].pop("root_task_id", None)
            if "required" in schema:
                schema["required"] = [name for name in schema["required"] if name != "root_task_id"]
        for value in schema.values():
            if isinstance(value, dict):
                host_bound(value)
            elif isinstance(value, list):
                for child in value:
                    host_bound(child)
    for schema in schemas.values():
        host_bound(schema)
    return [{"name": name, "description": DESCRIPTIONS[name], "input_schema": schema}
            for name, schema in schemas.items()]


class AgentAdapter:
    def __init__(self, runtime: Runtime, *, context: TaskContext | None = None) -> None:
        self.runtime, self.context = runtime, context

    def _bound(self, arguments: Json) -> Json:
        self.runtime.control.authenticate(self.context)
        assert self.context
        if arguments.get("root_task_id", self.context.root_task_id) != self.context.root_task_id:
            raise ATRError("task_authority", "Agent cannot change the host-bound root task")
        return {**arguments, "root_task_id": self.context.root_task_id}

    def call(self, name: str, arguments: Json) -> Any:
        with self.runtime.activity():
            return self._call(name, arguments)

    def _call(self, name: str, arguments: Json) -> Any:
        self.runtime.control.authenticate(self.context)
        if name == "read":
            request = ReadRequest.model_validate(arguments)
            return self.runtime.read(request.operation, context=self.context, **request.arguments)
        if name == "execute":
            if "action" in arguments:
                if arguments.get("intent") is not None:
                    arguments = {**arguments, "intent": self._bound(arguments["intent"])}
                action = ExecuteAction.model_validate(arguments)
                if action.action == "cancel" and action.run_id:
                    return self.runtime.cancel(action.run_id, context=self.context)
                if action.action == "route" and action.intent:
                    return self.runtime.route(action.intent, context=self.context)
                raise ATRError("invalid_operation", "execute action requires route+intent or cancel+run_id")
            return self.runtime.execute(ExecuteRequest.model_validate(self._bound(arguments)), context=self.context)
        if name == "tool_create":
            return self.runtime.tool_create(CreateRequest.model_validate(self._bound(arguments)), context=self.context)
        if name == "tool_review":
            return self.runtime.tool_review(**ReviewRequest.model_validate(self._bound(arguments)).model_dump(), context=self.context)
        if name == "tool_delete":
            return self.runtime.tool_delete(**DeleteRequest.model_validate(arguments).model_dump(), context=self.context)
        raise ATRError("unknown_tool", "only the fixed five model tools are available")

    def dispatch(self, call: Json) -> Json:
        try:
            request = ToolCall.model_validate(call)
            result = self.call(request.name, request.arguments)
            if isinstance(result, BaseModel):
                result = result.model_dump(mode="json")
            return {"id": request.id, "result": result}
        except ATRError as exc:
            return {"id": call.get("id"), "error": {"code": exc.code, "message": str(exc)}}
        except (ValidationError, TypeError, KeyError, ValueError):
            # Never echo validation input: it can contain credentials or large source text.
            return {"id": call.get("id"), "error": {"code": "invalid_input", "message": "request does not satisfy interface contract"}}

