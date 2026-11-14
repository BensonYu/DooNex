"""Stateless, resource-limited Docker shell tool. No host execution fallback."""

from __future__ import annotations

import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .skills import _check_name, _no_links
from .tasksets import _overlaps, _snapshot

TASK_IMAGE = "python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CONTROL_OUTPUT_BYTES = 64 * 1024
MAX_CONTAINER_IDS = 64
MAX_MOUNT_OUTPUT_BYTES = 16 * 1024
CLEANUP_SECONDS = 15.0
POLL_SECONDS = 0.05


def _evidence() -> dict:
    return {
        "container_name": None,
        "container_absence_confirmed": None,
        "processes": [],
        "errors": [],
        "local_process_unknown": False,
    }


def _unknown(evidence: dict) -> bool:
    return evidence["local_process_unknown"] or (
        evidence["container_absence_confirmed"] is False
    )


class SandboxError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        cleanup_evidence: dict | None = None,
        reason: str | None = None,
    ):
        super().__init__(message)
        self.cleanup_evidence = cleanup_evidence or _evidence()
        self.cleanup_unknown = _unknown(self.cleanup_evidence)
        self.reason = reason


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SandboxError("Invalid run identifier")
    return value


def _mount_path(value: Path, *, extra: bool = False) -> Path:
    try:
        path = Path(value).expanduser().absolute()
        _no_links(path)
        if not path.is_dir():
            raise SandboxError("A mount must be a real directory")
        path = path.resolve(strict=True)
        if any(char in str(path) for char in (",", '"')) or any(
            ord(char) < 32 or ord(char) == 127 for char in str(path)
        ):
            raise SandboxError("Unsupported mount path")
        if extra:
            info, _ = _snapshot(path, "verifier")
            for entry in info["verifier_files"]:
                _check_name(entry["path"])
            for name in info["verifier_directories"]:
                _check_name(name)
        return path
    except (OSError, ValueError, TypeError) as error:
        raise SandboxError("Unsafe sandbox mount: " + str(error)) from error


