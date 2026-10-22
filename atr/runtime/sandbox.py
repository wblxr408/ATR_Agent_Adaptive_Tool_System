"""Docker-only subprocess boundary with bounded output and process-tree cancellation."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from uuid import uuid4

from atr.errors import ATRError
from atr.models import Limits, Permissions
from atr.policy import Policy, is_link


@dataclass
class SandboxSpec:
    argv: list[str]
    stdin: bytes
    files: dict[str, bytes]
    workspace: Path
    permissions: Permissions
    limits: Limits
    policy: Policy
    max_workspace_bytes: int
    environment: str | None = None


@dataclass
class SandboxOutcome:
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    files: dict[str, bytes] = field(default_factory=dict)
    truncated: bool = False
    error_code: str | None = None


class Sandbox(Protocol):
    def run(self, spec: SandboxSpec, cancel: threading.Event) -> SandboxOutcome: ...


def snapshot(workspace: Path, destination: Path, policy: Policy, maximum: int) -> None:
    used = 0
    for root, dirs, files in os.walk(workspace, followlinks=False):
        base = Path(root)
        dirs[:] = [d for d in dirs if not is_link(base / d)
                   and not policy.excluded((base / d).relative_to(workspace))]
        target = destination / base.relative_to(workspace)
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            source = base / name
            if is_link(source) or policy.excluded(source.relative_to(workspace)):
                continue
            if not source.is_file():
                continue
            size = source.stat().st_size
            used += size
            if used > maximum:
                raise ATRError("workspace_limit", "workspace snapshot exceeds configured byte budget")
            # Copy from an opened regular file; the snapshot never contains source links.
            with source.open("rb") as inp, (target / name).open("wb") as out:
                shutil.copyfileobj(inp, out, length=65536)


class DockerSandbox:
    def __init__(self, image: str = "atr-sandbox:0.1", executable: str = "docker") -> None:
        self.image = image
        self.executable = executable

    def fingerprint(self) -> str:
        try:
            inspected = subprocess.run([self.executable, "image", "inspect", "--format", "{{.Id}} {{.Os}}", self.image],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ATRError("sandbox_unavailable", "sandbox image metadata is unavailable") from exc
        parts = inspected.stdout.decode(errors="replace").strip().split()
        if inspected.returncode or len(parts) != 2 or parts[1] != "linux" or not parts[0].startswith("sha256:"):
            raise ATRError("sandbox_unavailable", "prepare a Linux sandbox image before executing tools")
        return "oci:" + parts[0]

    def run(self, spec: SandboxSpec, cancel: threading.Event) -> SandboxOutcome:
        if cancel.is_set():
            return SandboxOutcome("cancelled")
        environment = self.fingerprint()
        if spec.environment and spec.environment != environment:
            raise ATRError("environment_mismatch", "sandbox image changed since contract validation")
        with tempfile.TemporaryDirectory(prefix="atr-data-") as temporary:
            base = Path(temporary)
            code, work, output = (base / x for x in ("tool", "workspace", "out"))
            for path in (code, work, output):
                path.mkdir()
            for name, content in spec.files.items():
                target = code / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            # Even write-capable executions get filtered staging. Changes are committed
            # under the project's write lock only after the process has stopped.
            if spec.permissions.file_read or "workspace" in spec.permissions.file_write:
                snapshot(spec.workspace, work, spec.policy, spec.max_workspace_bytes)
            writable = "workspace" in spec.permissions.file_write
            baseline = self._collect(work, spec.max_workspace_bytes) if writable else {}
            container = "atr-" + uuid4().hex
            command = [self.executable, "run", "--rm", "--pull=never", "--name", container,
                       "--network=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--user", "65534:65534",
                       "--cpus", str(spec.limits.cpus), "--memory", f"{spec.limits.memory_mb}m",
                       "--memory-swap", f"{spec.limits.memory_mb}m", "--pids-limit", str(spec.limits.pids),
                       "--ulimit", f"fsize={spec.limits.artifact_bytes}:{spec.limits.artifact_bytes}",
                       "--log-driver=none", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
                       "--workdir", "/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1",
                       "--env", "PYTHONUNBUFFERED=1", "-i"]
            for host, guest, readonly in ((code, "/tool", True),
                                           (work, "/workspace", not writable),
                                           (output, "/out", False)):
                if "," in str(host):
                    return SandboxOutcome("failed", error_code="invalid_mount")
                command += ["--mount", f"type=bind,source={host.resolve()},target={guest}"
                            + (",readonly" if readonly else "")]
            # Unprivileged Linux needs write permission on these ephemeral directories.
            for root, _dirs, files in os.walk(base):
                Path(root).chmod(0o777)
                for name in files:
                    (Path(root) / name).chmod(0o666 if writable and Path(root).is_relative_to(work)
                                               else 0o755)
            command += ["--entrypoint", spec.argv[0], environment.removeprefix("oci:"), *spec.argv[1:]]
            outcome = self._process(command, spec.stdin, spec.limits, cancel, container)
            if outcome.status == "succeeded":
                if "artifacts" in spec.permissions.file_write:
                    outcome.files = self._collect(output, spec.limits.artifact_bytes)
                if writable:
                    changes = self._commit(work, spec.workspace, spec.policy, spec.max_workspace_bytes, baseline)
                    outcome.files["workspace_changes.json"] = json.dumps(changes, ensure_ascii=False).encode()
            return outcome

    def _process(self, command: list[str], stdin: bytes, limits: Limits,
                 cancel: threading.Event, container: str) -> SandboxOutcome:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, creationflags=flags)
        except OSError:
            return SandboxOutcome("failed", stderr=b"Docker executable unavailable",
                                  error_code="sandbox_unavailable")
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        counts = [0, 0]
        output_lock = threading.Lock()

        def drain(stream: BinaryIO, index: int) -> None:
            while chunk := stream.read(4096):
                with output_lock:
                    remaining = max(0, limits.output_bytes - sum(counts))
                    buffers[index].extend(chunk[:remaining])
                    counts[index] += len(chunk)
                    if sum(counts) > limits.output_bytes:
                        overflow.set()

        def feed() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(stdin)
                process.stdin.close()
            except (OSError, ValueError):
                pass

        readers = [threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, 1), daemon=True)]
        writer = threading.Thread(target=feed, daemon=True)
        for thread in [*readers, writer]:
            thread.start()
        started = time.monotonic()
        status: Literal["succeeded", "failed", "timed_out", "cancelled"] = "succeeded"
        error = None
        while process.poll() is None:
            if cancel.is_set() or overflow.is_set() or time.monotonic() - started > limits.timeout_seconds:
                status = "cancelled" if cancel.is_set() else (
                    "failed" if overflow.is_set() else "timed_out")
                error = "output_limit" if overflow.is_set() else None
                # Killing docker's client alone would leave its container running.
                try:
                    subprocess.run([self.executable, "rm", "--force", container],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10, creationflags=flags)
                except (OSError, subprocess.TimeoutExpired):
                    error = "sandbox_cleanup_failed"
                process.kill()
                break
            cancel.wait(0.02)
        process.wait()
        for thread in [*readers, writer]:
            thread.join(timeout=2)
        if overflow.is_set():
            status, error = "failed", "output_limit"
        if status == "succeeded" and process.returncode != 0:
            status = "failed"
            error = "sandbox_unavailable" if process.returncode in (125, 126, 127) else "execution_failed"
        return SandboxOutcome(status, process.returncode, bytes(buffers[0]), bytes(buffers[1]),
                              truncated=overflow.is_set(), error_code=error)

    @staticmethod
    def _collect(output: Path, maximum: int) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        used = 0
        for root, dirs, files in os.walk(output, followlinks=False):
            dirs[:] = [d for d in dirs if not is_link(Path(root) / d)]
            for name in files:
                path = Path(root) / name
                if is_link(path) or not path.is_file():
                    raise ATRError("unsafe_output", "sandbox output contains a link or non-file")
                used += path.stat().st_size
                if used > maximum or len(result) >= 128:
                    raise ATRError("artifact_limit", "sandbox artifacts exceed budget")
                result[path.relative_to(output).as_posix()] = path.read_bytes()
        return result

    @classmethod
    def _commit(cls, staged: Path, workspace: Path, policy: Policy, maximum: int,
                baseline: dict[str, bytes]) -> dict[str, object]:
        targets, changes = cls._plan_commit(staged, workspace, policy, maximum, baseline)
        cls._apply_commit(targets)
        return changes

    @classmethod
    def _plan_commit(cls, staged: Path, workspace: Path, policy: Policy, maximum: int,
                     baseline: dict[str, bytes]) -> tuple[list[tuple[Path, bytes | None]], dict[str, object]]:
        files = cls._collect(staged, maximum)
        targets: list[tuple[Path, bytes | None]] = []
        diffs: list[str] = []
        changed: list[str] = []
        # Validate all paths and stale inputs before modifying any user file.
        for name in sorted(set(files) | set(baseline)):
            before, content = baseline.get(name), files.get(name)
            if before == content:
                continue
            relative = Path(name)
            if policy.excluded(relative):
                raise ATRError("permission_denied", "tool tried to write a protected workspace path")
            target = policy.readable_path(workspace, name)
            current = target.read_bytes() if target.is_file() else None
            if current != before:
                raise ATRError("workspace_conflict", "workspace changed while the tool was running")
            targets.append((target, content))
            changed.append(name)
            if (before is None or b"\x00" not in before) and (content is None or b"\x00" not in content):
                diff = "".join(difflib.unified_diff(
                    (before or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                    (content or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                    fromfile="a/" + name, tofile="b/" + name))
                diffs.append(diff[:65536])
        return targets, {"changed_paths": changed, "diff": "\n".join(diffs)[:262144],
                         "diff_truncated": sum(len(d) for d in diffs) > 262144}

    @staticmethod
    def _apply_commit(targets: list[tuple[Path, bytes | None]]) -> None:
        for target, content in targets:
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".atr-write-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.replace(name, target)
            finally:
                if os.path.isfile(name):
                    os.unlink(name)
