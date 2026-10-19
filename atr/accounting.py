from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from atr.errors import ATRError
from atr.models import Cost, canonical
from atr.storage import Store


class Ledger:
    def __init__(self, store: Store, clock: Callable[[], datetime], budget: float) -> None:
        self.store, self.clock, self.budget = store, clock, budget

    def reserve(self, task: str, maximum: float, run_id: str | None = None) -> str:
        if maximum < 0:
            raise ATRError("invalid_budget", "cost bound cannot be negative")
        reservation = uuid4().hex
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO task_budget(task) VALUES (?)", (task,))
            row = db.execute("SELECT reserved FROM task_budget WHERE task=?", (task,)).fetchone()
            amount = db.execute("SELECT COALESCE(SUM(amount),0) FROM costs WHERE task=? "
                                "AND currency='USD'", (task,)).fetchone()[0]
            if amount + row["reserved"] + maximum > self.budget:
                raise ATRError("cost_budget", "task cost budget would be exceeded")
            db.execute("UPDATE task_budget SET reserved=reserved+? WHERE task=?", (maximum, task))
            db.execute("INSERT INTO reservations(id,task,maximum,run_id) VALUES (?,?,?,?)",
                       (reservation, task, maximum, run_id))
        return reservation

    def release(self, reservation: str, *, billing_complete: bool) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM reservations WHERE id=?", (reservation,)).fetchone()
            if row and row["state"] == "pending":
                if billing_complete:
                    db.execute("UPDATE task_budget SET reserved=MAX(0,reserved-?) WHERE task=?",
                               (row["maximum"], row["task"]))
                db.execute("UPDATE reservations SET state=? WHERE id=?",
                           ("settled" if billing_complete else "unbilled", reservation))

    def record(self, task: str, cost: Cost, run_id: str | None = None,
               *, entry_id: str | None = None) -> None:
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO costs VALUES (?,?,?,?,?,?,?,?)", (
                entry_id or uuid4().hex, task, run_id, self.clock().timestamp(), cost.category,
                cost.currency, cost.amount, canonical(cost)))
        self.store.emit("cost_recorded", self.clock(), task=task, run_id=run_id,
                        category=cost.category, amount=cost.amount, currency=cost.currency)

    def report(self, task: str | None = None) -> dict[str, Any]:
        rows = self.store.rows("SELECT * FROM costs" + (" WHERE task=?" if task else ""),
                               (task,) if task else ())
        totals: dict[str, float] = defaultdict(float)
        categories: dict[str, dict[str, Any]] = {}
        input_tokens = output_tokens = cached_tokens = 0
        unknown = estimated = 0
        for row in rows:
            cost = Cost.model_validate_json(row["data"])
            input_tokens += cost.input_tokens
            output_tokens += cost.output_tokens
            cached_tokens += cost.cached_input_tokens
            unknown += cost.amount is None
            estimated += cost.estimated
            group = categories.setdefault(cost.category, {"entries": 0, "known_amounts": {},
                                                            "unknown_entries": 0})
            group["entries"] += 1
            if cost.amount is not None:
                totals[cost.currency] += cost.amount
                group["known_amounts"][cost.currency] = (
                    group["known_amounts"].get(cost.currency, 0) + cost.amount)
            else:
                group["unknown_entries"] += 1
        pending = self.store.rows("SELECT id,task,maximum,state FROM reservations WHERE state!='settled'"
                                  + (" AND task=?" if task else ""), (task,) if task else ())
        return {"root_task_id": task, "known_totals": dict(totals),
                "unsettled_budget_usd": sum(r["maximum"] for r in pending),
                "unsettled_reservations": pending, "estimated_entries": estimated,
                "billing_complete": bool(rows) and unknown == 0 and estimated == 0,
                "end_to_end_complete": bool(rows) and unknown == 0 and estimated == 0 and "main_llm" in categories,
                "unknown_entries": unknown, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "cached_input_tokens": cached_tokens,
                "uncached_input_tokens": input_tokens - cached_tokens, "categories": categories}

    def settle(self, reservation: str, receipt: Cost) -> dict[str, Any]:
        if receipt.amount is None or receipt.estimated or receipt.currency != "USD":
            raise ATRError("invalid_receipt", "settlement requires an actual USD receipt")
        with self.store.transaction() as db:
            held = db.execute("SELECT * FROM reservations WHERE id=?", (reservation,)).fetchone()
            if not held:
                raise ATRError("not_found", "reservation not found")
            if held["state"] == "settled":
                existing = db.execute("SELECT data FROM costs WHERE id=?", (reservation + ":settled",)).fetchone()
                if existing and existing["data"] == canonical(receipt):
                    return {"settled": True, "reservation": reservation}
                raise ATRError("receipt_conflict", "reservation was already settled")
            # One adapter invocation's provisional entries are replaced, not added a second time.
            db.execute("DELETE FROM costs WHERE id LIKE ?", (reservation + ":%",))
            db.execute("INSERT INTO costs VALUES (?,?,?,?,?,?,?,?)", (
                reservation + ":settled", held["task"], held["run_id"], self.clock().timestamp(),
                receipt.category, receipt.currency, receipt.amount, canonical(receipt)))
            db.execute("UPDATE task_budget SET reserved=MAX(0,reserved-?) WHERE task=?",
                       (held["maximum"], held["task"]))
            db.execute("UPDATE reservations SET state='settled' WHERE id=?", (reservation,))
        self.store.emit("cost_settled", self.clock(), task=held["task"], reservation=reservation)
        return {"settled": True, "reservation": reservation}

    def metrics(self) -> dict[str, Any]:
        routes = [json.loads(r["data"])["data"] for r in self.store.rows(
            "SELECT data FROM events WHERE kind='route_decided'")]
        n = len(routes)
        runs = self.store.rows("SELECT result FROM runs WHERE review_run=0 AND finished IS NOT NULL")
        results = [json.loads(r["result"]) for r in runs]
        durations = sorted(r["duration_ms"] for r in results)
        registry_count = self.store.one("SELECT COUNT(*) AS n FROM tools WHERE state!='deleted'")
        return {"routing_events": n,
                "model_fallback_ratio": sum(bool(r.get("model_calls")) for r in routes) / n if n else None,
                "reuse_selection_ratio": sum(r.get("decision") == "REUSE" for r in routes) / n if n else None,
                "execution_count": len(results),
                "execution_failure_ratio": sum(r["status"] != "succeeded" for r in results) / len(results)
                if results else None,
                "p50_ms": durations[(len(durations) - 1) // 2] if durations else None,
                "p95_ms": durations[min(len(durations) - 1, int(len(durations) * .95))] if durations else None,
                "registry_growth": registry_count["n"] if registry_count else 0}
