"""Bounded, uncached persistence for the pinned local SDK conversation.

This is a cooperative local IO budget, not an interruptible filesystem SLA.
Any observed failure is latched: SDK scan/marker handlers may swallow errors,
but must never turn that failure into a writable empty conversation.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from openhands.sdk.conversation.persistence_const import EVENT_NAME_RE
from openhands.sdk.io import FileStore
from pydantic import BaseModel, SecretStr

MIB = 1024 * 1024


class SessionStorageError(RuntimeError):
    """Session persistence failed closed; preserve its files for inspection."""


@dataclass(frozen=True)
class SessionLimits:
    base_bytes: int = 16 * MIB
    event_bytes: int = 16 * MIB
    metadata_bytes: int = 16 * 1024
    total_bytes: int = 64 * MIB
    peak_bytes: int = 80 * MIB
    events: int = 1024
    entries: int = 2048
    path_bytes: int = 1024
    depth: int = 8


LIMITS = SessionLimits()


def check_source(value, *, max_bytes=16 * MIB, check=lambda: None):
    """Bound source shape before Pydantic/JSON allocation, including base fields.

    This limits work and source strings; it is not the exact encoded size.
    FileStore enforces exact UTF-8 size after serialization as well.
    """
    nodes, size, active = 0, 0, set()

    def walk(item, depth=0):
        nonlocal nodes, size
        check()
        nodes += 1
        if nodes > 65536 or depth > 32:
            raise SessionStorageError("Session source shape exceeded")
        if item is None or isinstance(item, (bool, SecretStr)):
            size += 16
        elif isinstance(item, Enum):
            walk(item.value, depth + 1)
        elif isinstance(item, (uuid.UUID, datetime)):
            size += 64
        elif isinstance(item, int):
            if item.bit_length() > 64:
                raise SessionStorageError("Session integer exceeded")
            size += 24
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise SessionStorageError("Nonfinite session number")
            size += 32
        elif isinstance(item, str):
            if len(item) > max_bytes:
                raise SessionStorageError("Session source capacity exceeded")
            for offset in range(0, len(item), 65536):
                check()
                size += len(item[offset : offset + 65536].encode("utf-8"))
                if size > max_bytes:
                    raise SessionStorageError("Session source capacity exceeded")
            size += 2
        elif isinstance(item, type) and (item.__module__, item.__name__) in {
            ("doonex.sdk", "ShellAction"),
            ("doonex.sdk", "ShellObservation"),
            ("openhands.sdk.tool.builtins.finish", "FinishAction"),
            ("openhands.sdk.tool.builtins.finish", "FinishObservation"),
        }:
            # ToolDefinition serializes these four pinned classes as schemas.
            # No arbitrary user class or serializer is admitted here.
            walk(item.model_json_schema(), depth + 1)
        elif isinstance(item, (BaseModel, dict, list, tuple)):
            if id(item) in active:
                raise SessionStorageError("Cyclic session source")
            active.add(id(item))
            try:
                if isinstance(item, BaseModel):
                    for name, info in type(item).model_fields.items():
                        if info.exclude is not True:
                            walk(name, depth + 1)
                            walk(getattr(item, name), depth + 1)
                elif isinstance(item, dict):
                    if len(item) > 4096:
                        raise SessionStorageError("Session object capacity exceeded")
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise TypeError("Invalid session object key")
                        walk(key, depth + 1)
                        walk(child, depth + 1)
                else:
                    if len(item) > 4096:
                        raise SessionStorageError("Session list capacity exceeded")
                    for child in item:
                        walk(child, depth + 1)
            finally:
                active.remove(id(item))
        else:
            raise TypeError("Unsupported session source")
        if size > max_bytes:
            raise SessionStorageError("Session source capacity exceeded")

    walk(value)


def canonical_sha256(value, *, limit=128 * MIB, check=lambda: None):
    """Same canonical bytes as before, hashed without a second whole JSON copy."""
    import hashlib

    digest, size = hashlib.sha256(), 0
    check()
    for chunk in json.JSONEncoder(
        sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).iterencode(value):
        check()
        encoded = chunk.encode("utf-8")
        size += len(encoded)
        if size > limit:
            raise SessionStorageError("Session canonical capacity exceeded")
        digest.update(encoded)
    check()
    return digest.hexdigest()


def _identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


class SessionFileStore(FileStore):
    def __init__(self, root: Path, *, limits=LIMITS, deadline=None):
        self.root = str(root)
        self.limits, self.deadline = limits, deadline
        self._failure = None
        self._failure_cause = None
        self._fd = None
        self._files, self._dirs = {}, {}
        self._total = 0
        try:
            self.check_ok()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._root_identity = self._directory_identity(os.fstat(self._fd))
            self._inventory()
        except BaseException:
            self.close()
            raise

    def _fail(self, reason, *, cause=None):
        if self._failure is None:
            self._failure, self._failure_cause = reason, cause
        raise SessionStorageError(self._failure) from self._failure_cause

    def check_ok(self):
        if self._failure is not None:
            raise SessionStorageError(self._failure) from self._failure_cause
        from doonex.deadline import ExecutionDeadlineExceeded, check_deadline

        try:
            check_deadline(self.deadline)
        except ExecutionDeadlineExceeded as error:
            self._fail("Session persistence deadline exceeded", cause=error)

    def validate_source(self, value, *, max_bytes=16 * MIB):
        with self._operation():
            check_source(value, max_bytes=max_bytes, check=self.check_ok)

    def _json_shape(self, text):
        # Bound nesting and structural tokens before the SDK's json.loads.
        # String contents are already bounded by the exact file byte limit.
        quoted, escaped, depth, tokens = False, False, 0, 0
        for index, char in enumerate(text):
            if index % 65536 == 0:
                self.check_ok()
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
                tokens += 1
            elif char in "[{":
                depth += 1
                tokens += 1
                if depth > 32:
                    self._fail("Session JSON nesting exceeded")
            elif char in "]}":
                depth -= 1
            elif char in ":,":
                tokens += 1
            if tokens > 131072:
                self._fail("Session JSON shape exceeded")

    @contextmanager
    def _operation(self):
        self.check_ok()
        try:
            yield
            self.check_ok()
        except FileNotFoundError:
            raise
        except SessionStorageError:
            self._failure = self._failure or "Session persistence failed"
            raise
        except Exception:  # noqa: BLE001 -- latch even errors swallowed by SDK scan/marker handlers
            self._fail("Session persistence IO failed")

    def _parts(self, path):
        if not isinstance(path, str):
            self._fail("Invalid session path")
        value = path.rstrip("/")
        if value in ("", "."):
            return []
        parts = value.split("/")
        if (
            len(value) > self.limits.path_bytes
            or len(value.encode("utf-8")) > self.limits.path_bytes
            or len(parts) > self.limits.depth
            or any(
                part in ("", ".", "..")
                or "\\" in part
                or any(ord(char) < 32 or ord(char) == 127 for char in part)
                for part in parts
            )
        ):
            self._fail("Invalid session path")
        return parts

    @staticmethod
    def _directory_identity(info):
        if not stat.S_ISDIR(info.st_mode):
            raise SessionStorageError("Invalid session directory")
        return (info.st_dev, info.st_ino)

    def _file_limit(self, path):
        if path == "base_state.json":
            return self.limits.base_bytes
        if path.startswith("events/") and EVENT_NAME_RE.fullmatch(path[7:]):
            return self.limits.event_bytes
        return self.limits.metadata_bytes

    def _is_event(self, path):
        return path.startswith("events/") and bool(EVENT_NAME_RE.fullmatch(path[7:]))

    def _check_file(self, path, info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > self._file_limit(path)
            or Path(path).name.startswith(".doonex-write-")
        ):
            self._fail("Invalid or oversized session file")

    @contextmanager
    def _directory(self, parts, *, create=False):
        self.check_ok()
        if self._fd is None:
            self._fail("Session file store is closed")
        try:
            root_info = os.stat(self.root, follow_symlinks=False)
        except FileNotFoundError:
            self._fail("Session root disappeared")
        if self._directory_identity(root_info) != self._root_identity:
            self._fail("Session root changed")
        fd = os.dup(self._fd)
        try:
            for index, part in enumerate(parts):
                self.check_ok()
                path = "/".join(parts[: index + 1])
                if create and path not in self._dirs:
                    self._capacity(extra_entries=1)
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    self._dirs[path] = self._directory_identity(
                        os.stat(part, dir_fd=fd, follow_symlinks=False)
                    )
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                except FileNotFoundError:
                    if path in self._dirs:
                        self._fail("Session directory disappeared")
                    raise
                os.close(fd)
                fd = child
                if self._directory_identity(os.fstat(fd)) != self._dirs.get(path):
                    self._fail("Session directory changed")
            yield fd
        finally:
            os.close(fd)

    def _capacity(self, *, extra_entries=0, extra_events=0, total=None, peak=None):
        if (
            len(self._files) + len(self._dirs) + extra_entries > self.limits.entries
            or sum(self._is_event(path) for path in self._files) + extra_events
            > self.limits.events
            or (self._total if total is None else total) > self.limits.total_bytes
            or (0 if peak is None else peak) > self.limits.peak_bytes
        ):
            self._fail("Session capacity exceeded")

    def _read_fd(self, fd, path, info):
        self._check_file(path, info)
        chunks, remaining = [], info.st_size + 1
        while remaining:
            self.check_ok()
            data = os.read(fd, min(65536, remaining))
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        if remaining != 1 or _identity(info) != _identity(os.fstat(fd)):
            self._fail("Session file changed while reading")
        try:
            text = b"".join(chunks).decode("utf-8")
            if path.endswith(".json"):
                self._json_shape(text)
            return text
        except UnicodeError:
            self._fail("Session file is not UTF-8")

    def _inventory(self):
        def scan(fd, prefix=""):
            initial = os.fstat(fd)
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.check_ok()
                    path = prefix + entry.name
                    self._parts(path)
                    self._capacity(extra_entries=1)
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        self._dirs[path] = self._directory_identity(info)
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd,
                        )
                        try:
                            if (
                                self._directory_identity(os.fstat(child))
                                != self._dirs[path]
                            ):
                                self._fail("Session directory changed")
                            scan(child, path + "/")
                        finally:
                            os.close(child)
                    else:
                        self._check_file(path, info)
                        self._capacity(
                            extra_events=int(self._is_event(path)),
                            total=self._total + info.st_size,
                        )
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                            dir_fd=fd,
                        )
                        try:
                            if _identity(os.fstat(child)) != _identity(info):
                                self._fail("Session file changed")
                            self._read_fd(child, path, info)
                        finally:
                            os.close(child)
                        self._files[path] = _identity(info)
                        self._total += info.st_size
            final = os.fstat(fd)
            if (initial.st_mtime_ns, initial.st_ctime_ns) != (
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                self._fail("Session directory changed while scanning")

        with self._operation():
            scan(self._fd)

    def verify_inventory(self):
        """Metadata audit at admission/checkpoint boundaries, without a cache."""
        seen_files, seen_dirs = set(), set()

        def scan(fd, prefix=""):
            initial = os.fstat(fd)
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.check_ok()
                    if len(seen_files) + len(seen_dirs) >= self.limits.entries:
                        self._fail("Session directory capacity exceeded")
                    path = prefix + entry.name
                    self._parts(path)
                    info = self._lookup(fd, entry.name, path)
                    if stat.S_ISDIR(info.st_mode):
                        seen_dirs.add(path)
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd,
                        )
                        try:
                            if (
                                self._directory_identity(os.fstat(child))
                                != self._dirs[path]
                            ):
                                self._fail("Session directory changed")
                            scan(child, path + "/")
                        finally:
                            os.close(child)
                    else:
                        seen_files.add(path)
            final = os.fstat(fd)
            if (initial.st_mtime_ns, initial.st_ctime_ns) != (
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                self._fail("Session directory changed while scanning")

        with self._operation(), self._directory([]) as fd:
            scan(fd)
            if seen_files != self._files.keys() or seen_dirs != self._dirs.keys():
                self._fail("Session inventory changed")

    def _lookup(self, fd, name, path):
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            if path in self._files or path in self._dirs:
                self._fail("Session entry disappeared")
            raise
        if path in self._dirs:
            if self._directory_identity(info) != self._dirs[path]:
                self._fail("Session directory changed")
        elif path not in self._files or _identity(info) != self._files[path]:
            self._fail("Session file changed")
        else:
            self._check_file(path, info)
        return info

    def read(self, path):
        with self._operation():
            parts = self._parts(path)
            if not parts:
                self._fail("Cannot read a session directory")
            with self._directory(parts[:-1]) as fd:
                info = self._lookup(fd, parts[-1], path)
                child = os.open(
                    parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd
                )
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        self._fail("Session file changed")
                    return self._read_fd(child, path, info)
                finally:
                    os.close(child)

    def write(self, path, contents):
        with self._operation():
            parts = self._parts(path)
            if not parts or not isinstance(contents, (str, bytes)):
                self._fail("Invalid session write")
            if len(contents) > self._file_limit(path):
                self._fail("Session file capacity exceeded")
            data = contents.encode("utf-8") if isinstance(contents, str) else contents
            if len(data) > self._file_limit(path):
                self._fail("Session file capacity exceeded")
            data.decode("utf-8")
            old = self._files.get(path)
            self._capacity(
                extra_entries=int(old is None),
                extra_events=int(old is None and self._is_event(path)),
                total=self._total - (old[2] if old else 0) + len(data),
                peak=self._total + len(data),
            )
            with self._directory(parts[:-1], create=True) as fd:
                self._capacity(
                    extra_entries=int(old is None),
                    extra_events=int(old is None and self._is_event(path)),
                    total=self._total - (old[2] if old else 0) + len(data),
                    peak=self._total + len(data),
                )
                try:
                    self._lookup(fd, parts[-1], path)
                except FileNotFoundError:
                    pass
                temporary = ".doonex-write-" + uuid.uuid4().hex
                child = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
                try:
                    view = memoryview(data)
                    while view:
                        self.check_ok()
                        count = os.write(child, view[:65536])
                        if count <= 0:
                            self._fail("Incomplete session write")
                        view = view[count:]
                    os.fsync(child)
                    expected = _identity(os.fstat(child))
                finally:
                    os.close(child)
                self.check_ok()
                os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
                info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                # Rename changes ctime; the inode and content identity must agree.
                if _identity(info)[:4] != expected[:4] or info.st_nlink != 1:
                    self._fail("Session replacement changed")
                self._files[path] = _identity(info)
                self._total += len(data) - (old[2] if old else 0)

    def list(self, path):
        with self._operation():
            parts = self._parts(path)
            try:
                with self._directory(parts) as fd:
                    result = []
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            self.check_ok()
                            if len(result) >= self.limits.entries:
                                self._fail("Session directory capacity exceeded")
                            name = "/".join([*parts, entry.name])
                            info = self._lookup(fd, entry.name, name)
                            result.append(
                                name + ("/" if stat.S_ISDIR(info.st_mode) else "")
                            )
                    expected = {
                        name
                        for name in self._files | self._dirs
                        if name.split("/")[:-1] == parts
                    }
                    if {name.rstrip("/") for name in result} != expected:
                        self._fail("Session directory contents changed")
                    return result
            except FileNotFoundError:
                return []

    def exists(self, path):
        with self._operation():
            parts = self._parts(path)
            if not parts:
                return True
            try:
                with self._directory(parts[:-1]) as fd:
                    self._lookup(fd, parts[-1], path)
                return True
            except FileNotFoundError:
                return False

    def delete(self, path):
        with self._operation():
            parts = self._parts(path)
            if not parts:
                self._fail("Cannot delete session root")
            try:
                with self._directory(parts[:-1]) as fd:
                    info = self._lookup(fd, parts[-1], path)
                    if not stat.S_ISREG(info.st_mode):
                        self._fail("Only session files can be deleted")
                    os.unlink(parts[-1], dir_fd=fd)
                    self._total -= self._files.pop(path)[2]
            except FileNotFoundError:
                pass

    def get_absolute_path(self, path):
        with self._operation():
            return str(Path(self.root).joinpath(*self._parts(path)))

    @contextmanager
    def lock(self, path, timeout=30.0):
        with self._operation():
            if path != "events/.eventlog.lock":
                self._fail("Unsupported session lock")
            if not self.exists(path):
                self.write(path, "")
            with self._directory(["events"]) as fd:
                self._lookup(fd, ".eventlog.lock", path)
                child = os.open(
                    ".eventlog.lock",
                    os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=fd,
                )
                try:
                    if _identity(os.fstat(child)) != self._files[path]:
                        self._fail("Session lock changed")
                    end = time.monotonic() + max(0, timeout)
                    while True:
                        self.check_ok()
                        try:
                            fcntl.flock(child, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            remaining = (
                                min(
                                    end,
                                    self.deadline if self.deadline is not None else end,
                                )
                                - time.monotonic()
                            )
                            if remaining <= 0:
                                self.check_ok()
                                self._fail("Session lock deadline exceeded")
                            time.sleep(min(remaining, 0.01))
                    yield
                    self.check_ok()
                finally:
                    os.close(child)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        self.check_ok()
        return self

    def __exit__(self, *_):
        self.close()
