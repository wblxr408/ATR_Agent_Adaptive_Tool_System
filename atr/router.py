from __future__ import annotations

from typing import Protocol

from atr.accounting import Ledger
from atr.control.registry import Registry
from atr.models import Cost, Intent, Route


class RouteModel(Protocol):
    max_cost_usd: float
    def decide(self, summary: str, matches: list[dict[str, object]]) -> tuple[str | dict[str, object], Cost]: ...


class Router:
    def __init__(self, registry: Registry, model: RouteModel | None = None,
                 ledger: Ledger | None = None) -> None:
        self.registry, self.model, self.ledger = registry, model, ledger

    def route(self, intent: Intent) -> Route:
        matches = self.registry.search(intent.tool_id or intent.summary,
                                       inputs=intent.inputs, side_effect=intent.side_effect,
                                       output_schema=intent.output_schema)
        exact = [m for m in matches if m["exact"]]
        if exact:
            result = Route(decision="REUSE", selected=exact[0]["id"], reason="compatible exact match")
        elif len(matches) == 1:
            result = Route(decision="REUSE", selected=matches[0]["id"], reason="compatible local retrieval")
        elif intent.simple_command:
            result = Route(decision="DIRECT", reason="existing command; no routing model needed")
        elif intent.explicit_tool_creation and not matches:
            result = Route(decision="CREATE", reason="explicit creation after deduplication")
        elif intent.independent_program and not matches:
            result = Route(decision="EXECUTE_AND_OBSERVE", reason="new independent execution unit")
        elif self.model is None:
            result = Route(decision="DEFER", reason="ambiguity requires main agent")
        else:
            # Only a constrained decision enum is accepted; no returned commands are executed.
            reservation = None
            billed = False
            try:
                if self.ledger:
                    reservation = self.ledger.reserve(intent.root_task_id, self.model.max_cost_usd)
                decision, cost = self.model.decide(intent.summary[:1024], matches[:3])
                if self.ledger:
                    self.ledger.record(intent.root_task_id, cost)
                    billed = cost.amount is not None and not cost.estimated
                choice = {"decision": decision} if isinstance(decision, str) else decision
                result = Route.model_validate({**choice, "reason": "one bounded model decision", "model_calls": 1})
                if (result.decision == "REUSE" and result.selected not in {m["id"] for m in matches}) or (
                        result.decision == "CREATE" and matches):
                    result = Route(decision="DEFER", reason="model cannot invent a reusable target",
                                   model_calls=1)
                self.registry.store.emit("router_usage", self.registry.clock(),
                                         task=intent.root_task_id, cost=cost.model_dump())
            except Exception:
                result = Route(decision="DEFER", reason="routing model failed or returned invalid enum",
                               model_calls=1)
            finally:
                if reservation and self.ledger:
                    if not billed:
                        self.ledger.record(intent.root_task_id, Cost(category="router_llm"))
                    self.ledger.release(reservation, billing_complete=billed)
        self.registry.store.emit("route_decided", self.registry.clock(), task=intent.root_task_id,
                                 **result.model_dump(), retrieved=[m["id"] for m in matches])
        return result
