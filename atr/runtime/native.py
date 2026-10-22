"""Operator-trusted Python bundles. This is NOT an OS security sandbox.

No shell, free argv, network adapter, workspace access, or model-issued trust.
CPU/memory/PID isolation requires the existing Docker backend instead.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Literal

from atr.errors import ATRError
from atr.models import Program
from atr.runtime.sandbox import SandboxOutcome, SandboxSpec


def _job(process: subprocess.Popen[bytes]) -> tuple[Any, Any] | None:
    """An unnamed Windows job owns children, including after the parent exits."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.flags = (
        0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, including host process failure.
    )
    process_handle = process._handle  # type: ignore[attr-defined]  # Windows Popen native handle.
    if (
        not job
        or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        or not kernel.AssignProcessToJobObject(job, int(process_handle))
    ):
        if job:
            kernel.CloseHandle(job)
        process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                pipe.close()
        raise ATRError("native_job", "could not attach native process to its cleanup job")
    return kernel, job


class NativeSandbox:
    def __init__(self) -> None:
        self.approved: list[tuple[list[str], dict[str, bytes]]] = []

    @staticmethod
    def fingerprint() -> str:
        return f"native-python:{sys.version}:{sys.executable}:{sys.platform}"

    def approve(self, program: Program) -> None:
        manifest = program.manifest
        if (
            manifest.kind != "script"
            or manifest.side_effect != "pure"
            or manifest.dependencies
            or manifest.permissions.file_read
            or manifest.permissions.network
            or manifest.permissions.secrets
            or set(manifest.permissions.file_write) - {"artifacts"}
            or manifest.entrypoint != ["python", "/tool/main.py"]
            or "main.py" not in program.files
            or program.binary_files
        ):
            raise ATRError(
                "native_policy", "native tools must be pure stdlib Python main.py bundles"
            )
        self.approved.append((manifest.entrypoint, program.sandbox_files()))

    def run(self, spec: SandboxSpec, cancel: threading.Event) -> SandboxOutcome:
        if not any(spec.argv == argv and spec.files == files for argv, files in self.approved):
            raise ATRError(
                "native_not_approved", "operator has not approved this exact native bundle"
            )
        if spec.environment and spec.environment != self.fingerprint():
            raise ATRError("environment_mismatch", "native interpreter changed")
        if (
            spec.permissions.file_read
            or spec.permissions.network
            or spec.permissions.secrets
            or set(spec.permissions.file_write) - {"artifacts"}
        ):
            raise ATRError(
                "native_policy",
                "native integration does not grant filesystem or network operations",
            )
        if cancel.is_set():
            return SandboxOutcome("cancelled", error_code="cancelled")
        with tempfile.TemporaryDirectory(prefix="atr-native-") as temporary:
            base = Path(temporary)
            for name, content in spec.files.items():
                Program.safe_files({name: ""})
                target = base / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
            env.update(TEMP=temporary, TMP=temporary, TMPDIR=temporary)
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-X", "utf8", "-u", str(base / "main.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=base,
                env=env,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            job = _job(process)
            chunks: list[bytearray] = [bytearray(), bytearray()]
            lock, exceeded = threading.Lock(), threading.Event()

            def drain(stream: Any, index: int) -> None:
                try:
                    while block := stream.read(4096):
                        with lock:
                            remaining = spec.limits.output_bytes - sum(map(len, chunks))
                            chunks[index].extend(block[: max(0, remaining)])
                            if len(block) > remaining:
                                exceeded.set()
                finally:
                    stream.close()

            def feed() -> None:
                assert process.stdin
                try:
                    process.stdin.write(spec.stdin)
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    process.stdin.close()

            assert process.stdout and process.stderr
            threads = [
                threading.Thread(target=drain, args=(process.stdout, 0)),
                threading.Thread(target=drain, args=(process.stderr, 1)),
                threading.Thread(target=feed),
            ]
            status: Literal["succeeded", "failed", "timed_out", "cancelled"] = "succeeded"
            error: str | None = None
            deadline = time.monotonic() + spec.limits.timeout_seconds
            try:
                for thread in threads:
                    thread.start()
                while process.poll() is None:
                    if cancel.is_set():
                        status, error = "cancelled", "cancelled"
                        break
                    if exceeded.is_set():
                        status, error = "failed", "output_limit"
                        break
                    if time.monotonic() >= deadline:
                        status, error = "timed_out", "timed_out"
                        break
                    cancel.wait(0.01)
            finally:
                if job:
                    kernel, handle = job
                    kernel.TerminateJobObject(handle, 1)
                    kernel.CloseHandle(handle)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
                    except ProcessLookupError:
                        pass
                process.wait()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join()
            if exceeded.is_set():
                status, error = "failed", "output_limit"
            elif status == "succeeded" and process.returncode:
                status, error = "failed", "native_exit"
            return SandboxOutcome(
                status,
                process.returncode,
                bytes(chunks[0]),
                bytes(chunks[1]),
                truncated=exceeded.is_set(),
                error_code=error,
            )