def _validated_mounts(payload: str) -> None:
    """Trusted helper: keep filesystem scans inside a supervised process."""
    try:
        value = json.loads(payload)
        workspace = _mount_path(Path(value["workspace"]))
        extra = value["extra"]
        if extra is not None:
            extra = _mount_path(Path(extra), extra=True)
            if _overlaps(workspace, extra):
                raise SandboxError("Extra context must not overlap the workspace")
        print(json.dumps([str(workspace), str(extra) if extra is not None else None]))
    except Exception as error:  # noqa: BLE001 - untrusted mount failures stay in helper
        # Both content and diagnostic output are bounded by the supervisor.
        print(f"Unsafe sandbox mount: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None


class _Cleanup:
    """One separate necessary-cleanup window, never refreshed by a child call."""

    def __init__(self):
        self.deadline: float | None = None

    def start(self) -> float:
        if self.deadline is None:
            self.deadline = time.monotonic() + CLEANUP_SECONDS
        return self.deadline


@dataclass
class _ProcessResult:
    returncode: int | None
    stdout: bytes
    reason: str | None = None
    truncated: bool = False


class _Supervisor:
    def __init__(self, evidence: dict, cleanup: _Cleanup):
        self.evidence, self.cleanup = evidence, cleanup

    def process(
        self,
        arguments: list[str],
        *,
        deadline: float,
        operation: str,
        maximum: int,
        truncate: bool = False,
        should_stop: Callable[[], bool] | None = None,
        env: dict | None = None,
    ) -> _ProcessResult:
        if should_stop is not None and should_stop():
            return _ProcessResult(None, b"", "cancelled")
        if not math.isfinite(deadline):
            raise SandboxError("A finite sandbox process deadline is required")
        if self.cleanup.deadline is not None:
            # Reserve part of the *same* cleanup window to observe SIGKILL,
            # waitpid and EOF. A stalled CLI must not consume that entire window
            # before the supervisor even attempts to reap it.
            remaining = max(0, self.cleanup.deadline - time.monotonic())
            deadline = min(deadline, self.cleanup.deadline - min(1.0, remaining / 4))
        if time.monotonic() >= deadline:
            return _ProcessResult(None, b"", "timeout")
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=True,
                env=env,
            )
        except OSError as error:
            raise SandboxError(
                f"Sandbox {operation} could not start: {type(error).__name__}",
                cleanup_evidence=self.evidence,
            ) from error
        assert process.stdout is not None
        record = {
            "operation": operation,
            "pid": process.pid,
            "reaped": False,
            "pipe_eof": False,
        }
        self.evidence["processes"].append(record)
        collected = bytearray()
        reason = None
        truncated = False
        signalled = False
        failure = None
        try:
            descriptor = None
            try:
                descriptor = process.stdout.fileno()
                os.set_blocking(descriptor, False)
            except BaseException as error:  # noqa: BLE001 - reap after setup interruption
                failure, reason = error, "supervision_failed"
            while True:
                try:
                    current = time.monotonic()
                    if reason is None:
                        if current >= deadline:
                            reason = "timeout"
                        elif should_stop is not None and should_stop():
                            reason = "cancelled"
                    if reason is not None:
                        cutoff = self.cleanup.start()
                        if not signalled:
                            # Include ordinary descendants which inherited the pipe.
                            # Escaped descendants remain unknown unless EOF is observed.
                            signalled = True
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            except OSError as error:
                                record["termination_error"] = type(error).__name__
                    else:
                        cutoff = deadline
                    # Do not reap an exited leader while an inherited pipe is
                    # still open: its zombie keeps this PID/PGID from reuse
                    # until our first and only group signal. After EOF there
                    # is no reason to signal a reaped leader's numeric group.
                    if record["pipe_eof"] or signalled:
                        record["reaped"] = process.poll() is not None
                    if record["reaped"] and record["pipe_eof"]:
                        break
                    remaining = cutoff - time.monotonic()
                    if remaining <= 0:
                        if reason is None:
                            reason = "timeout"
                            continue
                        break
                    if record["pipe_eof"]:
                        time.sleep(min(POLL_SECONDS, remaining))
                        continue
                    ready, _, _ = select.select(
                        [descriptor], [], [], min(POLL_SECONDS, remaining)
                    )
                    if ready:
                        try:
                            chunk = os.read(descriptor, 8192)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            record["pipe_eof"] = True
                            continue
                        available = maximum - len(collected)
                        collected.extend(chunk[: max(0, available)])
                        if len(chunk) > available:
                            truncated = True
                            if not truncate:
                                reason = "output_limit"
                except BaseException as error:  # noqa: BLE001 - cleanup after signals/callback
                    if failure is None:
                        failure = error
                    reason = "supervision_failed"
                    remaining = self.cleanup.start() - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(POLL_SECONDS, remaining))
            record["reaped"] = process.poll() is not None
        finally:
            # A single owner uses unbuffered, nonblocking reads: close cannot wait
            # behind a daemon reader, and process exit never substitutes for EOF.
            try:
                process.stdout.close()
            except OSError as error:
                self.evidence["local_process_unknown"] = True
                self.evidence["errors"].append(
                    f"{operation}: pipe close failed: {type(error).__name__}"
                )
            finally:
                if not (record["reaped"] and record["pipe_eof"]):
                    self.evidence["local_process_unknown"] = True
                    self.evidence["errors"].append(
                        f"{operation}: process reap or pipe EOF unconfirmed"
                    )
            record.update(
                returncode=process.returncode, reason=reason, truncated=truncated
            )
        if failure is not None:
            raise SandboxError(
                f"Sandbox {operation} supervision failed: {type(failure).__name__}",
                cleanup_evidence=self.evidence,
            ) from failure
        return _ProcessResult(process.returncode, bytes(collected), reason, truncated)


