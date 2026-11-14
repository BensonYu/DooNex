"""Stateless, resource-limited Docker shell tool. No host execution fallback."""

from __future__ import annotations

import hashlib
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

from .controller_operations import (
    LaunchCapability,
    ProcessPermit,
    RegistryError,
    current_registry,
    scope,
)
from .deadline import check_deadline
from .skills import _check_name, _no_links
from .store import OwnershipLost
from .tasksets import _overlaps, _snapshot

TASK_IMAGE = "python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CONTROL_OUTPUT_BYTES = 64 * 1024
MAX_CONTAINER_IDS = 64
MAX_MOUNT_OUTPUT_BYTES = 16 * 1024
CLEANUP_SECONDS = 15.0
POLL_SECONDS = 0.05
_UNSET = object()


def _evidence() -> dict:
    return {
        "container_name": None,
        "container_absence_confirmed": None,
        "container_snapshot_absence_confirmed": None,
        "processes": [],
        "errors": [],
        "local_process_unknown": False,
        "daemon_request_unknown": False,
        "controller_record_unknown": False,
        "controller_operation_id": None,
    }


def _unknown(evidence: dict) -> bool:
    return (
        evidence["local_process_unknown"]
        or evidence.get("daemon_request_unknown", False)
        or evidence.get("controller_record_unknown", False)
        or (evidence["container_absence_confirmed"] is False)
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

    def __init__(self, *, business_deadline: float | None = None):
        self.deadline: float | None = None
        self.business_deadline = business_deadline

    def start(self) -> float:
        if self.deadline is None:
            started = time.monotonic()
            if self.business_deadline is not None:
                started = min(started, self.business_deadline)
            self.deadline = started + CLEANUP_SECONDS
        return self.deadline


@dataclass
class _ProcessResult:
    returncode: int | None
    stdout: bytes
    reason: str | None = None
    truncated: bool = False
    permit: ProcessPermit | None = None


class _Supervisor:
    def __init__(
        self, evidence: dict, cleanup: _Cleanup, *, capability: LaunchCapability
    ):
        if type(capability) is not LaunchCapability:
            raise RegistryError("capability")
        self.evidence, self.cleanup, self.capability = evidence, cleanup, capability
        self.create_attempted = False
        self.create_acknowledged = False
        self.observation_complete = False
        self.remaining_containers: list[str] | None = None

    def process(
        self,
        arguments: list[str],
        *,
        deadline: float,
        operation: str,
        maximum: int,
        capability: LaunchCapability,
        target: str | None = None,
        truncate: bool = False,
        should_stop: Callable[[], bool] | None = None,
        env: dict | None = None,
    ) -> _ProcessResult:
        if (
            type(capability) is not LaunchCapability
            or capability is not self.capability
        ):
            raise RegistryError("capability")
        phase = operation if operation == "mount_validation" else "docker_" + operation
        if phase != "mount_validation" and (
            len(arguments) < 2
            or arguments[1] != operation
            or _docker_target(tuple(arguments[1:])) != target
        ):
            raise RegistryError("scope")
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
        # Complete the last potentially blocking caller callback before the
        # capability rechecks the actual Run owner and action binding in DB.
        if should_stop is not None and should_stop():
            return _ProcessResult(None, b"", "cancelled")
        permit = capability.before_process(phase, target=target)
        if phase == "docker_create":
            self.create_attempted = True
        try:
            if current_registry() is not capability._operation.registry:
                raise RegistryError("capability")
        except BaseException:
            capability.after_process(
                permit, pid=None, exit_code=None, reaped=False, pipe_eof=False
            )
            self.evidence["local_process_unknown"] = True
            raise
        # before_process may wait for the database. The durable launching gate
        # and checked local permit cannot replace this final deadline check.
        if time.monotonic() >= deadline:
            capability.after_process(
                permit, pid=None, exit_code=None, reaped=False, pipe_eof=False
            )
            self.evidence["local_process_unknown"] = True
            return _ProcessResult(None, b"", "timeout", permit=permit)
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
            capability.after_process(
                permit, pid=None, exit_code=None, reaped=False, pipe_eof=False
            )
            self.evidence["local_process_unknown"] = True
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
            try:
                capability.after_process(
                    permit,
                    pid=process.pid,
                    exit_code=process.returncode if record["reaped"] else None,
                    reaped=record["reaped"],
                    pipe_eof=record["pipe_eof"],
                )
            except BaseException:
                self.evidence["controller_record_unknown"] = True
                raise
        if failure is not None:
            raise SandboxError(
                f"Sandbox {operation} supervision failed: {type(failure).__name__}",
                cleanup_evidence=self.evidence,
            ) from failure
        return _ProcessResult(
            process.returncode, bytes(collected), reason, truncated, permit
        )


def _reserve(
    kind,
    *,
    run_id,
    deadline,
    intent,
    container_name=None,
    project_id=None,
    parent_action_id=None,
    expected_run_owner=_UNSET,
    repair_of=None,
):
    # A label alone must never detach a real Run from its execution authority.
    if expected_run_owner is _UNSET:
        raise RegistryError("run_owner_missing")
    registry = current_registry()
    run_label = "doonex.run_id=" + run_id
    if repair_of is not None:
        if kind != "sandbox_cleanup" or parent_action_id is not None:
            raise RegistryError("repair_scope")
        operation = registry.repair_only(
            repair_of,
            deadline_monotonic_ns=int(deadline * 1_000_000_000),
            expected_run_owner=expected_run_owner,
        )
        record = operation.record
        if (
            record["run_id"] != run_id
            or record["scope"]["run_label"] != run_label
            or project_id is not None
            and record["project_id"] != project_id
        ):
            operation.mark_unknown("interrupted")
            raise RegistryError("repair_scope", operation_id=operation.operation_id)
        return operation
    return registry.reserve(
        kind,
        scope=scope(
            intent_digest=hashlib.sha256(
                json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            container_name=container_name,
            run_label=run_label,
        ),
        deadline_monotonic_ns=int(deadline * 1_000_000_000),
        run_id=run_id,
        project_id=project_id,
        parent_action_id=parent_action_id,
        expected_run_owner=expected_run_owner,
        cleanup_allowance_seconds=math.ceil(CLEANUP_SECONDS),
    )


def _launch(operation, evidence):
    evidence["controller_operation_id"] = operation.operation_id
    try:
        return operation.launch()
    except BaseException as error:
        evidence["controller_record_unknown"] = True
        try:
            operation.mark_unknown("interrupted")
        except BaseException:  # noqa: BLE001 - preserve the launch uncertainty
            evidence["errors"].append("Controller launch state remains unknown")
        error.cleanup_unknown, error.cleanup_evidence = True, evidence
        raise


def _settle(operation, supervisor, *, reason):
    evidence = supervisor.evidence
    known = (
        not _unknown(evidence)
        and supervisor.observation_complete
        and supervisor.remaining_containers is not None
        and (not supervisor.create_attempted or supervisor.create_acknowledged)
    )
    try:
        if not known:
            evidence["controller_record_unknown"] = True
            operation.mark_unknown(
                "daemon_unknown"
                if evidence["daemon_request_unknown"]
                else "supervision_unknown"
            )
            return
        operation.finish(
            {
                "outcome": "success" if reason == "success" else "failed",
                "reason": reason,
                "local_process_unknown": False,
                "daemon_request_unknown": False,
                "docker": {
                    "create_attempted": supervisor.create_attempted,
                    "create_acknowledged": supervisor.create_acknowledged,
                    "container_removed": supervisor.create_acknowledged
                    and supervisor.remaining_containers == [],
                    "observation_complete": True,
                    "run_label": operation.record["scope"]["run_label"],
                    "remaining_containers": len(supervisor.remaining_containers),
                },
            }
        )
    except BaseException as error:
        evidence["controller_record_unknown"] = True
        evidence["errors"].append("Controller terminal receipt remains unknown")
        error.cleanup_unknown, error.cleanup_evidence = True, evidence
        raise


def _docker_target(arguments):
    operation = arguments[0] if arguments else None
    if operation == "create" and arguments.count("--name") == 1:
        index = arguments.index("--name")
        if index + 1 < len(arguments):
            return arguments[index + 1]
    elif operation == "start" and len(arguments) == 3 and arguments[1] == "-a":
        return arguments[2]
    elif (
        operation == "inspect"
        and len(arguments) == 4
        and arguments[1:3]
        == (
            "--format",
            "{{.State.ExitCode}}",
        )
    ):
        return arguments[3]
    elif operation == "rm" and len(arguments) == 3 and arguments[1] == "-f":
        return arguments[2]
    elif (
        operation == "ps"
        and len(arguments) == 5
        and arguments[1:4] == ("-aq", "--no-trunc", "--filter")
        and arguments[4].startswith("label=doonex.run_id=")
    ):
        return arguments[4].removeprefix("label=")
    raise RegistryError("scope")


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
        if _supervisor is None:
            raise RegistryError("capability")
        supervisor = _supervisor
        target = _docker_target(arguments)
        result = supervisor.process(
            [self.docker, *arguments],
            deadline=_deadline if _deadline is not None else time.monotonic() + timeout,
            operation=arguments[0],
            maximum=_maximum,
            should_stop=_should_stop,
            capability=supervisor.capability,
            target=target,
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
        completed = subprocess.CompletedProcess(
            arguments, result.returncode, result.stdout
        )
        completed.controller_permit = result.permit
        return completed

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
        supervisor.capability.observe_containers(
            result.controller_permit,
            identifiers,
            run_label="doonex.run_id=" + run_id,
            complete=True,
        )
        supervisor.observation_complete = True
        supervisor.remaining_containers = identifiers
        return identifiers

    def containers(
        self,
        run_id: str,
        *,
        deadline: float | None = None,
        project_id=None,
        parent_action_id=None,
        expected_run_owner=_UNSET,
    ) -> list[str]:
        check_deadline(deadline)
        run_id = _run_id(run_id)
        deadline = (
            min(time.monotonic() + CLEANUP_SECONDS, deadline)
            if deadline is not None
            else time.monotonic() + CLEANUP_SECONDS
        )
        evidence = _evidence()
        operation = _reserve(
            "sandbox_probe",
            run_id=run_id,
            deadline=deadline,
            intent={"operation": "probe", "run_id": run_id},
            project_id=project_id,
            parent_action_id=parent_action_id,
            expected_run_owner=expected_run_owner,
        )
        supervisor = _Supervisor(
            evidence, _Cleanup(), capability=_launch(operation, evidence)
        )
        supervisor.cleanup.deadline = deadline
        try:
            identifiers = self._containers(run_id, deadline, supervisor)
        except BaseException as error:
            _settle(operation, supervisor, reason="operation_failed")
            error.cleanup_evidence = supervisor.evidence
            error.cleanup_unknown = _unknown(supervisor.evidence)
            raise
        _settle(operation, supervisor, reason="success")
        return identifiers

    def stop_run(
        self,
        run_id: str,
        *,
        project_id=None,
        parent_action_id=None,
        expected_run_owner=_UNSET,
        repair_of=None,
    ) -> dict:
        run_id = _run_id(run_id)
        deadline = time.monotonic() + CLEANUP_SECONDS
        evidence = _evidence()
        operation = _reserve(
            "sandbox_cleanup",
            run_id=run_id,
            deadline=deadline,
            intent={"operation": "cleanup", "run_id": run_id},
            project_id=project_id,
            parent_action_id=parent_action_id,
            expected_run_owner=expected_run_owner,
            repair_of=repair_of,
        )
        supervisor = _Supervisor(
            evidence, _Cleanup(), capability=_launch(operation, evidence)
        )
        supervisor.cleanup.deadline = deadline
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
        except BaseException as error:
            _settle(operation, supervisor, reason="operation_failed")
            error.cleanup_evidence = supervisor.evidence
            error.cleanup_unknown = _unknown(supervisor.evidence)
            raise
        supervisor.evidence["container_absence_confirmed"] = remaining == []
        supervisor.evidence["container_snapshot_absence_confirmed"] = remaining == []
        _settle(
            operation, supervisor, reason="operation_failed" if errors else "success"
        )
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
        project_id=None,
        parent_action_id=None,
        expected_run_owner=_UNSET,
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
        evidence = _evidence()
        name = "doonex-" + uuid.uuid4().hex
        operation = _reserve(
            "sandbox_execute",
            run_id=run_id,
            deadline=deadline,
            intent={
                "operation": "execute",
                "run_id": run_id,
                "workspace": str(workspace),
                "command": command,
                "read_only": read_only,
                "skill_path": str(skill_path) if skill_path is not None else None,
                "verifier_path": str(verifier_path)
                if verifier_path is not None
                else None,
                "image": self.image,
            },
            container_name=name,
            project_id=project_id,
            parent_action_id=parent_action_id,
            expected_run_owner=expected_run_owner,
        )
        supervisor = _Supervisor(
            evidence,
            _Cleanup(business_deadline=deadline),
            capability=_launch(operation, evidence),
        )
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
        attempted_create = False
        acknowledged_create = False
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
                    capability=supervisor.capability,
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
                        acknowledged_create = True
                        supervisor.create_acknowledged = True
                        result["container_id"] = identifier
                        attached = supervisor.process(
                            [self.docker, "start", "-a", name],
                            deadline=deadline,
                            operation="start",
                            maximum=MAX_OUTPUT_BYTES,
                            truncate=True,
                            should_stop=should_stop,
                            capability=supervisor.capability,
                            target=name,
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
                                name,
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
                if not acknowledged_create and supervisor.create_attempted:
                    # Killing/reaping the client does not cancel a daemon's
                    # in-flight create. A current empty ps is only a snapshot;
                    # the named container may materialize after that query.
                    supervisor.evidence["daemon_request_unknown"] = True
                    supervisor.evidence["errors"].append(
                        "Create daemon request completion unconfirmed"
                    )
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
                except (RegistryError, OwnershipLost) as error:
                    supervisor.evidence["controller_record_unknown"] = True
                    if pending is None:
                        pending = error
            if not supervisor.observation_complete or attempted_create:
                try:
                    cleanup_deadline = supervisor.cleanup.start()
                    remaining = self._containers(run_id, cleanup_deadline, supervisor)
                    supervisor.evidence[
                        "container_snapshot_absence_confirmed"
                    ] = not remaining
                    supervisor.evidence["container_absence_confirmed"] = (
                        not remaining
                        and not supervisor.evidence["daemon_request_unknown"]
                    )
                    if remaining:
                        supervisor.evidence["errors"].append(
                            "Container absence unconfirmed"
                        )
                except SandboxError as error:
                    supervisor.evidence["errors"].append(str(error)[:1000])
                except (RegistryError, OwnershipLost) as error:
                    supervisor.evidence["controller_record_unknown"] = True
                    if pending is None:
                        pending = error
            reason = (
                "operation_failed"
                if pending is not None
                else "cancelled"
                if result["cancelled"]
                else "deadline"
                if result["timed_out"]
                else "command_failed"
                if result["exit_code"] != 0
                else "success"
            )
            try:
                _settle(operation, supervisor, reason=reason)
            except BaseException as error:  # noqa: BLE001 - preserve original failure
                if pending is None:
                    pending = error
            result["cleanup_unknown"] = _unknown(supervisor.evidence)
            if result["cleanup_unknown"]:
                result["cleanup_error"] = (
                    "; ".join(supervisor.evidence["errors"])[:1000]
                    or "Sandbox cleanup remains unknown"
                )
            result["ended_at"] = _now()
        if pending is not None:
            if isinstance(pending, (RegistryError, OwnershipLost)):
                pending.cleanup_evidence = supervisor.evidence
                pending.cleanup_unknown = result["cleanup_unknown"]
                raise pending
            if isinstance(pending, SandboxError):
                pending.cleanup_evidence = supervisor.evidence
                pending.cleanup_unknown = result["cleanup_unknown"]
                raise pending
            raise SandboxError(
                f"Sandbox execution failed: {type(pending).__name__}",
                cleanup_evidence=supervisor.evidence,
            ) from pending
        return result
