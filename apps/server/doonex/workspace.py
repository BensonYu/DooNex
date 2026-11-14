"""Bounded Git-object exports and symlink-safe private workspace snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .controller_operations import RegistryError, current_registry, scope
from .deadline import ExecutionDeadlineExceeded
from .deadline import check_deadline as _check_deadline
from .store import OwnershipLost

MAX_FILES = 10_000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_GIT_CONFIG_BYTES = 64 * 1024
MAX_GIT_FILTERS = 256
MAX_GIT_FILTER_NAME_BYTES = 16 * 1024
GIT_CONFIG_TIMEOUT = 5
GIT_OPERATION_TIMEOUT = 30
GIT_CLEANUP_TIMEOUT = 5
MAX_GIT_OUTPUT_BYTES = 200 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 64 * 1024
MAX_GIT_TREE_ENTRIES = 20_000
MAX_GIT_TREE_BYTES = 16 * 1024 * 1024
MAX_GIT_PATH_BYTES = 4096
MAX_GIT_TREE_RECORD_BYTES = MAX_GIT_PATH_BYTES + 128
IO_CHUNK_BYTES = 64 * 1024
GENERATED_NAMES = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".DS_Store",
    }
)


def _generated_path(name: str) -> bool:
    return any(part in GENERATED_NAMES for part in name.split("/")) or name.endswith(
        (".pyc", ".pyo")
    )


_SECRET_NAMES = {
    ".git",
    ".ssh",
    ".aws",
    ".gnupg",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "credentials",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    ".npmrc",
    ".pypirc",
    ".netrc",
}


class WorkspaceError(ValueError):
    """The project or its file content cannot safely enter a task."""


class _GitOutputLimitExceeded(WorkspaceError):
    pass


class _GitLocalDeadlineExceeded(WorkspaceError):
    pass


class WorkspaceProcessUnknown(WorkspaceError):
    """A Git child or its pipes could not be confirmed terminated."""

    cleanup_unknown = True

    def __init__(self, process: subprocess.Popen, *, pipe_eof: bool = False):
        super().__init__("Git termination could not be confirmed")
        self.cleanup_evidence = {
            "container_name": None,
            "container_absence_confirmed": None,
            "processes": [
                {
                    "operation": "git",
                    "pid": process.pid,
                    "reaped": process.returncode is not None,
                    # Closing a descriptor in finally is not an observed EOF.
                    "pipe_eof": pipe_eof,
                }
            ],
            "errors": ["git_termination_unconfirmed"],
            "local_process_unknown": True,
        }


class _GitControllerGroup:
    """Live supervisor receipts for one already-durable controller group."""

    def __init__(self, registry, operation, capability):
        self.registry, self.operation, self.capability = registry, operation, capability
        self.attempts = 0
        self.receipts = []

    def before_process(self):
        self.check_live()
        permit = self.capability.before_process("git")
        self.attempts += 1
        return permit

    def check_live(self):
        if current_registry() is not self.registry:
            raise RegistryError(
                "registry_changed", operation_id=self.operation.operation_id
            )

    def after_process(self, permit, process, *, pipe_eof):
        receipt = {
            "pid": process.pid,
            "exit_code": process.returncode,
            "reaped": process.returncode is not None,
            "pipe_eof": pipe_eof,
        }
        self.capability.after_process(permit, **receipt)
        self.receipts.append(receipt)

    @property
    def terminal(self):
        return (
            bool(self.attempts)
            and len(self.receipts) == self.attempts
            and all(item["reaped"] and item["pipe_eof"] for item in self.receipts)
        )

    def finish(self, error=None):
        if self.terminal and not isinstance(error, (RegistryError, OwnershipLost)):
            reason = (
                "success"
                if error is None
                else "deadline"
                if isinstance(
                    error, (ExecutionDeadlineExceeded, _GitLocalDeadlineExceeded)
                )
                else "command_failed"
                if any(item["exit_code"] != 0 for item in self.receipts)
                else "operation_failed"
            )
            self.operation.finish(
                {
                    "outcome": "success" if error is None else "failed",
                    "reason": reason,
                    "local_process_unknown": False,
                    "daemon_request_unknown": False,
                    "docker": None,
                }
            )
        else:
            self.operation.mark_unknown(
                "supervision_unknown" if not self.terminal else "interrupted"
            )


def _git_intent(repository, kind, intent):
    encoded = os.fsencode(repository)
    if len(encoded) > MAX_GIT_PATH_BYTES:
        raise WorkspaceError("Git project path limit exceeded")
    source = hashlib.sha256(b"doonex-git-source-v1\0" + encoded).hexdigest()
    if (
        type(intent) is not tuple
        or len(intent) > 64
        or any(type(item) is not str for item in intent)
    ):
        raise WorkspaceError("Git operation intent limit exceeded")
    parts, total = [], 0
    for item in intent:
        if len(item) > MAX_GIT_CONFIG_BYTES:
            raise WorkspaceError("Git operation intent limit exceeded")
        part = item.encode("utf-8", errors="surrogatepass")
        total += len(part)
        if total > MAX_GIT_CONFIG_BYTES:
            raise WorkspaceError("Git operation intent limit exceeded")
        parts.append(part)
    digest = hashlib.sha256(
        b"doonex-git-intent-v1\0" + kind.encode("ascii") + source.encode("ascii")
    )
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return scope(intent_digest=digest.hexdigest(), repository_digest=source)


def _controller_context(operation_context):
    context = {} if operation_context is None else operation_context
    if type(context) is not dict or set(context) - {
        "run_id",
        "project_id",
        "parent_action_id",
        "expected_run_owner",
    }:
        raise RegistryError("operation_context")
    return dict(context)


@contextmanager
def _controller_group(repository, kind, deadline, *, intent=(), operation_context=None):
    registry = current_registry()
    context = _controller_context(operation_context)
    operation = registry.reserve(
        kind,
        scope=_git_intent(repository, kind, intent),
        deadline_monotonic_ns=int(deadline * 1_000_000_000),
        cleanup_allowance_seconds=GIT_CLEANUP_TIMEOUT,
        **context,
    )
    capability = operation.launch()
    group = _GitControllerGroup(registry, operation, capability)
    try:
        yield group
    except BaseException as error:
        try:
            group.finish(error)
        except BaseException as registry_error:
            raise registry_error from error
        raise
    else:
        group.finish()


def _check_git_deadline(deadline: float, outer_deadline: float | None) -> None:
    _check_deadline(outer_deadline)
    if time.monotonic() >= deadline:
        raise _GitLocalDeadlineExceeded("Project operation deadline exceeded")


def _allowed_path(name: str) -> None:
    parts = name.split("/")
    if not name or name.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise WorkspaceError(f"Unsafe project path: {name!r}")
    for part in parts:
        lower = part.lower()
        if any(ord(char) < 32 or ord(char) == 127 for char in part):
            raise WorkspaceError("Control character in project path")
        if (
            lower in _SECRET_NAMES
            or lower.startswith((".env", "service-account"))
            or lower.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            raise WorkspaceError(f"Sensitive or management path is excluded: {name}")


def _terminate_git(process: subprocess.Popen, *, observed_eof=None) -> None:
    """Reap the child and observe both pipe EOFs, without a reader thread."""
    deadline = time.monotonic() + GIT_CLEANUP_TIMEOUT
    # Never signal a numeric process group after its leader has been reaped.
    # Darwin can report EPERM for an exiting/zombie child; bounded wait and EOF
    # inspection below can still confirm a terminal child without another signal.
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    with selectors.DefaultSelector() as selector:
        for source in (process.stdout, process.stderr):
            if source is not None and not source.closed:
                os.set_blocking(source.fileno(), False)
                selector.register(source, selectors.EVENT_READ)
        while selector.get_map():
            process.poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkspaceError("Git cleanup incomplete: pipe EOF unconfirmed")
            for key, _ in selector.select(min(remaining, 0.05)):
                try:
                    chunk = os.read(key.fd, IO_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    if observed_eof is not None:
                        observed_eof.add(key.fileobj)
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        try:
            process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired as error:
            raise WorkspaceError("Git cleanup incomplete: child not reaped") from error


def _run_git_command(
    command: list[str],
    environment: dict[str, str],
    *,
    deadline: float,
    stdout_limit: int,
    outer_deadline: float | None = None,
    input_bytes: bytes | None = None,
    consume: Callable[[bytes], None] | None = None,
    controller: _GitControllerGroup | None = None,
) -> subprocess.CompletedProcess:
    """Read finite Git pipes under one deadline; callbacks consume bounded chunks."""
    if controller is None:
        raise RegistryError("capability_missing")
    _check_git_deadline(deadline, outer_deadline)
    permit = controller.before_process()
    controller.check_live()
    _check_git_deadline(deadline, outer_deadline)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
        bufsize=0,
    )
    stdout = bytearray()
    stderr = bytearray()
    stdout_count = 0
    written = 0
    observed_eof = set()
    recorded = False
    try:
        with selectors.DefaultSelector() as selector:
            for source in (process.stdout, process.stderr):
                assert source is not None
                os.set_blocking(source.fileno(), False)
                selector.register(source, selectors.EVENT_READ)
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            while selector.get_map():
                _check_git_deadline(deadline, outer_deadline)
                events = selector.select(max(deadline - time.monotonic(), 0))
                for key, _ in events:
                    _check_git_deadline(deadline, outer_deadline)
                    if key.fileobj is process.stdin:
                        assert input_bytes is not None
                        try:
                            written += os.write(
                                key.fd, input_bytes[written : written + IO_CHUNK_BYTES]
                            )
                        except BrokenPipeError:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        except BlockingIOError:
                            continue
                        if written == len(input_bytes):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    is_stdout = key.fileobj is process.stdout
                    limit = stdout_limit if is_stdout else MAX_GIT_STDERR_BYTES
                    count = stdout_count if is_stdout else len(stderr)
                    try:
                        chunk = os.read(key.fd, min(IO_CHUNK_BYTES, limit + 1 - count))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        observed_eof.add(key.fileobj)
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    if count + len(chunk) > limit:
                        if is_stdout:
                            raise _GitOutputLimitExceeded("Git stdout limit exceeded")
                        raise WorkspaceError("Git stderr limit exceeded")
                    if is_stdout:
                        stdout_count += len(chunk)
                        if consume is None:
                            stdout.extend(chunk)
                        else:
                            consume(chunk)
                    else:
                        stderr.extend(chunk)
            _check_git_deadline(deadline, outer_deadline)
            process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        _check_git_deadline(deadline, outer_deadline)
        controller.after_process(
            permit,
            process,
            pipe_eof=all(
                source in observed_eof for source in (process.stdout, process.stderr)
            ),
        )
        recorded = True
        return subprocess.CompletedProcess(
            command, process.returncode, bytes(stdout), bytes(stderr)
        )
    except BaseException as error:
        try:
            _terminate_git(process, observed_eof=observed_eof)
        except (OSError, WorkspaceError) as cleanup_error:
            eof = all(
                source in observed_eof for source in (process.stdout, process.stderr)
            )
            if not recorded:
                controller.after_process(permit, process, pipe_eof=eof)
            raise WorkspaceProcessUnknown(process, pipe_eof=eof) from cleanup_error
        if not recorded:
            controller.after_process(
                permit,
                process,
                pipe_eof=all(
                    source in observed_eof
                    for source in (process.stdout, process.stderr)
                ),
            )
        if isinstance(error, subprocess.TimeoutExpired):
            _check_deadline(outer_deadline)
            raise _GitLocalDeadlineExceeded(
                "Project operation deadline exceeded"
            ) from error
        raise
    finally:
        for source in (process.stdin, process.stdout, process.stderr):
            if source is not None and not source.closed:
                source.close()


def _git_config_names(
    command: list[str],
    environment: dict[str, str],
    *,
    deadline: float,
    outer_deadline: float | None = None,
    controller: _GitControllerGroup | None = None,
) -> bytes:
    """Inspect effective keys, including includes, without printing their values."""
    try:
        result = _run_git_command(
            [*command, "config", "--includes", "--null", "--name-only", "--list"],
            environment,
            deadline=min(deadline, time.monotonic() + GIT_CONFIG_TIMEOUT),
            stdout_limit=MAX_GIT_CONFIG_BYTES,
            outer_deadline=outer_deadline,
            controller=controller,
        )
    except _GitOutputLimitExceeded as error:
        raise WorkspaceError("Git configuration key limit exceeded") from error
    except _GitLocalDeadlineExceeded as error:
        raise WorkspaceError("Git configuration inspection timed out") from error
    if result.returncode:
        raise WorkspaceError("Git configuration cannot be inspected safely")
    return result.stdout


def _git_context(
    repository: Path,
    deadline: float,
    outer_deadline: float | None,
    controller: _GitControllerGroup | None = None,
) -> tuple[list[str], dict[str, str]]:
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("GIT_"):
            environment.pop(key)
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_ATTR_NOSYSTEM="1",
    )
    command = [
        "git",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-C",
        str(repository),
    ]
    names = _git_config_names(
        command,
        environment,
        deadline=deadline,
        outer_deadline=outer_deadline,
        controller=controller,
    ).split(b"\0")
    drivers = set()
    for name in names:
        section, separator, _ = name[7:].rpartition(b".")
        if name.startswith(b"filter.") and separator:
            drivers.add(section)
    if (
        len(drivers) > MAX_GIT_FILTERS
        or sum(map(len, drivers)) > MAX_GIT_FILTER_NAME_BYTES
    ):
        raise WorkspaceError("Git content filter configuration limit exceeded")
    # Environment key/value pairs preserve valid subsection names containing '='.
    # Empty filters pass bytes through; required=false prevents inherited failures.
    overrides = [
        (f"filter.{os.fsdecode(driver)}.{setting}", value)
        for driver in sorted(drivers)
        for setting, value in (
            ("clean", ""),
            ("smudge", ""),
            ("process", ""),
            ("required", "false"),
        )
    ]
    environment["GIT_CONFIG_COUNT"] = str(len(overrides))
    for index, (key, value) in enumerate(overrides):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return command, environment


class _GitOperation:
    """One export uses one inspected configuration and one absolute deadline."""

    def __init__(
        self,
        repository: Path,
        deadline: float,
        *,
        outer_deadline: float | None = None,
        controller: _GitControllerGroup | None = None,
    ):
        self.deadline = deadline
        self.outer_deadline = outer_deadline
        if controller is None:
            raise RegistryError("capability_missing")
        self.controller = controller
        self.command, self.environment = _git_context(
            repository,
            deadline,
            outer_deadline,
            controller,
        )

    def check_deadline(self) -> None:
        _check_git_deadline(self.deadline, self.outer_deadline)

    def run(
        self,
        *arguments: str,
        check: bool = True,
        stdout_limit: int = MAX_GIT_OUTPUT_BYTES,
        input_bytes: bytes | None = None,
        consume: Callable[[bytes], None] | None = None,
    ) -> subprocess.CompletedProcess:
        if arguments and arguments[0] in (
            "diff",
            "diff-files",
            "diff-index",
            "diff-tree",
            "show",
            "log",
        ):
            arguments = (arguments[0], "--no-ext-diff", "--no-textconv", *arguments[1:])
        result = _run_git_command(
            [*self.command, *arguments],
            self.environment,
            deadline=self.deadline,
            stdout_limit=stdout_limit,
            outer_deadline=self.outer_deadline,
            input_bytes=input_bytes,
            consume=consume,
            controller=self.controller,
        )
        if check and result.returncode:
            raise WorkspaceError(
                "Git project operation failed: "
                + result.stderr.decode(errors="replace")[:1000]
            )
        return result


def _git(
    repository: Path,
    *arguments: str,
    check: bool = True,
    deadline: float | None = None,
    stdout_limit: int = MAX_GIT_OUTPUT_BYTES,
    operation_context: dict | None = None,
) -> subprocess.CompletedProcess:
    _check_deadline(deadline)
    local_deadline = time.monotonic() + GIT_OPERATION_TIMEOUT
    effective_deadline = (
        local_deadline if deadline is None else min(deadline, local_deadline)
    )
    repository = _repository_path(repository)
    with _controller_group(
        repository,
        "git_api_read",
        effective_deadline,
        intent=arguments,
        operation_context=operation_context,
    ) as controller:
        return _GitOperation(
            repository,
            effective_deadline,
            outer_deadline=deadline,
            controller=controller,
        ).run(*arguments, check=check, stdout_limit=stdout_limit)


def _repository_path(repository: Path) -> Path:
    repository = Path(repository).expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise WorkspaceError("Project path is not a directory")
    return repository


def _validate_root(repository: Path, operation: _GitOperation) -> Path:
    result = operation.run(
        "rev-parse", "--show-toplevel", stdout_limit=MAX_GIT_PATH_BYTES + 1
    )
    root = Path(os.fsdecode(result.stdout).strip()).resolve(strict=True)
    if root != repository:
        raise WorkspaceError("Register the Git project root, not a nested directory")
    if (
        operation.run(
            "rev-parse", "--is-bare-repository", stdout_limit=16
        ).stdout.strip()
        != b"false"
    ):
        raise WorkspaceError("Bare repositories cannot be registered")
    return root


def validate_repository(
    repository: Path,
    *,
    deadline: float | None = None,
    operation_context: dict | None = None,
) -> Path:
    _check_deadline(deadline)
    local_deadline = time.monotonic() + GIT_OPERATION_TIMEOUT
    effective_deadline = (
        local_deadline if deadline is None else min(deadline, local_deadline)
    )
    repository = _repository_path(repository)
    with _controller_group(
        repository,
        "git_validate",
        effective_deadline,
        operation_context=operation_context,
    ) as controller:
        return _validate_root(
            repository,
            _GitOperation(
                repository,
                effective_deadline,
                outer_deadline=deadline,
                controller=controller,
            ),
        )


@dataclass(frozen=True)
class FileContent:
    data: bytes
    mode: int

    def entry(self, name: str, *, deadline: float | None = None) -> dict:
        digest = hashlib.sha256()
        for offset in range(0, len(self.data), IO_CHUNK_BYTES):
            _check_deadline(deadline)
            digest.update(memoryview(self.data)[offset : offset + IO_CHUNK_BYTES])
        _check_deadline(deadline)
        return {
            "path": name,
            "sha256": digest.hexdigest(),
            "size": len(self.data),
            "mode": self.mode,
        }


def read_files(
    path: Path,
    *,
    exclude_generated: bool = True,
    max_files: int | None = None,
    max_file_bytes: int | None = None,
    max_total_bytes: int | None = None,
    max_depth: int = 100,
    directories: list[dict] | None = None,
    deadline: float | None = None,
) -> dict[str, FileContent]:
    """Read with directory descriptors; no path component may become a symlink."""
    path = Path(path)
    max_files = MAX_FILES if max_files is None else max_files
    max_file_bytes = MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
    max_total_bytes = MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
    files: dict[str, FileContent] = {}
    total = 0
    entry_count = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def walk(directory_fd: int, prefix: str = "") -> None:
        nonlocal total, entry_count
        _check_deadline(deadline)
        directory_before = os.fstat(directory_fd)
        if directories is not None:
            directories.append(
                {
                    "path": prefix.rstrip("/"),
                    "mode": stat.S_IMODE(directory_before.st_mode),
                }
            )
        names = []
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                _check_deadline(deadline)
                entry_count += 1
                if entry_count > max_files:
                    raise WorkspaceError("Project entry limit exceeded")
                names.append(entry.name)
        for name in sorted(names):
            _check_deadline(deadline)
            relative = prefix + name
            _allowed_path(relative)
            if relative.count("/") > max_depth:
                raise WorkspaceError("Project directory depth limit exceeded")
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
                raise WorkspaceError(
                    f"Symlink or special file is forbidden: {relative}"
                )
            if exclude_generated and _generated_path(relative):
                continue
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, flags | os.O_DIRECTORY, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise WorkspaceError("Project changed during snapshot")
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode):
                if before.st_mode & 0o7000:
                    raise WorkspaceError("Privileged file mode is forbidden")
                if len(files) >= max_files or before.st_size > max_file_bytes:
                    raise WorkspaceError("Project file limit exceeded")
                if total + before.st_size > max_total_bytes:
                    raise WorkspaceError("Project size limit exceeded")
                descriptor = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode) or (
                        opened.st_dev,
                        opened.st_ino,
                    ) != (before.st_dev, before.st_ino):
                        raise WorkspaceError("Project changed during snapshot")
                    content = bytearray()
                    while len(content) <= max_file_bytes:
                        _check_deadline(deadline)
                        chunk = os.read(
                            descriptor,
                            min(IO_CHUNK_BYTES, max_file_bytes + 1 - len(content)),
                        )
                        if not chunk:
                            break
                        content.extend(chunk)
                    _check_deadline(deadline)
                    after = os.fstat(descriptor)
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise WorkspaceError("Project changed during snapshot")
                    if len(content) > max_file_bytes or len(content) != opened.st_size:
                        raise WorkspaceError("Project changed or file limit exceeded")
                    total += len(content)
                    if total > max_total_bytes:
                        raise WorkspaceError("Project size limit exceeded")
                    files[relative] = FileContent(
                        bytes(content), stat.S_IMODE(opened.st_mode)
                    )
                finally:
                    os.close(descriptor)
            else:
                raise WorkspaceError(
                    f"Symlink or special file is forbidden: {relative}"
                )
        if directories is not None:
            directory_after = os.fstat(directory_fd)
            if (directory_before.st_mtime_ns, directory_before.st_ctime_ns) != (
                directory_after.st_mtime_ns,
                directory_after.st_ctime_ns,
            ):
                raise WorkspaceError("Project directory changed during snapshot")

    _check_deadline(deadline)
    try:
        directory_fd = os.open(path, flags | os.O_DIRECTORY)
        try:
            walk(directory_fd)
        finally:
            os.close(directory_fd)
    except ExecutionDeadlineExceeded:
        raise
    except OSError as error:
        raise WorkspaceError(f"Cannot safely read project: {error.strerror}") from error
    _check_deadline(deadline)
    return files


def manifest(
    files: dict[str, FileContent], *, deadline: float | None = None
) -> list[dict]:
    _check_deadline(deadline)
    return [
        content.entry(name, deadline=deadline)
        for name, content in sorted(files.items())
    ]


def _json_digest(value: object, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        _check_deadline(deadline)
        digest.update(chunk.encode())
    _check_deadline(deadline)
    return digest.hexdigest()


def manifest_digest(entries: list[dict], *, deadline: float | None = None) -> str:
    return _json_digest(entries, deadline=deadline)


def write_files(
    files: dict[str, FileContent], destination: Path, *, deadline: float | None = None
) -> None:
    _check_deadline(deadline)
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in sorted(files.items()):
        _check_deadline(deadline)
        _allowed_path(name)
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as output:
            for offset in range(0, len(content.data), IO_CHUNK_BYTES):
                _check_deadline(deadline)
                output.write(memoryview(content.data)[offset : offset + IO_CHUNK_BYTES])
        _check_deadline(deadline)
        target.chmod(content.mode & 0o777)
    _check_deadline(deadline)


def _object_id(value: bytes) -> bool:
    return len(value) in (40, 64) and all(char in b"0123456789abcdef" for char in value)


def _export_files(operation: _GitOperation, commit: str) -> dict[str, FileContent]:
    """Count raw tree records before exclusions, then verify each batch object."""
    pending = bytearray()
    entries: list[tuple[str, bytes, bytes, int]] = []
    names: set[str] = set()
    raw_count = 0
    total = 0

    def tree_chunk(chunk: bytes) -> None:
        nonlocal raw_count, total
        pending.extend(chunk)
        start = 0
        while (end := pending.find(b"\0", start)) >= 0:
            operation.check_deadline()
            raw_count += 1
            if raw_count > MAX_GIT_TREE_ENTRIES:
                raise WorkspaceError("Git raw tree entry limit exceeded")
            if end - start > MAX_GIT_TREE_RECORD_BYTES:
                raise WorkspaceError("Git tree record limit exceeded")
            record = bytes(pending[start:end])
            start = end + 1
            try:
                metadata, name_bytes = record.split(b"\t", 1)
                mode, kind, object_id, raw_size = metadata.split()
            except ValueError as error:
                raise WorkspaceError("Malformed Git tree record") from error
            if len(name_bytes) > MAX_GIT_PATH_BYTES:
                raise WorkspaceError("Git project path limit exceeded")
            name = os.fsdecode(name_bytes)
            _allowed_path(name)
            if name.count("/") > 100:
                raise WorkspaceError("Git project directory depth limit exceeded")
            if name in names:
                raise WorkspaceError("Duplicate Git tree path")
            names.add(name)
            if mode not in (b"100644", b"100755") or kind != b"blob":
                raise WorkspaceError(
                    f"Symlink, submodule or special Git entry is forbidden: {name}"
                )
            if (
                not _object_id(object_id)
                or not raw_size.isdigit()
                or len(raw_size) > 20
            ):
                raise WorkspaceError("Malformed Git object identity or size")
            if _generated_path(name):
                continue
            size = int(raw_size)
            if (
                size > MAX_FILE_BYTES
                or len(entries) >= MAX_FILES
                or total + size > MAX_TOTAL_BYTES
            ):
                raise WorkspaceError("Project resource limit exceeded")
            total += size
            entries.append((name, mode, object_id, size))
        del pending[:start]
        if len(pending) > MAX_GIT_TREE_RECORD_BYTES:
            raise WorkspaceError("Git tree record limit exceeded")

    operation.run(
        "ls-tree",
        "-r",
        "-z",
        "-l",
        commit,
        stdout_limit=MAX_GIT_TREE_BYTES,
        consume=tree_chunk,
    )
    if pending:
        raise WorkspaceError("Incomplete Git tree record")
    if not entries:
        return {}

    files: dict[str, FileContent] = {}
    index = 0
    header = bytearray()
    content = bytearray()
    content_digest = None
    reading_header = True
    needs_newline = False

    def object_chunk(chunk: bytes) -> None:
        nonlocal index, reading_header, needs_newline, content_digest
        offset = 0
        while offset < len(chunk):
            operation.check_deadline()
            if index >= len(entries):
                raise WorkspaceError("Unexpected extra Git object output")
            name, mode, object_id, size = entries[index]
            if reading_header:
                end = chunk.find(b"\n", offset)
                header.extend(chunk[offset : end if end >= 0 else len(chunk)])
                if len(header) > 128:
                    raise WorkspaceError("Git object header limit exceeded")
                if end < 0:
                    return
                parts = bytes(header).split(b" ")
                if parts != [object_id, b"blob", str(size).encode()]:
                    raise WorkspaceError("Git object identity, type or size changed")
                header.clear()
                content_digest = hashlib.new(
                    "sha1" if len(object_id) == 40 else "sha256",
                    b"blob " + str(size).encode() + b"\0",
                )
                reading_header = False
                needs_newline = size == 0
                offset = end + 1
            elif needs_newline:
                if chunk[offset] != 10:
                    raise WorkspaceError("Malformed Git object terminator")
                if (
                    content_digest is None
                    or content_digest.hexdigest().encode() != object_id
                ):
                    raise WorkspaceError(
                        "Git object content does not match its identity"
                    )
                files[name] = FileContent(
                    bytes(content), 0o755 if mode == b"100755" else 0o644
                )
                content.clear()
                content_digest = None
                index += 1
                reading_header = True
                needs_newline = False
                offset += 1
            else:
                length = min(size - len(content), len(chunk) - offset)
                content.extend(chunk[offset : offset + length])
                assert content_digest is not None
                content_digest.update(chunk[offset : offset + length])
                offset += length
                needs_newline = len(content) == size

    operation.run(
        "cat-file",
        "--batch",
        input_bytes=b"\n".join(entry[2] for entry in entries) + b"\n",
        stdout_limit=total + len(entries) * 130,
        consume=object_chunk,
    )
    if index != len(entries) or header or content or not reading_header:
        raise WorkspaceError("Incomplete Git object output")
    operation.check_deadline()
    return files


@dataclass(frozen=True)
class Workspace:
    path: Path
    repository: Path | None = None
    base_commit: str | None = None
    baseline_path: Path | None = None
    baseline_digest: str | None = None
    operation_context: dict | None = None

    @classmethod
    def create(
        cls,
        repository: Path,
        commit: str,
        destination: Path,
        *,
        baseline_path: Path | None = None,
        deadline: float | None = None,
        operation_context: dict | None = None,
    ) -> Workspace:
        _check_deadline(deadline)
        local_deadline = time.monotonic() + GIT_OPERATION_TIMEOUT
        effective_deadline = (
            local_deadline if deadline is None else min(deadline, local_deadline)
        )
        operation_context = (
            _controller_context(operation_context)
            if operation_context is not None
            else None
        )
        try:
            repository = _repository_path(repository)
            with _controller_group(
                repository,
                "git_export",
                effective_deadline,
                intent=(commit,),
                operation_context=operation_context,
            ) as controller:
                return cls._create(
                    repository,
                    commit,
                    destination,
                    baseline_path=baseline_path,
                    deadline=effective_deadline,
                    outer_deadline=deadline,
                    controller=controller,
                    operation_context=operation_context,
                )
        except ExecutionDeadlineExceeded:
            _check_deadline(deadline)
            # A larger outer allowance never widens the export's local limit.
            raise _GitLocalDeadlineExceeded(
                "Project export deadline exceeded"
            ) from None

    @classmethod
    def _create(
        cls,
        repository: Path,
        commit: str,
        destination: Path,
        *,
        baseline_path: Path | None,
        deadline: float,
        outer_deadline: float | None,
        controller: _GitControllerGroup | None = None,
        operation_context: dict | None = None,
    ) -> Workspace:
        _check_deadline(deadline)
        repository = _repository_path(repository)
        operation = _GitOperation(
            repository, deadline, outer_deadline=outer_deadline, controller=controller
        )
        repository = _validate_root(repository, operation)
        if (
            not isinstance(commit, str)
            or not commit
            or commit.startswith("-")
            or len(os.fsencode(commit)) > MAX_GIT_PATH_BYTES
        ):
            raise WorkspaceError("An explicit starting commit is required")
        resolved = (
            operation.run(
                "rev-parse",
                "--verify",
                "--end-of-options",
                commit + "^{commit}",
                stdout_limit=65,
            )
            .stdout.decode()
            .strip()
        )
        if not _object_id(resolved.encode()):
            raise WorkspaceError(
                "Git starting commit did not resolve to a fixed object"
            )
        files = _export_files(operation, resolved)
        baseline_digest = None
        if baseline_path is not None:
            baseline_path = Path(baseline_path)
            destination = Path(destination)
            base_location, task_location = (
                baseline_path.resolve(),
                destination.resolve(),
            )
            if (
                base_location == task_location
                or base_location in task_location.parents
                or task_location in base_location.parents
            ):
                raise WorkspaceError(
                    "Baseline must be separate from the writable workspace"
                )
            baseline_digest = manifest_digest(
                manifest(files, deadline=deadline), deadline=deadline
            )
            if baseline_path.exists():
                existing = read_files(
                    baseline_path, exclude_generated=False, deadline=deadline
                )
                if (
                    manifest_digest(
                        manifest(existing, deadline=deadline), deadline=deadline
                    )
                    != baseline_digest
                ):
                    raise WorkspaceError(
                        "Existing baseline does not match the starting commit"
                    )
            else:
                write_files(files, baseline_path, deadline=deadline)
            baseline_path = baseline_path.resolve()
        write_files(files, Path(destination), deadline=deadline)
        resolved_destination = Path(destination).resolve()
        _check_deadline(deadline)
        return cls(
            resolved_destination,
            repository,
            resolved,
            baseline_path,
            baseline_digest,
            dict(operation_context) if operation_context is not None else None,
        )

    def base_files(
        self, *, deadline: float | None = None, operation_context: dict | None = None
    ) -> dict[str, FileContent]:
        """Read a database-bound initial copy, with a legacy M1 export fallback."""
        if self.baseline_path is not None or self.baseline_digest is not None:
            if self.baseline_path is None or not self.baseline_digest:
                raise WorkspaceError("Baseline requires its persisted content digest")
            files = read_files(
                self.baseline_path, exclude_generated=False, deadline=deadline
            )
            if (
                manifest_digest(manifest(files, deadline=deadline), deadline=deadline)
                != self.baseline_digest
            ):
                raise WorkspaceError("Starting baseline was changed")
            return files
        if self.repository is None or self.base_commit is None:
            raise WorkspaceError("Delivery requires an explicit Git starting commit")
        with tempfile.TemporaryDirectory(prefix="doonex-baseline-") as temporary:
            exported = Workspace.create(
                self.repository,
                self.base_commit,
                Path(temporary) / "baseline",
                deadline=deadline,
                operation_context=self.operation_context
                if operation_context is None
                else operation_context,
            )
            return read_files(exported.path, deadline=deadline)

    @staticmethod
    def validate_repository(
        path: Path,
        *,
        deadline: float | None = None,
        operation_context: dict | None = None,
    ) -> Path:
        return validate_repository(
            path, deadline=deadline, operation_context=operation_context
        )

    @staticmethod
    def scan(path: Path, *, deadline: float | None = None) -> list[dict]:
        return manifest(read_files(path, deadline=deadline), deadline=deadline)

    @staticmethod
    def snapshot_digest(path: Path, *, deadline: float | None = None) -> str:
        return manifest_digest(
            Workspace.scan(path, deadline=deadline), deadline=deadline
        )

    @staticmethod
    def execution_digest(path: Path, *, deadline: float | None = None) -> str:
        """Bind all tool-visible files and directory names/modes for recovery."""
        directories = []
        files = read_files(
            path, exclude_generated=False, directories=directories, deadline=deadline
        )
        return _json_digest(
            {"files": manifest(files, deadline=deadline), "directories": directories},
            deadline=deadline,
        )
