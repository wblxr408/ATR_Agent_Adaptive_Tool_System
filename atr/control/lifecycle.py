from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

from atr.control.registry import Registry
from atr.models import State


class Lifecycle:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.store, self.settings = registry.store, registry.settings

    def tick(self) -> list[dict[str, Any]]:
        now = self.registry.clock()
        changes: list[dict[str, Any]] = []
        for row in self.store.rows("SELECT * FROM candidates WHERE state!='deleted' AND expires<=?",
                                   (now.timestamp(),)):
            with self.store.transaction() as db:
                db.execute("UPDATE candidates SET state='deleted' WHERE id=?", (row["id"],))
                db.execute("DELETE FROM search WHERE entity='candidate' AND id=?", (row["id"],))
            changes.append({"candidate_id": row["id"], "state": State.DELETED})
        for row in self.store.rows("SELECT * FROM tools WHERE state!='deleted'"):
            tool_id, version, state = row["id"], row["version"], row["state"]
            target = None
            if state == State.TEMPORARY:
                stats = self.registry.stats(tool_id, version)
                end = row["window_start"] + timedelta(days=self.settings.temporary_days).total_seconds()
                # Eligible is set only for runs completed within this fixed window.
                ordinary = (stats["eligible_uses"] >= self.settings.promotion_uses
                            and stats["distinct_root_tasks"] >= self.settings.promotion_tasks)
                exception = (self.settings.same_task_uses is not None
                             and stats["distinct_inputs"] >= self.settings.same_task_uses)
                if row["reviewed"] and (ordinary or exception):
                    target = State.PERSISTENT
                elif now.timestamp() >= end:
                    target = State.ARCHIVED
            elif state == State.PERSISTENT:
                last = row["last_used"] if row["last_used"] is not None else row["window_start"]
                if now.timestamp() - last >= timedelta(days=self.settings.dormant_days).total_seconds():
                    target = State.DORMANT
            elif state == State.ARCHIVED and row["archived"] is not None:
                if (now.timestamp() - row["archived"] >= timedelta(days=self.settings.archive_days).total_seconds()
                        and not self.registry.referenced(tool_id, version)
                        and not self.store.one("SELECT id FROM runs WHERE tool_id=? AND version=? "
                                               "AND finished IS NULL", (tool_id, version))):
                    target = State.DELETED
            if target is not None:
                with self.store.transaction() as db:
                    updated = db.execute("UPDATE tools SET state=?,archived=? WHERE id=? AND version=? "
                               "AND state=? AND epoch=? AND reviewed=?",
                               (target, now.timestamp() if target == State.ARCHIVED else row["archived"],
                                tool_id, version, state, row["epoch"], row["reviewed"])).rowcount
                    if not updated:
                        continue
                    if target not in (State.TEMPORARY, State.PERSISTENT):
                        db.execute("DELETE FROM search WHERE entity='tool' AND id=? AND version=?",
                                   (tool_id, version))
                    if target == State.DELETED:
                        db.execute("DELETE FROM active WHERE id=? AND version=?", (tool_id, version))
                kind = {State.PERSISTENT: "tool_promoted", State.ARCHIVED: "tool_archived",
                        State.DORMANT: "tool_dormant", State.DELETED: "tool_deleted"}[target]
                self.store.emit(kind, now, tool_id=tool_id, version=version)
                changes.append({"tool_id": tool_id, "version": version, "state": target})
        self._retention()
        self.collect_garbage()
        return changes

    def _retention(self) -> None:
        now = self.registry.clock().timestamp()
        raw_cutoff = now - timedelta(days=self.settings.raw_log_days).total_seconds()
        aggregate_cutoff = now - timedelta(days=self.settings.aggregate_days).total_seconds()
        with self.store.transaction() as db:
            db.execute("DELETE FROM artifacts WHERE created<?", (raw_cutoff,))
            # Preserve non-sensitive counters and refs but expire raw excerpts.
            for row in db.execute("SELECT id,result FROM runs WHERE finished<?", (raw_cutoff,)).fetchall():
                result = json.loads(row["result"])
                result["stdout_excerpt"], result["artifacts"] = "", []
                db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), row["id"]))
            db.execute("DELETE FROM events WHERE timestamp<?", (aggregate_cutoff,))
            db.execute("DELETE FROM costs WHERE timestamp<?", (aggregate_cutoff,))

    def collect_garbage(self) -> int:
        # Writers link objects under the same SQLite write lock, including across clients.
        with self.store.transaction():
            return self._collect_garbage_locked()

    def _collect_garbage_locked(self) -> int:
        # References, not filename age or a caller-supplied path, determine managed cleanup.
        refs = {r["program_ref"] for r in self.store.rows(
            "SELECT program_ref FROM tools WHERE state!='deleted' UNION "
            "SELECT program_ref FROM candidates WHERE state!='deleted'")}
        refs.update(r["ref"] for r in self.store.rows("SELECT ref FROM artifacts"))
        deleted = 0
        for folder in self.store.objects.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            for path in folder.iterdir():
                ref = "artifact:sha256/" + folder.name + path.name
                # Grace period protects staged object writes not yet linked by another process.
                if (ref not in refs and path.is_file() and not path.is_symlink()
                        and time.time() - path.stat().st_mtime > 3600):
                    path.unlink()
                    deleted += 1
        return deleted
