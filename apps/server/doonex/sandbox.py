"""Stateless, resource-limited Docker shell tool. No host execution fallback."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

TASK_IMAGE = "python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class SandboxError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SandboxError("Invalid run identifier")
    return value


class Sandbox:
    def __init__(self, image: str = TASK_IMAGE, docker: str = "docker"):
        if image != TASK_IMAGE:
            raise SandboxError("M1 requires the pinned, reviewed task image")
        self.image = image
        self.docker = docker

    def _docker(
        self, *arguments: str, timeout: float = 15, check: bool = True
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                [self.docker, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxError(
                f"Docker operation failed: {type(error).__name__}"
            ) from error
        if check and result.returncode:
            raise SandboxError(
                "Docker operation failed: "
                + result.stdout.decode(errors="replace")[:1000]
            )
        return result

    def containers(self, run_id: str) -> list[str]:
        result = self._docker(
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=doonex.run_id=" + _run_id(run_id),
        )
        return result.stdout.decode().split()

    def stop_run(self, run_id: str) -> dict:
        stopped, errors = [], []
        for identifier in self.containers(run_id):
            result = self._docker("rm", "-f", identifier, check=False)
            if result.returncode:
                errors.append(result.stdout.decode(errors="replace")[:1000])
            else:
                stopped.append(identifier)
        remaining = self.containers(run_id)
        return {"remaining": remaining, "stopped": stopped, "errors": errors}

    def run(
        self,
        workspace: Path,
        command: str,
        timeout: float,
        run_id: str,
        should_stop: Callable[[], bool] = lambda: False,
        *,
        read_only: bool = False,
    ) -> dict:
        run_id = _run_id(run_id)
        workspace = Path(workspace)
        if workspace.is_symlink() or not workspace.is_dir():
            raise SandboxError("Workspace must be a real directory")
        workspace = workspace.resolve(strict=True)
        if "," in str(workspace) or "\n" in str(workspace):
            raise SandboxError("Unsupported workspace path")
        if not isinstance(command, str) or not command or len(command) > 100_000:
            raise SandboxError("A bounded shell command is required")
        if not isinstance(timeout, (float, int)) or not 0 < timeout <= 3600:
            raise SandboxError("Tool timeout must be between zero and 3600 seconds")
        if not isinstance(read_only, bool):
            raise SandboxError("Workspace access mode must be explicit")
        result = {
            "exit_code": None,
            "output": "",
            "started_at": _now(),
            "ended_at": None,
            "timed_out": False,
            "cancelled": False,
            "container_id": None,
            "image": self.image,
            "output_truncated": False,
            "cleanup_error": None,
        }
        if should_stop():
            result.update(cancelled=True, ended_at=_now())
            return result
        name = "doonex-" + uuid.uuid4().hex
        arguments = [
            "create",
            "--name",
            name,
            "--label",
            "doonex.run_id=" + run_id,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--pids-limit",
            "64",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--init",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=67108864",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace"
            + (",readonly" if read_only else ""),
            "--workdir",
            "/workspace",
        ]
        for variable in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ]:
            arguments.extend(["--env", variable + "="])
        arguments.extend([self.image, "/bin/sh", "-c", "umask 077\n" + command])
        identifier = self._docker(*arguments, timeout=30).stdout.decode().strip()
        result["container_id"] = identifier
        process = None
        collected = bytearray()
        drain = None
        try:
            if should_stop():
                result["cancelled"] = True
            else:
                process = subprocess.Popen(
                    [self.docker, "start", "-a", identifier],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                def consume() -> None:
                    assert process is not None and process.stdout is not None
                    while chunk := process.stdout.read(8192):
                        remaining = MAX_OUTPUT_BYTES - len(collected)
                        if len(chunk) > remaining:
                            result["output_truncated"] = True
                        collected.extend(chunk[: max(remaining, 0)])

                drain = threading.Thread(target=consume, daemon=True)
                drain.start()
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if should_stop():
                        result["cancelled"] = True
                        break
                    if time.monotonic() >= deadline:
                        result["timed_out"] = True
                        break
                    time.sleep(0.1)
                if result["timed_out"] or result["cancelled"]:
                    self._docker("kill", identifier, check=False)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                state = self._docker(
                    "inspect",
                    "--format",
                    "{{.State.ExitCode}}",
                    identifier,
                    check=False,
                )
                if state.returncode == 0:
                    result["exit_code"] = int(state.stdout.strip())
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if drain is not None:
                drain.join(timeout=5)
            if process is not None and process.stdout is not None:
                process.stdout.close()
            cleanup = self._docker("rm", "-f", identifier, check=False)
            if cleanup.returncode:
                result["cleanup_error"] = cleanup.stdout.decode(errors="replace")[:1000]
            result["output"] = bytes(collected).decode(errors="replace")
            result["ended_at"] = _now()
        return result
