# ruff: noqa: BLE001
# Every BaseException must enter finite cleanup, including control exceptions.
"""One direct child per synchronous Responses transport attempt.

The four hooks own durable state; this module owns only its direct child.  A free
lease never proves reap, and no PID from a prior process is used for signaling.
Parent-side hooks/serialization are not forcibly bounded by the child timer.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import selectors
import signal
import stat
import struct
import sys
import sysconfig
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_MODE = "isolated_direct_v1"
_CLEANUP_SECONDS = 1.0
_TERM_SECONDS = 0.15
_POLL_SECONDS = 0.01
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CATCHABLE = {
    signal.SIGINT,
    signal.SIGTERM,
    signal.SIGHUP,
    signal.SIGQUIT,
    signal.SIGALRM,
}


FAILURE_REASONS = frozenset(
    {
        "success",
        "helper_error",
        "internal",
        "preflight",
        "register",
        "spawn",
        "ready",
        "arm",
        "terminal",
        "complete",
        "cleanup",
        "deadline",
        "lease_invalid",
        "lease_changed",
        "lease_path",
        "lease_directory",
        "lease_create_identity",
        "lease_locked",
        "parent_identity",
        "hook_ack",
        "frame_size",
        "early_eof",
        "write",
        "extra_frame",
        "reap_unknown",
        "child_exit",
        "child_identity",
        "lease_inspection_cleanup",
    }
)
CLEANUP_ERROR_CODES = frozenset(
    {
        "close",
        "selector_close",
        "wait_echild",
        "wait_eintr",
        "wait",
        "signal_missing",
        "signal",
        "cleanup_pause",
        "signal_mask",
        "signal_restore",
        "complete",
        "block",
    }
)


@dataclass(frozen=True)
class LeaseIdentity:
    device: int
    inode: int

    def data(self) -> dict:
        return {"device": self.device, "inode": self.inode}


@dataclass(frozen=True)
class LeaseSpec:
    path: Path
    expected_identity: LeaseIdentity | None = None
    allow_create: bool = False


@dataclass(frozen=True)
class LeaseInspection:
    identity: LeaseIdentity
    locked: bool


@dataclass(frozen=True)
class HelperRequest:
    call: dict = field(repr=False)
    api_key: str = field(repr=False)
    deadline_monotonic_ns: int
    lease: LeaseSpec
    parent_boot_id: str
    client_request_id: str | None = None


@dataclass(frozen=True)
class HelperHooks:
    register: Callable[[dict], dict]
    arm: Callable[[dict], dict]
    complete: Callable[[dict], dict]
    block: Callable[[dict], dict]


@dataclass(frozen=True)
class HelperTerminal:
    envelope: dict = field(repr=False)
    reap: dict
    completion: dict


class HelperProcessError(RuntimeError):
    """A bounded, non-secret failure; its evidence is not budget settlement."""

    def __init__(self, reason: str, evidence: dict | None = None):
        self.reason = reason if reason in FAILURE_REASONS else "internal"
        super().__init__(f"Responses helper failed: {self.reason}")
        self.evidence = evidence or {}


@dataclass(frozen=True)
class _Runtime:
    interpreter: Path
    bootstrap: Path
    protocol: Path
    transport: Path
    site_packages: Path


def _runtime() -> _Runtime:
    directory = Path(__file__).resolve().parent
    return _Runtime(
        Path(sys.executable).absolute(),
        directory / "responses_helper_bootstrap.py",
        directory / "responses_protocol.py",
        directory / "responses_transport.py",
        Path(sysconfig.get_paths()["purelib"]).absolute(),
    )


def _protocol():
    from . import responses_protocol

    return responses_protocol


def _deadline(deadline_ns: int) -> float:
    remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
    if remaining <= 0:
        raise HelperProcessError("deadline")
    return remaining


def _identity(fd: int) -> LeaseIdentity:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        raise HelperProcessError("lease_invalid")
    return LeaseIdentity(info.st_dev, info.st_ino)


def _check_path(fd: int, directory_fd: int, name: str) -> LeaseIdentity:
    identity = _identity(fd)
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (info.st_dev, info.st_ino) != (identity.device, identity.inode):
        raise HelperProcessError("lease_changed")
    return identity


def _open_lease(
    spec: LeaseSpec, resources: _Resources
) -> tuple[int, int, LeaseIdentity]:
    path = Path(spec.path)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise HelperProcessError("lease_path")
    if path.parent.resolve(strict=True) != path.parent:
        raise HelperProcessError("lease_path")
    directory_fd = resources.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    parent = os.fstat(directory_fd)
    if stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.geteuid():
        raise HelperProcessError("lease_directory")
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    creating = spec.allow_create
    if creating:
        if spec.expected_identity is not None:
            raise HelperProcessError("lease_create_identity")
        flags |= os.O_CREAT | os.O_EXCL
    fd = resources.open(path.name, flags, 0o600, dir_fd=directory_fd)
    if creating:
        os.fsync(fd)
        os.fsync(directory_fd)
    identity = _check_path(fd, directory_fd, path.name)
    if spec.expected_identity is not None and identity != spec.expected_identity:
        raise HelperProcessError("lease_changed")
    return fd, directory_fd, identity


def inspect_lease(spec: LeaseSpec) -> LeaseInspection:
    """Inspect an existing inode.  Obtainable flock is not an exit/reap claim."""
    resources = _Resources()
    try:
        fd, directory_fd, identity = _open_lease(
            LeaseSpec(spec.path, spec.expected_identity, False), resources
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return LeaseInspection(identity, True)
        _check_path(fd, directory_fd, Path(spec.path).name)
        # Close our separate open description; never LOCK_UN an inherited lock.
        return LeaseInspection(identity, False)
    finally:
        resources.close_all()
        if resources.errors:
            raise HelperProcessError("lease_inspection_cleanup") from None


@contextmanager
def _defer_signals(resources: _Resources | None = None):
    # Allocation callers fail before allocating if masking fails.  Cleanup must
    # still run when masking/restoration itself fails, and records that failure.
    previous = None
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, _CATCHABLE)
    except BaseException as exc:
        if resources is None:
            raise
        resources.failure("signal_mask", exc)
    try:
        yield
    finally:
        if previous is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            except BaseException as exc:
                if resources is None:
                    raise
                resources.failure("signal_restore", exc)


class _Resources:
    def __init__(self):
        self.fds: set[int] = set()
        self.selector: selectors.BaseSelector | None = None
        self.pid: int | None = None
        self.spawn_attempted = False
        self.reap: dict | None = None
        self.authority = True
        self.errors: list[str] = []
        self.liveness: int | None = None
        self.control: BaseException | None = None
        self.cleaning = False

    def error(self, code: str):
        if code not in self.errors and len(self.errors) < 16:
            self.errors.append(code)

    def failure(self, code: str, exc: BaseException):
        self.error(code)
        if not isinstance(exc, Exception) and self.control is None:
            self.control = exc

    def open(self, path, flags, mode=0o777, *, dir_fd=None) -> int:
        with _defer_signals():
            fd = os.open(path, flags, mode, dir_fd=dir_fd)
            self.fds.add(fd)
        return fd

    def duplicate(self, fd: int) -> int:
        with _defer_signals():
            result = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
            self.fds.add(result)
        return result

    def pipe(self) -> tuple[int, int]:
        # os.pipe returns non-inheritable FDs.  Register both before any later work.
        with _defer_signals():
            read_fd, write_fd = os.pipe()
            self.fds.update((read_fd, write_fd))
        return read_fd, write_fd

    def close(self, fd: int | None):
        if fd is None or fd not in self.fds:
            return
        # Do not retry close(EINTR): the numeric FD might already have been closed.
        self.fds.remove(fd)
        try:
            os.close(fd)
        except BaseException as exc:
            self.failure("close", exc)
            if not self.cleaning:
                raise

    def close_all(self):
        self.cleaning = True
        if self.selector is not None:
            try:
                self.selector.close()
            except BaseException as exc:
                self.failure("selector_close", exc)
            self.selector = None
        for fd in list(self.fds):
            self.close(fd)

    def poll_reap(self):
        if self.pid is None or self.reap is not None or not self.authority:
            return
        try:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.authority = False
            self.error("wait_echild")
        except InterruptedError:
            self.error("wait_eintr")
        except BaseException as exc:
            self.failure("wait", exc)
        else:
            if waited == self.pid:
                self.reap = {
                    "pid": waited,
                    "wait_status": status,
                    "exit_code": os.waitstatus_to_exitcode(status),
                    "observed_monotonic_ns": time.monotonic_ns(),
                }

    def kill(self, sig: int):
        if self.pid is None or self.reap is not None or not self.authority:
            return
        try:
            # Direct-child PID cannot be reused while it remains unreaped.  Do not
            # use a process group or any persisted/restarted PID as authority.
            os.kill(self.pid, sig)
        except ProcessLookupError:
            self.error("signal_missing")
        except BaseException as exc:
            self.failure("signal", exc)

    def pause(self):
        try:
            time.sleep(_POLL_SECONDS)
        except BaseException as exc:
            self.failure("cleanup_pause", exc)

    def cleanup(self):
        self.cleaning = True
        self.close(self.liveness)
        end = time.monotonic() + _CLEANUP_SECONDS
        self.poll_reap()
        if self.pid is not None and self.reap is None and self.authority:
            self.kill(signal.SIGTERM)
            term_end = min(end, time.monotonic() + _TERM_SECONDS)
            while self.reap is None and self.authority and time.monotonic() < term_end:
                self.poll_reap()
                if self.reap is None:
                    self.pause()
            self.kill(signal.SIGKILL)
            while self.reap is None and self.authority and time.monotonic() < end:
                self.poll_reap()
                if self.reap is None:
                    self.pause()
        self.close_all()


def _ack(reply: Any, base: dict, states: set[str]) -> dict:
    if (
        type(reply) is not dict
        or set(reply) != {"schema", "attempt_id", "nonce", "state", "record_sha256"}
        or type(reply["schema"]) is not int
        or reply["schema"] != 1
        or reply["attempt_id"] != base["attempt_id"]
        or reply["nonce"] != base["nonce"]
        or reply["state"] not in states
        or not isinstance(reply["record_sha256"], str)
        or not _HASH.fullmatch(reply["record_sha256"])
    ):
        raise HelperProcessError("hook_ack")
    return dict(reply)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spawn(
    resources: _Resources,
    runtime: _Runtime,
    base: dict,
    request: HelperRequest,
    lease_fd: int,
    directory_fd: int,
    child_in: int,
    child_out: int,
    child_alive: int,
) -> int:
    # Duplicate every source above the fixed target range before constructing the
    # file actions: a dup2 must not overwrite a later source FD.
    sources = []
    null_fd = resources.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
    for fd in (child_in, child_out, lease_fd, child_alive, directory_fd, null_fd):
        sources.append(resources.duplicate(fd))
    actions = [
        (os.POSIX_SPAWN_DUP2, source, target)
        for source, target in zip(sources[:5], (3, 4, 5, 6, 7))
    ]
    actions.extend((os.POSIX_SPAWN_DUP2, sources[5], target) for target in (0, 1, 2))
    argv = [
        str(runtime.interpreter),
        "-I",
        "-S",
        str(runtime.bootstrap),
        base["attempt_id"],
        base["client_request_id"],
        base["nonce"],
        str(request.deadline_monotonic_ns),
        str(runtime.site_packages),
        _hash(runtime.protocol),
        _hash(runtime.transport),
    ]
    _deadline(request.deadline_monotonic_ns)
    with _defer_signals():
        resources.spawn_attempted = True
        resources.pid = os.posix_spawn(
            str(runtime.interpreter),
            argv,
            {"LANG": "C", "LC_ALL": "C"},
            file_actions=actions,
            setsid=True,
            setsigmask=(),
            setsigdef=(
                signal.SIGALRM,
                signal.SIGINT,
                signal.SIGTERM,
                signal.SIGHUP,
                signal.SIGQUIT,
                signal.SIGPIPE,
            ),
        )
    for fd in sources:
        resources.close(fd)
    resources.close(null_fd)
    resources.close(child_in)
    resources.close(child_out)
    resources.close(child_alive)
    return resources.pid


def _read_frame(resources: _Resources, fd: int, limit: int, deadline_ns: int) -> bytes:
    header = _read_exact(resources, fd, 4, deadline_ns)
    length = struct.unpack("!I", header)[0]
    if length == 0 or length > limit:
        raise HelperProcessError("frame_size")
    return _read_exact(resources, fd, length, deadline_ns)


def _read_exact(resources: _Resources, fd: int, count: int, deadline_ns: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        remaining = _deadline(deadline_ns)
        events = resources.selector.select(min(remaining, 0.05))
        if not events:
            continue
        try:
            chunk = os.read(fd, min(count - len(chunks), 65536))
        except (BlockingIOError, InterruptedError):
            continue
        if not chunk:
            raise HelperProcessError("early_eof")
        chunks.extend(chunk)
    return bytes(chunks)


def _write_frame(resources: _Resources, fd: int, frame: bytes, deadline_ns: int):
    resources.selector.modify(fd, selectors.EVENT_WRITE)
    view = memoryview(frame)
    while view:
        remaining = _deadline(deadline_ns)
        if not resources.selector.select(min(remaining, 0.05)):
            continue
        try:
            count = os.write(fd, view[:65536])
        except (BlockingIOError, InterruptedError):
            continue
        if count <= 0:
            raise HelperProcessError("write")
        view = view[count:]


def _eof_and_exit(resources: _Resources, fd: int, deadline_ns: int):
    while True:
        remaining = _deadline(deadline_ns)
        if resources.selector.select(min(remaining, 0.05)):
            try:
                tail = os.read(fd, 1)
            except (BlockingIOError, InterruptedError):
                continue
            if tail:
                raise HelperProcessError("extra_frame")
            break
    while resources.reap is None and resources.authority:
        _deadline(deadline_ns)
        resources.poll_reap()
        if resources.reap is None:
            time.sleep(_POLL_SECONDS)
    if resources.reap is None:
        raise HelperProcessError("reap_unknown")
    if resources.reap["exit_code"] != 0:
        raise HelperProcessError("child_exit")


def run_helper(request: HelperRequest, hooks: HelperHooks) -> HelperTerminal:
    """Run once; never retry.  Hooks are durable, parent-only acknowledgments.

    Hook input schema is documented by its exact construction below.  A hook's
    exception/ambiguous commit cannot authorize spawn/ARM/result acceptance.
    """
    resources = _Resources()  # Outer ledger exists before every FD allocation.
    base = {
        "schema": 1,
        "transport": _MODE,
        "attempt_id": str(uuid4()),
        "client_request_id": request.client_request_id or str(uuid4()),
        "nonce": os.urandom(32).hex(),
    }
    identity = {key: base[key] for key in ("attempt_id", "client_request_id", "nonce")}
    previous: dict | None = None
    terminal: dict | None = None
    reason = "internal"
    original: BaseException | None = None
    result: HelperTerminal | None = None
    stage = "preflight"
    try:
        _deadline(request.deadline_monotonic_ns)
        if (
            type(request.parent_boot_id) is not str
            or not 1 <= len(request.parent_boot_id) <= 128
        ):
            raise HelperProcessError("parent_identity")
        protocol = _protocol()
        arm = {
            "v": 1,
            "transport": _MODE,
            **identity,
            "kind": "arm",
            "deadline_monotonic_ns": request.deadline_monotonic_ns,
            "api_key": request.api_key,
            "call": request.call,
        }
        frame = protocol.encode_frame(arm, "arm")
        runtime = _runtime()
        lease_fd, directory_fd, lease_identity = _open_lease(request.lease, resources)
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HelperProcessError("lease_locked") from None
        _check_path(lease_fd, directory_fd, Path(request.lease.path).name)
        stage = "register"
        _deadline(request.deadline_monotonic_ns)
        previous = _ack(
            hooks.register(
                {
                    **base,
                    "parent": {
                        "pid": os.getpid(),
                        "observed_monotonic_ns": time.monotonic_ns(),
                        "boot_id": request.parent_boot_id,
                    },
                    "deadline_monotonic_ns": request.deadline_monotonic_ns,
                    "observed_at_utc": datetime.now(UTC).isoformat(),
                    "lease": lease_identity.data(),
                }
            ),
            base,
            {"prepared"},
        )
        _deadline(request.deadline_monotonic_ns)
        child_in, parent_in = resources.pipe()
        parent_out, child_out = resources.pipe()
        child_alive, resources.liveness = resources.pipe()
        stage = "spawn"
        pid = _spawn(
            resources,
            runtime,
            base,
            request,
            lease_fd,
            directory_fd,
            child_in,
            child_out,
            child_alive,
        )
        os.set_blocking(parent_in, False)
        os.set_blocking(parent_out, False)
        with _defer_signals():
            resources.selector = selectors.DefaultSelector()
        resources.selector.register(parent_out, selectors.EVENT_READ)
        stage = "ready"
        ready = protocol.decode_frame_payload(
            _read_frame(
                resources,
                parent_out,
                protocol.FRAME_LIMITS["ready"],
                request.deadline_monotonic_ns,
            ),
            "ready",
            identity,
        )
        if (
            ready["pid"] != pid
            or ready["pgid"] != pid
            or (ready["lease_dev"], ready["lease_ino"])
            != (lease_identity.device, lease_identity.inode)
            or os.getpgid(pid) != pid
        ):
            raise HelperProcessError("child_identity")
        _check_path(lease_fd, directory_fd, Path(request.lease.path).name)
        stage = "arm"
        _deadline(request.deadline_monotonic_ns)
        previous = _ack(
            hooks.arm(
                {
                    **base,
                    "previous_record_sha256": previous["record_sha256"],
                    "child": {"pid": pid, "pgid": pid},
                    "lease": lease_identity.data(),
                    "deadline_monotonic_ns": request.deadline_monotonic_ns,
                }
            ),
            base,
            {"armed"},
        )
        _deadline(request.deadline_monotonic_ns)
        resources.selector.unregister(parent_out)
        resources.selector.register(parent_in, selectors.EVENT_WRITE)
        _write_frame(resources, parent_in, frame, request.deadline_monotonic_ns)
        resources.selector.unregister(parent_in)
        resources.close(parent_in)
        resources.selector.register(parent_out, selectors.EVENT_READ)
        stage = "terminal"
        terminal = protocol.decode_frame_payload(
            _read_frame(
                resources,
                parent_out,
                protocol.FRAME_LIMITS["terminal"],
                request.deadline_monotonic_ns,
            ),
            "terminal",
            identity,
        )
        _eof_and_exit(resources, parent_out, request.deadline_monotonic_ns)
        reason = "success" if terminal["kind"] == "result" else "helper_error"
    except BaseException as exc:
        original = exc
        reason = exc.reason if isinstance(exc, HelperProcessError) else stage
    finally:
        # All resources, including partially allocated setup/spawn, take this path.
        try:
            with _defer_signals(resources):
                resources.cleanup()
                if resources.control is not None and original is None:
                    original = resources.control
                    reason = "cleanup"
                if resources.errors and reason in {"success", "helper_error"}:
                    original = original or HelperProcessError("cleanup")
                    reason = "cleanup"
                if resources.reap is not None and previous is not None:
                    # Receipt must be persisted even after expiry.  Expiry may
                    # never turn that receipt into an accepted SDK result.
                    try:
                        _deadline(request.deadline_monotonic_ns)
                    except HelperProcessError as exc:
                        if original is None:
                            original = exc
                            reason = "deadline"
                    stage = "complete"
                    try:
                        completion = _ack(
                            hooks.complete(
                                {
                                    **base,
                                    "previous_record_sha256": previous["record_sha256"],
                                    "reap": resources.reap,
                                    "terminal_summary": {
                                        "kind": terminal["kind"]
                                        if terminal is not None
                                        else "none",
                                        "outcome": "success"
                                        if reason == "success"
                                        else "failed",
                                        "reason": reason,
                                    },
                                }
                            ),
                            base,
                            {"reaped"},
                        )
                        previous = completion
                        if reason in {"success", "helper_error"}:
                            _deadline(request.deadline_monotonic_ns)
                            if resources.errors:
                                raise HelperProcessError("cleanup")
                            result = HelperTerminal(
                                terminal, resources.reap, completion
                            )
                    except BaseException as exc:
                        resources.error("complete")
                        if original is None:
                            original = exc
                            reason = (
                                exc.reason
                                if isinstance(exc, HelperProcessError)
                                else "complete"
                            )
                if previous is None or previous["state"] != "reaped":
                    try:
                        _ack(
                            hooks.block(
                                {
                                    **base,
                                    "previous_record_sha256": previous["record_sha256"]
                                    if previous
                                    else None,
                                    "spawn_attempted": resources.spawn_attempted,
                                    "child_pid": resources.pid,
                                    "reaped": resources.reap is not None,
                                    "errors": list(resources.errors),
                                    "reason": reason,
                                }
                            ),
                            base,
                            {"blocked", "reaped", "not_spawned"},
                        )
                    except BaseException:
                        resources.error("block")
        except BaseException as exc:
            # A signal delivered while restoring the mask still sees a closed
            # resource ledger and the durable prepared/armed/reaped record.
            if original is None:
                original = exc
                reason = "cleanup"
        resources.close_all()
        if resources.control is not None and original is None:
            original = resources.control
            reason = "cleanup"
    if original is not None and not isinstance(original, Exception):
        raise original
    if result is not None and original is None:
        return result
    evidence = {
        "attempt_id": base["attempt_id"],
        "spawn_attempted": resources.spawn_attempted,
        "child_pid": resources.pid,
        "reaped": resources.reap is not None,
        "errors": list(resources.errors),
    }
    raise HelperProcessError(reason, evidence) from None
