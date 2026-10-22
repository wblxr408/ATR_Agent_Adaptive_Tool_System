from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from atr.errors import ATRError
from atr.models import Manifest, Program, Settings, State, canonical
from atr.policy import Policy
from atr.storage import Store, digest

ACTIVE = (State.TEMPORARY, State.PERSISTENT)
RESTRICTED = (State.DELETED, State.DISABLED, State.QUARANTINED)


def signature(program: Program) -> str:
    manifest = program.manifest.model_dump(mode="json", exclude={
        "id", "version", "description", "aliases", "tags", "checks"})
    return digest(canonical({"manifest": manifest, "files": program.files, "binary_files": program.binary_files}))


class Registry:
    def __init__(self, store: Store, policy: Policy, settings: Settings,
                 project_key: str, environment: str, clock: Callable[[], datetime]) -> None:
        self.store, self.policy, self.settings = store, policy, settings
        self.project_key, self.environment, self.clock = project_key, environment, clock

    def program(self, tool_id: str, version: str | None = None) -> tuple[Program, dict[str, Any]]:
        if version is None:
            active = self.store.one("SELECT version FROM active WHERE id=?", (tool_id,))
            if not active:
                raise ATRError("not_found", "tool has no selected version")
            version = active["version"]
        row = self.store.one("SELECT * FROM tools WHERE id=? AND version=?", (tool_id, version))
        if not row or row["state"] == State.DELETED:
            raise ATRError("not_found", "tool version is not available")
        return Program.model_validate_json(self.store.get(row["program_ref"])), row

    def candidate(self, candidate_id: str, *, archived: bool = False) -> tuple[Program, dict[str, Any]]:
        row = self.store.one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not row or row["state"] == State.DELETED or (
            not archived and (row["state"] != State.CANDIDATE
                              or row["expires"] <= self.clock().timestamp())):
            raise ATRError("not_found", "candidate is expired or unavailable")
        return Program.model_validate_json(self.store.get(row["program_ref"])), row

    def compatible(self, manifest: Manifest, inputs: dict[str, Any] | None = None,
                   side_effect: str | None = None, output_schema: dict[str, Any] | None = None) -> bool:
        try:
            if output_schema is not None and manifest.output_schema != output_schema:
                # General JSON Schema subsumption is undecidable for some extensions.
                # Require equal declared output contracts rather than guess compatibility.
                return False
            self.policy.validate_manifest(manifest, self.project_key, self.environment)
            if inputs is not None:
                Draft202012Validator(manifest.input_schema).validate(inputs)
            if side_effect is not None:
                levels = {"pure": 0, "read_only": 1, "workspace_write": 2, "external_write": 3}
                if levels[manifest.side_effect] > levels[side_effect]:
                    return False
        except (ATRError, ValueError):
            return False
        except Exception as exc:
            # jsonschema ValidationError deliberately stays inside the retrieval filter.
            from jsonschema.exceptions import ValidationError
            if isinstance(exc, ValidationError):
                return False
            raise
        return True

    @staticmethod
    def body(manifest: Manifest) -> str:
        return " ".join([manifest.id, manifest.description, *manifest.aliases, *manifest.tags,
                         canonical(manifest.input_schema), canonical(manifest.output_schema)])

    def search(self, query: str, *, inputs: dict[str, Any] | None = None,
               side_effect: str | None = None, limit: int = 3,
               include_inactive: bool = False, output_schema: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 20 or len(query) > 4096:
            raise ATRError("invalid_query", "search limit must be 1..20 and query <=4096 characters")
        # Exact identifiers/aliases first; search only selected tool versions.
        records: list[dict[str, Any]] = []
        tools = self.store.rows("""SELECT t.* FROM tools t JOIN active a
            ON t.id=a.id AND t.version=a.version WHERE t.state != 'deleted'""")
        candidates = self.store.rows("SELECT * FROM candidates WHERE state!='deleted'")
        for entity, rows in (("tool", tools), ("candidate", candidates)):
            for row in rows:
                manifest = Manifest.model_validate_json(row["manifest"])
                exact = query == row["id"] or query == manifest.id or query in manifest.aliases
                if exact:
                    records.append({**row, "entity": entity, "exact": True})
        tokens = re.findall(r"\w+", query, re.UNICODE)[:20]
        if tokens:
            # Quote each token; user input never becomes FTS operators or SQL.
            match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            hits = self.store.rows("SELECT entity,id,version FROM search WHERE search MATCH ? "
                                   "ORDER BY bm25(search) LIMIT 100", (match,))
            by_tool = {(r["id"], r["version"]): r for r in tools}
            by_candidate = {r["id"]: r for r in candidates}
            for hit in hits:
                found = by_tool.get((hit["id"], hit["version"])) if hit["entity"] == "tool" else (
                    by_candidate.get(hit["id"]))
                if found:
                    records.append({**found, "entity": hit["entity"], "exact": False})
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        now = self.clock().timestamp()
        for row in records:
            key = (row["entity"], row["id"])
            if key in seen:
                continue
            seen.add(key)
            manifest = Manifest.model_validate_json(row["manifest"])
            state = row["state"]
            if state in RESTRICTED:
                continue
            if row["entity"] == "candidate":
                if state != State.CANDIDATE or row["expires"] <= now:
                    continue
            elif not include_inactive and (state not in ACTIVE or not row["reviewed"]):
                continue
            if not self.compatible(manifest, inputs, side_effect, output_schema):
                continue
            result.append({"id": row["id"], "version": manifest.version,
                           "entity": row["entity"], "description": manifest.description[:240],
                           "state": state, "exact": row["exact"],
                           "reviewed": bool(row.get("reviewed", False))})
            if len(result) == limit:
                break
        return result

    def capture(self, program: Program, task: str, run_id: str) -> str | None:
        if not self.store.enabled("auto_create"):
            return None
        sig = signature(program)
        for row in self.store.rows("SELECT program_ref FROM tools WHERE state!='deleted'"):
            if signature(Program.model_validate_json(self.store.get(row["program_ref"]))) == sig:
                return None
        now = self.clock()
        candidate_id = "candidate." + uuid4().hex
        with self.store.transaction() as db:
            existing = db.execute("SELECT id,state,expires FROM candidates WHERE signature=?", (sig,)).fetchone()
            if existing and existing["state"] == State.CANDIDATE and existing["expires"] > now.timestamp():
                return str(existing["id"])
            if existing:
                db.execute("DELETE FROM search WHERE entity='candidate' AND id=?", (existing["id"],))
                db.execute("DELETE FROM candidates WHERE id=?", (existing["id"],))
            ref = self.store.put(canonical(program).encode())
            db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)", (
                candidate_id, ref, sig, canonical(program.manifest), now.timestamp(),
                (now + timedelta(days=self.settings.candidate_days)).timestamp(),
                task, run_id, State.CANDIDATE))
            db.execute("INSERT INTO search VALUES (?,?,?,?)", (
                "candidate", candidate_id, program.manifest.version, self.body(program.manifest)))
        self.store.emit("candidate_saved", now, task=task, run_id=run_id, candidate_id=candidate_id)
        return candidate_id

    def _dependencies(self, manifest: Manifest) -> None:
        seen: set[tuple[str, str]] = set()

        def visit(tool_id: str, version: str, depth: int) -> None:
            if tool_id == manifest.id or depth > 16:
                raise ATRError("dependency_cycle", "tool dependency cycle or depth limit")
            if (tool_id, version) in seen:
                return
            seen.add((tool_id, version))
            program, row = self.program(tool_id, version)
            if not row["reviewed"] or row["state"] in RESTRICTED:
                raise ATRError("dependency_unavailable", "dependencies must be reviewed and available")
            child = program.manifest
            evidence = json.loads(row["review"]) if row["review"] else {}
            if evidence.get("environment") is not None and evidence["environment"] != self.environment:
                raise ATRError("environment_mismatch", "dependency environment requires revalidation")
            self.policy.validate_manifest(child, self.project_key, self.environment)
            levels = {"pure": 0, "read_only": 1, "workspace_write": 2, "external_write": 3}
            if levels[child.side_effect] > levels[manifest.side_effect]:
                raise ATRError("permission_escalation", "workflow understates dependency side effects")
            for capability in ("file_read", "file_write", "network", "secrets"):
                if not set(getattr(child.permissions, capability)) <= set(
                        getattr(manifest.permissions, capability)):
                    raise ATRError("permission_escalation", "workflow must declare dependency capabilities")
            for dependency in child.dependencies:
                visit(dependency.tool_id, dependency.version, depth + 1)
        for dependency in manifest.dependencies:
            visit(dependency.tool_id, dependency.version, 0)

    def create(self, program: Program, task: str, base_version: str | None = None) -> dict[str, Any]:
        self.policy.validate_manifest(program.manifest, self.project_key, self.environment)
        self._dependencies(program.manifest)
        now = self.clock()
        sig = signature(program)
        # Include inactive/unchecked versions in deduplication; they cannot be silently re-created.
        for row in self.store.rows("SELECT * FROM tools WHERE state!='deleted'"):
            other = Program.model_validate_json(self.store.get(row["program_ref"]))
            if signature(other) == sig and base_version is None:
                return {"created": False, "tool_id": row["id"], "version": row["version"],
                        "state": row["state"], "review_required": not bool(row["reviewed"])}
        manifest = program.manifest
        with self.store.transaction() as db:
            self._dependencies(manifest)
            if base_version is None:
                for duplicate in db.execute("SELECT * FROM tools WHERE state!='deleted'").fetchall():
                    saved = Program.model_validate_json(self.store.get(duplicate["program_ref"]))
                    if signature(saved) == sig:
                        return {"created": False, "tool_id": duplicate["id"], "version": duplicate["version"],
                                "state": duplicate["state"], "review_required": not bool(duplicate["reviewed"])}
            active = db.execute("SELECT version FROM active WHERE id=?", (manifest.id,)).fetchone()
            if active and base_version is None:
                raise ATRError("base_version_required", "updates must name the selected base_version")
            if base_version is not None:
                if not active or active["version"] != base_version:
                    raise ATRError("stale_version", "selected tool version changed")
                if int(manifest.version[1:]) <= int(base_version[1:]):
                    raise ATRError("invalid_version", "new version must increase")
            if db.execute("SELECT 1 FROM tools WHERE id=? AND version=?", (
                    manifest.id, manifest.version)).fetchone():
                raise ATRError("immutable_version", "tool version already exists")
            db.execute("INSERT OR IGNORE INTO task_budget(task) VALUES (?)", (task,))
            used = db.execute("SELECT creations FROM task_budget WHERE task=?", (task,)).fetchone()[0]
            authority = db.execute("SELECT max_creations FROM task_authority WHERE id=?", (task,)).fetchone()
            creation_limit = min(self.settings.max_creations_per_task, authority[0]) if authority else self.settings.max_creations_per_task
            if used >= creation_limit:
                raise ATRError("creation_budget", "task tool creation budget exhausted")
            repairing = base_version is not None and db.execute(
                "SELECT 1 FROM tools WHERE id=? AND state='quarantined'", (manifest.id,)).fetchone()
            if repairing:
                repairs = db.execute("SELECT repairs FROM task_budget WHERE task=?", (task,)).fetchone()[0]
                if repairs >= self.settings.max_repairs:
                    raise ATRError("repair_budget", "task tool repair budget exhausted")
                db.execute("UPDATE task_budget SET repairs=repairs+1 WHERE task=?", (task,))
            db.execute("UPDATE task_budget SET creations=creations+1 WHERE task=?", (task,))
            ref = self.store.put(canonical(program).encode())
            db.execute("""INSERT INTO tools
                (id,version,program_ref,manifest,state,created,window_start)
                VALUES (?,?,?,?,?,?,?)""", (manifest.id, manifest.version, ref, canonical(manifest),
                                           State.TEMPORARY, now.timestamp(), now.timestamp()))
            if not active:
                db.execute("INSERT INTO active VALUES (?,?)", (manifest.id, manifest.version))
            for dep in manifest.dependencies:
                db.execute("INSERT INTO dependencies VALUES (?,?,?,?)", (
                    manifest.id, manifest.version, dep.tool_id, dep.version))
            db.execute("INSERT INTO search VALUES (?,?,?,?)", (
                "tool", manifest.id, manifest.version, self.body(manifest)))
            for candidate in db.execute("SELECT id FROM candidates WHERE signature=?", (sig,)).fetchall():
                db.execute("UPDATE candidates SET state='archived' WHERE id=?", (candidate["id"],))
                db.execute("DELETE FROM search WHERE entity='candidate' AND id=?", (candidate["id"],))
        self.store.emit("tool_created", now, task=task, tool_id=manifest.id, version=manifest.version)
        return {"created": True, "tool_id": manifest.id, "version": manifest.version,
                "state": State.TEMPORARY, "review_required": True}

    def referenced(self, tool_id: str, version: str) -> bool:
        # Protect transitive dependency chains through archived intermediate versions.
        def protected(child: tuple[str, str], seen: set[tuple[str, str]]) -> bool:
            if child in seen:
                return False
            seen.add(child)
            parents = self.store.rows("""SELECT t.* FROM dependencies d JOIN tools t
                ON d.parent_id=t.id AND d.parent_version=t.version
                WHERE d.child_id=? AND d.child_version=? AND t.state!='deleted'""", child)
            return any(p["state"] in ACTIVE or protected((p["id"], p["version"]), seen)
                       for p in parents)
        return protected((tool_id, version), set())

    def stats(self, tool_id: str, version: str) -> dict[str, int]:
        _, row = self.program(tool_id, version)
        runs = self.store.rows("""SELECT * FROM runs WHERE tool_id=? AND version=?
            AND epoch=? AND review_run=0""", (tool_id, version, row["epoch"]))
        eligible = [r for r in runs if r["eligible"]]
        inputs_by_task: dict[str, set[str]] = {}
        for run in eligible:
            inputs_by_task.setdefault(run["task"], set()).add(run["input_key"])
        return {"total_calls": len(runs), "eligible_uses": len(eligible),
                "failed_calls": sum(json.loads(r["result"])["status"] in (
                    "failed", "timed_out", "cancelled") for r in runs),
                "direct_uses": sum(not r["parent"] for r in runs),
                "indirect_uses": sum(bool(r["parent"]) for r in runs),
                "distinct_root_tasks": len({r["task"] for r in eligible}),
                "distinct_inputs": len({r["input_key"] for r in eligible}),
                "same_task_distinct_inputs": max((len(values) for values in inputs_by_task.values()), default=0)}

    def delete(self, tool_id: str, version: str | None = None, *, purge: bool = False) -> dict[str, Any]:
        _, row = self.program(tool_id, version)
        version = row["version"]
        if self.referenced(tool_id, version):
            raise ATRError("dependency_protected", "version is required by an active dependency chain")
        if self.store.one("SELECT id FROM runs WHERE tool_id=? AND version=? AND finished IS NULL",
                          (tool_id, version)):
            raise ATRError("running_reference", "version has an unfinished execution")
        now = self.clock()
        state = State.DELETED if purge else State.ARCHIVED
        with self.store.transaction() as db:
            if self.referenced(tool_id, version):
                raise ATRError("dependency_protected", "version is required by an active dependency chain")
            if db.execute("SELECT 1 FROM runs WHERE tool_id=? AND version=? AND finished IS NULL",
                          (tool_id, version)).fetchone():
                raise ATRError("running_reference", "version has an unfinished execution")
            db.execute("UPDATE tools SET state=?,archived=? WHERE id=? AND version=?",
                       (state, now.timestamp(), tool_id, version))
            db.execute("DELETE FROM search WHERE entity='tool' AND id=? AND version=?", (tool_id, version))
            if purge:
                db.execute("DELETE FROM active WHERE id=? AND version=?", (tool_id, version))
        self.store.emit("tool_deleted" if purge else "tool_archived", now,
                        tool_id=tool_id, version=version)
        return {"tool_id": tool_id, "version": version, "state": state}

    def rollback(self, tool_id: str, version: str, *, expected_active: str | None = None) -> dict[str, str]:
        program, row = self.program(tool_id, version)
        self.policy.validate_manifest(program.manifest, self.project_key, self.environment)
        self._dependencies(program.manifest)
        if not row["reviewed"] or row["state"] not in ACTIVE:
            raise ATRError("review_required", "rollback requires an active reviewed version")
        with self.store.transaction() as db:
            current = db.execute("SELECT reviewed,state FROM tools WHERE id=? AND version=?",
                                 (tool_id, version)).fetchone()
            if not current or not current["reviewed"] or current["state"] not in ACTIVE:
                raise ATRError("review_required", "rollback target changed before selection")
            if expected_active is not None:
                if not db.execute("UPDATE active SET version=? WHERE id=? AND version=?",
                                  (version, tool_id, expected_active)).rowcount:
                    raise ATRError("stale_version", "active version no longer matches the failed version")
            else:
                db.execute("INSERT INTO active VALUES (?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                           (tool_id, version))
        self.store.emit("tool_rollback", self.clock(), tool_id=tool_id, version=version)
        return {"tool_id": tool_id, "version": version}
