"""Public data contracts; data supplied by a model never contains authority."""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Json = dict[str, Any]


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class State(StrEnum):
    CANDIDATE = "candidate"
    TEMPORARY = "temporary"
    PERSISTENT = "persistent"
    ARCHIVED = "archived"
    DORMANT = "dormant"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"
    DELETED = "deleted"


class Permissions(Contract):
    file_read: list[Literal["workspace"]] = Field(default_factory=list)
    file_write: list[Literal["artifacts", "workspace"]] = ["artifacts"]
    network: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)


class Limits(Contract):
    timeout_seconds: float = Field(default=30, gt=0, le=3600)
    memory_mb: int = Field(default=256, ge=32, le=8192)
    cpus: float = Field(default=1, gt=0, le=8)
    pids: int = Field(default=64, ge=8, le=512)
    output_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    artifact_bytes: int = Field(default=8_388_608, ge=1024, le=67_108_864)


class Dependency(Contract):
    tool_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    version: str = Field(pattern=r"^v[1-9][0-9]*$")


class Check(Contract):
    inputs: Json = Field(default_factory=dict)
    expected: Any
    name: str = "contract"
    expected_path: str = ""


class Step(Contract):
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    tool_id: str
    version: str
    inputs: Json = Field(default_factory=dict)


class Manifest(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    version: str = Field(default="v1", pattern=r"^v[1-9][0-9]*$")
    description: str = Field(min_length=1, max_length=4096)
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    kind: Literal["script", "workflow", "model_pipeline", "adapter"] = "script"
    scope: Literal["global", "project", "environment"] = "project"
    project_key: str | None = None
    environment: str | None = None
    entrypoint: list[str] = Field(default_factory=lambda: ["python", "/tool/main.py"])
    input_schema: Json = Field(default_factory=lambda: {"type": "object"})
    output_schema: Json = Field(default_factory=dict)
    permissions: Permissions = Field(default_factory=Permissions)
    runtime: Limits = Field(default_factory=Limits)
    dependencies: list[Dependency] = Field(default_factory=list)
    side_effect: Literal["pure", "read_only", "workspace_write", "external_write"] = "pure"
    checks: list[Check] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    adapter: str | None = None
    freshness_seconds: int = Field(default=0, ge=0, le=31_536_000)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def valid_schema(cls, value: Json) -> Json:
        Draft202012Validator.check_schema(value)
        # Contracts are local. Never let a schema validator fetch a remote resource.
        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in ("$ref", "$dynamicRef") and not str(child).startswith("#"):
                        raise ValueError("only local JSON Schema references are allowed")
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
        visit(value)
        return value

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.scope == "project" and not self.project_key:
            raise ValueError("project scope requires project_key")
        if self.scope == "environment" and not self.environment:
            raise ValueError("environment scope requires environment fingerprint")
        if not self.entrypoint or any("\x00" in x for x in self.entrypoint):
            raise ValueError("entrypoint must be a nonempty argument array")
        if self.side_effect == "workspace_write" and "workspace" not in self.permissions.file_write:
            raise ValueError("workspace_write requires the workspace write permission")
        if self.kind == "workflow":
            if not self.steps or len({s.name for s in self.steps}) != len(self.steps):
                raise ValueError("workflow requires uniquely named steps")
            pinned = {(d.tool_id, d.version) for d in self.dependencies}
            if any((s.tool_id, s.version) not in pinned for s in self.steps):
                raise ValueError("every workflow step must be a pinned dependency")
        if self.kind in ("adapter", "model_pipeline") and not self.adapter:
            raise ValueError("adapter or model pipeline requires an adapter capability name")
        if len(self.dependencies) > 32 or len(self.steps) > 64:
            raise ValueError("dependency/workflow limit exceeded")
        return self


class Program(Contract):
    manifest: Manifest
    files: dict[str, str] = Field(default_factory=dict)
    binary_files: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def safe_files(cls, files: dict[str, str]) -> dict[str, str]:
        if len(files) > 128 or sum(len(v.encode()) for v in files.values()) > 8_388_608:
            raise ValueError("tool bundle too large")
        for name in files:
            path = PurePosixPath(name)
            if (not name or not path.parts or str(path) != name or name.endswith("/")
                    or any(part.endswith((".", " ")) or part.split(".")[0].upper() in {
                        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))} for part in path.parts)
                    or path.is_absolute() or ".." in path.parts or "\\" in name
                    or ":" in name or re.search(r"[\x00-\x1f]", name)):
                raise ValueError("bundle paths must be safe relative POSIX paths")
        return files

    @field_validator("binary_files")
    @classmethod
    def valid_binary(cls, files: dict[str, str]) -> dict[str, str]:
        cls.safe_files(files)
        try:
            if sum(len(base64.b64decode(value, validate=True)) for value in files.values()) > 8_388_608:
                raise ValueError("binary tool bundle too large")
        except (binascii.Error, UnicodeError) as exc:
            raise ValueError("binary files must use valid base64") from exc
        return files

    @model_validator(mode="after")
    def no_duplicate_files(self) -> Self:
        if set(self.files) & set(self.binary_files):
            raise ValueError("text and binary files cannot overlap")
        if sum(len(v) for v in self.sandbox_files().values()) > 8_388_608:
            raise ValueError("combined tool bundle too large")
        return self

    def sandbox_files(self) -> dict[str, bytes]:
        return {**{k: v.encode("utf-8") for k, v in self.files.items()},
                **{k: base64.b64decode(v, validate=True) for k, v in self.binary_files.items()}}


