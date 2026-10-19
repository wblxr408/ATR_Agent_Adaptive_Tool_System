"""Trusted control-plane grants, never populated from execution request text."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from atr.errors import ATRError
from atr.models import Contract, Limits, Manifest, Permissions


class Policy(Contract):
    allow_workspace_read: bool = True
    allow_workspace_write: bool = False
    allow_external_write: bool = False
    allow_external_review: bool = False
    network_capabilities: list[str] = Field(default_factory=list)
    secret_capabilities: list[str] = Field(default_factory=list)
    max_limits: Limits = Field(default_factory=lambda: Limits(
        timeout_seconds=300, memory_mb=1024, cpus=2, pids=128,
        output_bytes=4_194_304, artifact_bytes=16_777_216))
    excluded_names: list[str] = Field(default_factory=lambda: [
        ".git", ".env", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".codex",
        ".claude", ".agents", "AGENTS.md", "CLAUDE.md", ".venv", "node_modules", "registry.sqlite3", "credentials.json",
        "id_rsa", "id_ed25519"])
    protected_relative_paths: list[str] = Field(default_factory=list)

    def authorize(self, permissions: Permissions, side_effect: str, limits: Limits,
                  *, adapter: bool = False) -> None:
        if permissions.file_read and not self.allow_workspace_read:
            raise ATRError("permission_denied", "workspace reading is not granted")
        if "workspace" in permissions.file_write and not self.allow_workspace_write:
            raise ATRError("permission_denied", "workspace writing requires an operator grant")
        if side_effect == "workspace_write" and "workspace" not in permissions.file_write:
            raise ATRError("permission_denied", "workspace writes require explicit capability")
        if "workspace" in permissions.file_write and side_effect not in (
                "workspace_write", "external_write"):
            raise ATRError("permission_denied", "writable tools must declare their side effects")
        if side_effect == "external_write" and not self.allow_external_write:
            raise ATRError("permission_denied", "external writing requires an operator grant")
        if not set(permissions.network) <= set(self.network_capabilities):
            raise ATRError("permission_denied", "network capability has not been granted")
        if not set(permissions.secrets) <= set(self.secret_capabilities):
            raise ATRError("permission_denied", "secret capability has not been granted")
        if not adapter and (permissions.network or permissions.secrets):
            raise ATRError("permission_denied", "raw processes cannot receive network or secrets")
        for key in ("timeout_seconds", "memory_mb", "cpus", "pids", "output_bytes", "artifact_bytes"):
            if getattr(limits, key) > getattr(self.max_limits, key):
                raise ATRError("resource_limit", f"requested {key} exceeds operator limit")

    def validate_manifest(self, manifest: Manifest, project_key: str, environment: str) -> None:
        if manifest.scope == "project" and manifest.project_key != project_key:
            raise ATRError("scope_mismatch", "tool belongs to another project")
        if manifest.environment and manifest.environment != environment:
            raise ATRError("environment_mismatch", "tool environment requires revalidation")
        self.authorize(manifest.permissions, manifest.side_effect, manifest.runtime,
                       adapter=manifest.kind != "script")

    def readable_path(self, workspace: Path, name: str) -> Path:
        candidate = workspace / name
        if not self.allow_workspace_read:
            raise ATRError("permission_denied", "workspace reading is not granted")
        try:
            relative = candidate.resolve().relative_to(workspace.resolve())
        except ValueError as exc:
            raise ATRError("permission_denied", "path leaves workspace") from exc
        if self.excluded(relative) or any(is_link(p) for p in [candidate, *candidate.parents]
                                          if p != workspace.parent):
            raise ATRError("permission_denied", "sensitive paths and links are excluded")
        return candidate

    def excluded(self, relative: Path) -> bool:
        return any(relative.is_relative_to(Path(path)) for path in self.protected_relative_paths) or any(part.lower() in {x.lower() for x in self.excluded_names}
                   or part.lower().startswith(".env.") or part.lower().endswith(
                       (".pem", ".key", ".p12", ".pfx")) for part in relative.parts)


def ensure_separate(home: Path, workspace: Path) -> None:
    home, workspace = home.resolve(), workspace.resolve()
    if home == workspace or home.is_relative_to(workspace) or workspace.is_relative_to(home):
        raise ATRError("unsafe_layout", "control home and workspace must be separate directory trees")
    if any(is_link(p) for p in (home, workspace)):
        raise ATRError("unsafe_layout", "control home and workspace cannot be links")


def default_home() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "atr"


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()
