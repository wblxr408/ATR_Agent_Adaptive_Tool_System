"""Fixed tool definitions and a JSONL adapter for any host Agent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

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
    "tool_review": "Run expected-output checks, inspect evidence, restore or roll back a tool version.",
    "tool_delete": "Archive or purge managed tool copies subject to dependency protection.",
}


def definitions() -> list[Json]:
    execute_contract: TypeAdapter[Any] = TypeAdapter(ExecuteRequest | ExecuteAction)  # type: ignore[arg-type]
    schemas = {"read": ReadRequest.model_json_schema(),
               "execute": execute_contract.json_schema(),
               "tool_create": CreateRequest.model_json_schema(),
               "tool_review": ReviewRequest.model_json_schema(),
               "tool_delete": DeleteRequest.model_json_schema()}
    return [{"name": name, "description": DESCRIPTIONS[name], "input_schema": schema}
            for name, schema in schemas.items()]


class AgentAdapter:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def call(self, name: str, arguments: Json) -> Any:
        if name == "read":
            request = ReadRequest.model_validate(arguments)
            return self.runtime.read(request.operation, **request.arguments)
        if name == "execute":
            if "action" in arguments:
                action = ExecuteAction.model_validate(arguments)
                if action.action == "cancel" and action.run_id:
                    return self.runtime.cancel(action.run_id)
                if action.action == "route" and action.intent:
                    return self.runtime.route(action.intent)
                raise ATRError("invalid_operation", "execute action requires route+intent or cancel+run_id")
            return self.runtime.execute(ExecuteRequest.model_validate(arguments))
        if name == "tool_create":
            return self.runtime.tool_create(CreateRequest.model_validate(arguments))
        if name == "tool_review":
            return self.runtime.tool_review(**ReviewRequest.model_validate(arguments).model_dump())
        if name == "tool_delete":
            return self.runtime.tool_delete(**DeleteRequest.model_validate(arguments).model_dump())
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