class ExecuteRequest(Contract):
    root_task_id: str = Field(min_length=1, max_length=200)
    mode: Literal["direct", "tool"] = "direct"
    argv: list[str] | None = None
    program: Program | None = None
    tool_id: str | None = None
    version: str | None = None
    inputs: Json = Field(default_factory=dict)
    operation_id: str | None = Field(default=None, min_length=1, max_length=200)
    retry_of: str | None = None
    cache_hit: bool = False
    generated_independent: bool = False
    intent: str = Field(default="", max_length=4096)
    permissions: Permissions = Field(default_factory=Permissions)
    limits: Limits = Field(default_factory=Limits)
    side_effect: Literal["pure", "read_only", "workspace_write", "external_write"] = "read_only"
    background: bool = False

    @model_validator(mode="after")
    def target(self) -> Self:
        if self.mode == "tool":
            if not self.tool_id or self.argv is not None or self.program is not None:
                raise ValueError("tool execution requires only tool_id and inputs")
        elif (self.argv is None) == (self.program is None):
            raise ValueError("direct execution requires either argv or program")
        if self.argv is not None and (not self.argv or any("\x00" in a for a in self.argv)):
            raise ValueError("argv must be a nonempty argument array")
        if self.generated_independent and not self.program:
            raise ValueError("capture requires an explicit independent program")
        return self


class Artifact(Contract):
    ref: str
    type: str = "text/plain"
    size_bytes: int
    name: str


class Cost(Contract):
    category: Literal["main_llm", "router_llm", "creator_llm", "review_llm", "nested_llm",
                      "external_api", "compute", "storage", "fail_retry"]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str = "USD"
    provider: str | None = None
    billing_ref: str | None = None
    estimated: bool = False

    @model_validator(mode="after")
    def cached_subset(self) -> Self:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached tokens cannot exceed input tokens")
        return self


class ExecutionResult(Contract):
    run_id: str
    mode: Literal["direct", "tool"]
    tool_id: str | None = None
    version: str | None = None
    status: Literal["running", "succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int | None = None
    summary: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    stdout_excerpt: str = ""
    validation_status: Literal["passed", "failed", "unknown"] = "unknown"
    duration_ms: int = 0
    truncated: bool = False
    candidate_id: str | None = None
    error_code: str | None = None
    suggested_fallback: Dependency | None = None


class CreateRequest(Contract):
    root_task_id: str = Field(min_length=1)
    program: Program | None = None
    candidate_id: str | None = None
    base_version: str | None = None

    @model_validator(mode="after")
    def source(self) -> Self:
        if (self.program is None) == (self.candidate_id is None):
            raise ValueError("choose program or candidate_id")
        return self


class ReviewReport(Contract):
    tool_id: str
    version: str
    passed: bool
    checked_at: datetime
    evidence: list[Json] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    fact_status: Literal["unknown", "verified"] = "unknown"
    environment: str | None = None


class Intent(Contract):
    root_task_id: str
    summary: str = Field(min_length=1, max_length=4096)
    tool_id: str | None = None
    inputs: Json = Field(default_factory=dict)
    output_schema: Json | None = None
    simple_command: bool = False
    explicit_tool_creation: bool = False
    independent_program: bool = False
    side_effect: Literal["pure", "read_only", "workspace_write", "external_write"] = "read_only"


class Route(Contract):
    decision: Literal["DIRECT", "REUSE", "EXECUTE_AND_OBSERVE", "CREATE", "DEFER"]
    selected: str | None = None
    reason: str
    model_calls: int = 0


class Settings(Contract):
    candidate_days: int = Field(default=14, ge=1)
    temporary_days: int = Field(default=14, ge=1)
    archive_days: int = Field(default=30, ge=1)
    dormant_days: int = Field(default=90, ge=1)
    promotion_uses: int = Field(default=5, ge=1)
    promotion_tasks: int = Field(default=2, ge=1)
    same_task_uses: int | None = Field(default=10, ge=1)
    max_repairs: int = Field(default=2, ge=0, le=10)
    max_creations_per_task: int = Field(default=8, ge=0, le=100)
    max_task_cost: float = Field(default=5, ge=0, allow_inf_nan=False)
    raw_log_days: int = Field(default=30, ge=0, le=30)
    aggregate_days: int = Field(default=180, ge=1)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    excerpt_chars: int = Field(default=2000, ge=64, le=16000)
    max_workspace_bytes: int = Field(default=67_108_864, ge=1024)
    image: str = "atr-sandbox:0.1"
    compute_usd_per_second: float | None = Field(default=None, ge=0)
    storage_usd_per_mb: float | None = Field(default=None, ge=0)


class Event(Contract):
    event_id: str
    kind: str
    timestamp: datetime
    root_task_id: str | None = None
    run_id: str | None = None
    tool_id: str | None = None
    version: str | None = None
    data: Json = Field(default_factory=dict)