class Sandbox:
    def __init__(self, image: str = TASK_IMAGE, docker: str = "docker"):
        if image != TASK_IMAGE:
            raise SandboxError("M1 requires the pinned, reviewed task image")
        self.image = image
        self.docker = docker

    def _docker(
        self,
        *arguments: str,
        timeout: float = 15,
        check: bool = True,
        _deadline: float | None = None,
        _supervisor: _Supervisor | None = None,
        _maximum: int = MAX_CONTROL_OUTPUT_BYTES,
        _should_stop: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess:
        supervisor = _supervisor or _Supervisor(_evidence(), _Cleanup())
        result = supervisor.process(
            [self.docker, *arguments],
            deadline=_deadline if _deadline is not None else time.monotonic() + timeout,
            operation=arguments[0],
            maximum=_maximum,
            should_stop=_should_stop,
        )
        if result.reason is not None:
            raise SandboxError(
                f"Docker {arguments[0]} operation failed: {result.reason}",
                cleanup_evidence=supervisor.evidence,
                reason=result.reason,
            )
        if check and result.returncode:
            raise SandboxError(
                "Docker operation failed: "
                + result.stdout[:1000].decode(errors="replace"),
                cleanup_evidence=supervisor.evidence,
            )
        return subprocess.CompletedProcess(arguments, result.returncode, result.stdout)

    def _containers(
        self, run_id: str, deadline: float, supervisor: _Supervisor
    ) -> list[str]:
        arguments = [
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=doonex.run_id=" + run_id,
        ]
        result = self._docker(
            *arguments,
            _deadline=deadline,
            _supervisor=supervisor,
            _maximum=MAX_CONTAINER_IDS * 65,
        )
        try:
            identifiers = result.stdout.decode("ascii").splitlines()
        except UnicodeDecodeError as error:
            raise SandboxError(
                "Docker container list is not ASCII",
                cleanup_evidence=supervisor.evidence,
            ) from error
        if (
            len(identifiers) > MAX_CONTAINER_IDS
            or len(set(identifiers)) != len(identifiers)
            or any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in identifiers)
        ):
            raise SandboxError(
                "Docker container list is incomplete or malformed",
                cleanup_evidence=supervisor.evidence,
            )
        return identifiers

    def containers(self, run_id: str) -> list[str]:
        deadline = time.monotonic() + CLEANUP_SECONDS
        supervisor = _Supervisor(_evidence(), _Cleanup())
        supervisor.cleanup.deadline = deadline
        return self._containers(_run_id(run_id), deadline, supervisor)

    def stop_run(self, run_id: str) -> dict:
        run_id = _run_id(run_id)
        supervisor = _Supervisor(_evidence(), _Cleanup())
        deadline = supervisor.cleanup.start()
        stopped, errors, remaining = [], [], ["unknown"]
        try:
            identifiers = self._containers(run_id, deadline, supervisor)
            for identifier in identifiers:
                try:
                    result = self._docker(
                        "rm",
                        "-f",
                        identifier,
                        check=False,
                        _deadline=deadline,
                        _supervisor=supervisor,
                    )
                    if result.returncode:
                        errors.append(result.stdout[:1000].decode(errors="replace"))
                    else:
                        stopped.append(identifier)
                except SandboxError as error:
                    errors.append(str(error)[:1000])
            remaining = self._containers(run_id, deadline, supervisor)
        except SandboxError as error:
            errors.append(str(error)[:1000])
        supervisor.evidence["container_absence_confirmed"] = remaining == []
        errors.extend(supervisor.evidence["errors"])
        return {
            "remaining": remaining,
            "stopped": stopped,
            "errors": errors,
            "cleanup_unknown": _unknown(supervisor.evidence),
            "cleanup_evidence": supervisor.evidence,
        }

    def run(
        self,
        workspace: Path,
        command: str,
        timeout: float,
        run_id: str,
        should_stop: Callable[[], bool] = lambda: False,
        *,
        read_only: bool = False,
        skill_path: Path | None = None,
        verifier_path: Path | None = None,
        deadline: float | None = None,
    ) -> dict:
        entered = time.monotonic()
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 3600
        ):
            raise SandboxError("Tool timeout must be between zero and 3600 seconds")
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (float, int))
            or not math.isfinite(deadline)
        ):
            raise SandboxError("A finite monotonic sandbox deadline is required")
        deadline = (
            min(entered + timeout, deadline)
            if deadline is not None
            else entered + timeout
        )
        run_id = _run_id(run_id)
        if not isinstance(command, str) or not command or len(command) > 100_000:
            raise SandboxError("A bounded shell command is required")
        if not isinstance(read_only, bool):
            raise SandboxError("Workspace access mode must be explicit")
        if skill_path is not None and verifier_path is not None:
            raise SandboxError("Skill and verifier mounts are mutually exclusive")
        supervisor = _Supervisor(_evidence(), _Cleanup())
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
            "cleanup_unknown": False,
            "cleanup_evidence": supervisor.evidence,
        }
        name = "doonex-" + uuid.uuid4().hex
        attempted_create = False
        pending = None
        try:
            if should_stop():
                result["cancelled"] = True
            else:
                extra = skill_path if skill_path is not None else verifier_path
                mounts = json.dumps(
                    {
                        "workspace": str(workspace),
                        "extra": str(extra) if extra is not None else None,
                    }
                )
                if len(mounts) > MAX_MOUNT_OUTPUT_BYTES:
                    raise SandboxError("Sandbox mount paths exceed the size limit")
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(Path(__file__).absolute().parents[1])
                validation = supervisor.process(
                    [
                        sys.executable,
                        "-c",
                        "from doonex.sandbox import _validated_mounts; import sys; _validated_mounts(sys.argv[1])",
                        mounts,
                    ],
                    deadline=deadline,
                    operation="mount_validation",
                    maximum=MAX_MOUNT_OUTPUT_BYTES,
                    should_stop=should_stop,
                    env=environment,
                )
                if validation.reason in {"timeout", "cancelled"}:
                    result["timed_out"] = validation.reason == "timeout"
                    result["cancelled"] = validation.reason == "cancelled"
                elif validation.reason or validation.returncode:
                    raise SandboxError(
                        "Mount validation failed: "
                        + validation.stdout[:1000].decode(errors="replace")
                    )
                else:
                    workspace, extra_path = json.loads(validation.stdout)
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
                    if extra_path is not None:
                        destination = (
                            "/skills" if skill_path is not None else "/verifier"
                        )
                        arguments.extend(
                            [
                                "--mount",
                                f"type=bind,src={extra_path},dst={destination},readonly",
                            ]
                        )
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
                    arguments.extend(
                        [self.image, "/bin/sh", "-c", "umask 077\n" + command]
                    )
                    if should_stop():
                        result["cancelled"] = True
                    elif time.monotonic() >= deadline:
                        result["timed_out"] = True
                    else:
                        attempted_create = True
                        supervisor.evidence.update(
                            container_name=name, container_absence_confirmed=False
                        )
                        created = self._docker(
                            *arguments,
                            _deadline=deadline,
                            _supervisor=supervisor,
                            _maximum=65,
                            _should_stop=should_stop,
                        )
                        identifier = created.stdout.decode("ascii").removesuffix("\n")
                        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                            raise SandboxError(
                                "Docker create returned a malformed container identifier"
                            )
                        result["container_id"] = identifier
                        attached = supervisor.process(
                            [self.docker, "start", "-a", identifier],
                            deadline=deadline,
                            operation="start",
                            maximum=MAX_OUTPUT_BYTES,
                            truncate=True,
                            should_stop=should_stop,
                        )
                        result["output"] = attached.stdout.decode(errors="replace")
                        result["output_truncated"] = attached.truncated
                        result["timed_out"] = (
                            attached.reason == "timeout" or time.monotonic() >= deadline
                        )
                        result["cancelled"] = attached.reason == "cancelled"
                        if not (result["timed_out"] or result["cancelled"]):
                            state = self._docker(
                                "inspect",
                                "--format",
                                "{{.State.ExitCode}}",
                                identifier,
                                _deadline=deadline,
                                _supervisor=supervisor,
                                _should_stop=should_stop,
                            )
                            code = state.stdout.decode("ascii").removesuffix("\n")
                            if not re.fullmatch(r"[0-9]{1,3}", code) or int(code) > 255:
                                raise SandboxError(
                                    "Docker inspect returned a malformed exit code"
                                )
                            result["exit_code"] = int(code)
        except BaseException as error:  # noqa: BLE001 - clean after shutdown/callback
            if isinstance(error, SandboxError) and error.reason in {
                "timeout",
                "cancelled",
            }:
                result["timed_out"] = error.reason == "timeout"
                result["cancelled"] = error.reason == "cancelled"
            else:
                pending = error
        finally:
            if not result["cancelled"] and time.monotonic() >= deadline:
                result["timed_out"] = True
            if attempted_create:
                cleanup_deadline = supervisor.cleanup.start()
                try:
                    # The chosen name is known even if create's ACK was lost.
                    self._docker(
                        "rm",
                        "-f",
                        name,
                        check=False,
                        _deadline=cleanup_deadline,
                        _supervisor=supervisor,
                    )
                except SandboxError as error:
                    supervisor.evidence["errors"].append(str(error)[:1000])
                try:
                    remaining = self._containers(run_id, cleanup_deadline, supervisor)
                    supervisor.evidence["container_absence_confirmed"] = not remaining
                    if remaining:
                        supervisor.evidence["errors"].append(
                            "Container absence unconfirmed"
                        )
                except SandboxError as error:
                    supervisor.evidence["errors"].append(str(error)[:1000])
            result["cleanup_unknown"] = _unknown(supervisor.evidence)
            if result["cleanup_unknown"]:
                result["cleanup_error"] = (
                    "; ".join(supervisor.evidence["errors"])[:1000]
                    or "Sandbox cleanup remains unknown"
                )
            result["ended_at"] = _now()
        if pending is not None:
            if isinstance(pending, SandboxError):
                pending.cleanup_evidence = supervisor.evidence
                pending.cleanup_unknown = result["cleanup_unknown"]
                raise pending
            raise SandboxError(
                f"Sandbox execution failed: {type(pending).__name__}",
                cleanup_evidence=supervisor.evidence,
            ) from pending
        return result
