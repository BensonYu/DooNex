"""Bounded Git-object exports and symlink-safe private workspace snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_FILES = 10_000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_GIT_CONFIG_BYTES = 64 * 1024
MAX_GIT_FILTERS = 256
MAX_GIT_FILTER_NAME_BYTES = 16 * 1024
GIT_CONFIG_TIMEOUT = 5
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


def _git_config_names(command: list[str], environment: dict[str, str]) -> bytes:
    """Inspect effective keys, including includes, without printing their values."""
    with subprocess.Popen(
        [*command, "config", "--includes", "--null", "--name-only", "--list"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
    ) as process:
        assert process.stdout is not None
        deadline = time.monotonic() + GIT_CONFIG_TIMEOUT
        content = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise WorkspaceError("Git configuration inspection timed out")
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(8192, MAX_GIT_CONFIG_BYTES + 1 - len(content)),
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                    if len(content) > MAX_GIT_CONFIG_BYTES:
                        raise WorkspaceError("Git configuration key limit exceeded")
            process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired as error:
            raise WorkspaceError("Git configuration inspection timed out") from error
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode:
            raise WorkspaceError("Git configuration cannot be inspected safely")
        return bytes(content)


def _git(
    repository: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess:
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
    names = _git_config_names(command, environment).split(b"\0")
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
    if arguments and arguments[0] in (
        "diff",
        "diff-files",
        "diff-index",
        "diff-tree",
        "show",
        "log",
    ):
        arguments = (arguments[0], "--no-ext-diff", "--no-textconv", *arguments[1:])
    result = subprocess.run(
        [*command, *arguments],
        capture_output=True,
        env=environment,
        timeout=30,
        check=False,
    )
    if check and result.returncode:
        raise WorkspaceError(
            "Git project operation failed: "
            + result.stderr.decode(errors="replace")[:1000]
        )
    return result


def validate_repository(repository: Path) -> Path:
    repository = Path(repository).expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise WorkspaceError("Project path is not a directory")
    result = _git(repository, "rev-parse", "--show-toplevel")
    root = Path(os.fsdecode(result.stdout).strip()).resolve(strict=True)
    if root != repository:
        raise WorkspaceError("Register the Git project root, not a nested directory")
    if _git(root, "rev-parse", "--is-bare-repository").stdout.strip() != b"false":
        raise WorkspaceError("Bare repositories cannot be registered")
    return root


@dataclass(frozen=True)
class FileContent:
    data: bytes
    mode: int

    def entry(self, name: str) -> dict:
        return {
            "path": name,
            "sha256": hashlib.sha256(self.data).hexdigest(),
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
                entry_count += 1
                if entry_count > max_files:
                    raise WorkspaceError("Project entry limit exceeded")
                names.append(entry.name)
        for name in sorted(names):
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
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        content = source.read(max_file_bytes + 1)
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
                    files[relative] = FileContent(content, stat.S_IMODE(opened.st_mode))
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

    try:
        directory_fd = os.open(path, flags | os.O_DIRECTORY)
        try:
            walk(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise WorkspaceError(f"Cannot safely read project: {error.strerror}") from error
    return files


def manifest(files: dict[str, FileContent]) -> list[dict]:
    return [content.entry(name) for name, content in sorted(files.items())]


def manifest_digest(entries: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(
            entries, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def write_files(files: dict[str, FileContent], destination: Path) -> None:
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in sorted(files.items()):
        _allowed_path(name)
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content.data)
        target.chmod(content.mode & 0o777)


@dataclass(frozen=True)
class Workspace:
    path: Path
    repository: Path | None = None
    base_commit: str | None = None
    baseline_path: Path | None = None
    baseline_digest: str | None = None

    @classmethod
    def create(
        cls,
        repository: Path,
        commit: str,
        destination: Path,
        *,
        baseline_path: Path | None = None,
    ) -> Workspace:
        repository = validate_repository(repository)
        if not isinstance(commit, str) or not commit or commit.startswith("-"):
            raise WorkspaceError("An explicit starting commit is required")
        resolved = (
            _git(
                repository,
                "rev-parse",
                "--verify",
                "--end-of-options",
                commit + "^{commit}",
            )
            .stdout.decode()
            .strip()
        )
        objects = _git(repository, "ls-tree", "-r", "-z", resolved).stdout.split(b"\0")
        files = {}
        total = 0
        for record in objects:
            if not record:
                continue
            metadata, name_bytes = record.split(b"\t", 1)
            mode, kind, object_id = metadata.split(b" ")
            name = os.fsdecode(name_bytes)
            _allowed_path(name)
            if mode not in (b"100644", b"100755") or kind != b"blob":
                raise WorkspaceError(
                    f"Symlink, submodule or special Git entry is forbidden: {name}"
                )
            if _generated_path(name):
                continue
            size = int(_git(repository, "cat-file", "-s", object_id.decode()).stdout)
            if (
                size > MAX_FILE_BYTES
                or len(files) >= MAX_FILES
                or total + size > MAX_TOTAL_BYTES
            ):
                raise WorkspaceError("Project resource limit exceeded")
            data = _git(repository, "cat-file", "blob", object_id.decode()).stdout
            if len(data) != size:
                raise WorkspaceError("Git object size changed")
            total += size
            files[name] = FileContent(data, 0o755 if mode == b"100755" else 0o644)
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
            baseline_digest = manifest_digest(manifest(files))
            if baseline_path.exists():
                existing = read_files(baseline_path, exclude_generated=False)
                if manifest_digest(manifest(existing)) != baseline_digest:
                    raise WorkspaceError(
                        "Existing baseline does not match the starting commit"
                    )
            else:
                write_files(files, baseline_path)
            baseline_path = baseline_path.resolve()
        write_files(files, Path(destination))
        return cls(
            Path(destination).resolve(),
            repository,
            resolved,
            baseline_path,
            baseline_digest,
        )

    def base_files(self) -> dict[str, FileContent]:
        """Read a database-bound initial copy, with a legacy M1 export fallback."""
        if self.baseline_path is not None or self.baseline_digest is not None:
            if self.baseline_path is None or not self.baseline_digest:
                raise WorkspaceError("Baseline requires its persisted content digest")
            files = read_files(self.baseline_path, exclude_generated=False)
            if manifest_digest(manifest(files)) != self.baseline_digest:
                raise WorkspaceError("Starting baseline was changed")
            return files
        if self.repository is None or self.base_commit is None:
            raise WorkspaceError("Delivery requires an explicit Git starting commit")
        with tempfile.TemporaryDirectory(prefix="doonex-baseline-") as temporary:
            exported = Workspace.create(
                self.repository, self.base_commit, Path(temporary) / "baseline"
            )
            return read_files(exported.path)

    @staticmethod
    def validate_repository(path: Path) -> Path:
        return validate_repository(path)

    @staticmethod
    def scan(path: Path) -> list[dict]:
        return manifest(read_files(path))

    @staticmethod
    def snapshot_digest(path: Path) -> str:
        return manifest_digest(Workspace.scan(path))

    @staticmethod
    def execution_digest(path: Path) -> str:
        """Bind all tool-visible files and directory names/modes for recovery."""
        directories = []
        files = read_files(path, exclude_generated=False, directories=directories)
        return hashlib.sha256(
            json.dumps(
                {"files": manifest(files), "directories": directories},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
