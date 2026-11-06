"""Frozen private Arena task sets with separate Git source and verifier inputs.

Import resolves Git refs and exports objects, without executing task or verifier
code. Hidden verifier bytes stay outside each source tree. Runtime consumers must
only send public_metadata to an executor and mount verifier_path after it stops.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .skills import (
    MAX_SKILL_ENTRIES,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_TOTAL_BYTES,
    _json,
    _no_links,
    _sha,
)
from .workspace import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    FileContent,
    Workspace,
    WorkspaceError,
    _allowed_path,
    manifest,
    read_files,
    validate_repository,
    write_files,
)

MAX_CONFIG_BYTES = 256 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_SUITE_BYTES = 256 * 1024 * 1024
MAX_SUITE_ENTRIES = 20_000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TASK_FIELDS = {
    "id",
    "title",
    "prompt",
    "base_commit",
    "visible_checks",
    "verifier_command",
    "critical",
}
_SNAPSHOT_FIELDS = {
    f"{kind}_{suffix}"
    for kind in ("source", "verifier")
    for suffix in ("digest", "files", "directories")
}


class TaskSetError(ValueError):
    """The task set cannot be imported or no longer matches its saved evidence."""


def _text(value, name: str, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TaskSetError(f"Invalid task set {name}")
    if any(
        ord(char) == 0
        or ord(char) == 127
        or (ord(char) < 32 and (not multiline or char not in "\n\r\t"))
        for char in value
    ):
        raise TaskSetError(f"Control character in task set {name}")
    return value


def _top(value: dict) -> dict:
    name = _text(value.get("name"), "name", 256)
    category = _text(value.get("category"), "category", 256)
    split = value.get("split")
    if split not in ("tuning", "heldout"):
        raise TaskSetError("Task set split must be tuning or heldout")
    rate = value.get("minimum_pass_rate", 0.8)
    if (
        type(rate) not in (int, float)
        or not math.isfinite(rate)
        or not 0.8 <= rate <= 1
    ):
        raise TaskSetError("Task set minimum_pass_rate must be between 0.8 and 1")
    return {
        "name": name,
        "category": category,
        "split": split,
        "minimum_pass_rate": float(rate),
    }


def _description(value: dict, *, imported: bool) -> dict:
    if not isinstance(value, dict):
        raise TaskSetError("Each task must be an object")
    task_id = value.get("id")
    if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
        raise TaskSetError("Invalid task ID")
    checks = value.get("visible_checks", [])
    if not isinstance(checks, list) or len(checks) > 20:
        raise TaskSetError("visible_checks must be a bounded list")
    checks = [_text(check, "visible check", 4096, multiline=True) for check in checks]
    critical = value.get("critical", False)
    if type(critical) is not bool:
        raise TaskSetError("Task critical must be a boolean")
    result = {
        "id": task_id,
        "title": _text(value.get("title"), "title", 256),
        "prompt": _text(value.get("prompt"), "prompt", 32768, multiline=True),
        "visible_checks": checks,
        "verifier_command": _text(
            value.get("verifier_command"), "verifier command", 4096, multiline=True
        ),
        "critical": critical,
    }
    if imported:
        commit = value.get("base_commit")
        if (
            not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
        ):
            raise TaskSetError("Task requires its resolved Git commit")
        result["base_commit"] = commit
    return result


def _local_path(value, base: Path | None = None) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise TaskSetError("Task set requires a local directory path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    # Check the original spelling before normalizing parent components.
    _no_links(path)
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise TaskSetError("Task set path must be a directory")
    return path


def _overlaps(path: Path, root: Path) -> bool:
    if path.is_relative_to(root) or root.is_relative_to(path):
        return True
    # APFS commonly ignores case, while Path comparisons do not. Also compare
    # existing ancestors by inode so an alternate spelling cannot bypass a root.
    for candidate, ancestor in ((path, root), (root, path)):
        if ancestor.exists():
            for entry in (candidate, *candidate.parents):
                if entry.exists() and entry.samefile(ancestor):
                    return True
    return False


def _check_location(path: Path, home: Path, *, project: bool = False) -> None:
    user_home = Path.home()
    protected = [
        home,
        Path(os.environ.get("DOONEX_HOME", "~/Library/Application Support/DooNex"))
        .expanduser()
        .resolve(),
    ]
    protected.extend(
        user_home / name
        for name in (
            ".ssh",
            ".aws",
            ".gnupg",
            ".config",
            ".docker",
            ".kube",
            ".claude",
            ".cursor",
            ".openhands",
        )
    )
    model_path = os.environ.get("DOONEX_MODEL_CONFIG")
    if model_path:
        protected.append(Path(model_path).expanduser().resolve().parent)
    codex = (user_home / ".codex").resolve()
    if not (project and path.is_relative_to(codex / "worktrees")):
        protected.append(codex)
    for root in protected:
        root = root.resolve()
        if _overlaps(path, root):
            raise TaskSetError("Task set path overlaps private management storage")


def _tree(
    path: Path, *, limit: int, depth: int, private: bool
) -> tuple[list[str], list[str]]:
    directories, files = [], []
    count = 0

    def walk(descriptor: int, prefix=""):
        nonlocal count
        if private and stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise TaskSetError("Task set directories must remain private (0700)")
        for name in sorted(os.listdir(descriptor)):
            count += 1
            if count > limit:
                raise TaskSetError("Task set entry limit exceeded")
            relative = prefix + name
            _allowed_path(relative)
            if name.lower() in {
                ".codex",
                ".config",
                ".claude",
                ".cursor",
                ".openhands",
                ".docker",
                ".kube",
            }:
                raise TaskSetError(
                    "Private management directories cannot enter a task set"
                )
            if relative.count("/") > depth or "\\" in relative or ":" in relative:
                raise TaskSetError("Unsupported task set path or directory depth")
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise TaskSetError("Task set directory changed while reading")
                    directories.append(relative)
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                files.append(relative)
            else:
                raise TaskSetError("Task set contains a symlink or special file")

    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(descriptor)
    finally:
        os.close(descriptor)
    return sorted(directories), sorted(files)


def _snapshot(
    path: Path, kind: str, *, private=False
) -> tuple[dict, dict[str, FileContent]]:
    limit = MAX_FILES if kind == "source" else MAX_SKILL_ENTRIES
    max_file = MAX_FILE_BYTES if kind == "source" else MAX_SKILL_FILE_BYTES
    max_total = MAX_TOTAL_BYTES if kind == "source" else MAX_SKILL_TOTAL_BYTES
    depth = 100 if kind == "source" else 8
    files = read_files(
        path,
        exclude_generated=False,
        max_files=limit,
        max_file_bytes=max_file,
        max_total_bytes=max_total,
        max_depth=depth,
    )
    directories, names = _tree(path, limit=limit, depth=depth, private=private)
    if names != sorted(files):
        raise TaskSetError("Task set files changed while reading")
    entries = manifest(files)
    data = {"files": entries, "directories": directories}
    return {
        f"{kind}_digest": _sha(_json(data)),
        f"{kind}_files": entries,
        f"{kind}_directories": directories,
    }, files


def _read_json(
    path: Path, maximum: int, *, private: bool = False
) -> tuple[dict, bytes]:
    _no_links(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o7000:
            raise TaskSetError("Task set JSON must be a regular file")
        if private and stat.S_IMODE(info.st_mode) != 0o600:
            raise TaskSetError("Task set report must remain private (0600)")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(maximum + 1)
        after = os.fstat(descriptor)
        if len(content) > maximum or (
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise TaskSetError("Task set JSON changed or exceeds its size limit")
    finally:
        os.close(descriptor)
    data = json.loads(content)
    if not isinstance(data, dict):
        raise TaskSetError("Task set JSON must be an object")
    return data, content


def _identity(top: dict, tasks: list[dict]) -> dict:
    return {"schema_version": 1, **top, "tasks": tasks, "visibility": "private"}


@dataclass(frozen=True)
class TaskContext:
    taskset_id: str
    task_id: str
    source_path: Path
    verifier_path: Path
    metadata: dict

    @property
    def public_metadata(self) -> dict:
        return {
            key: self.metadata[key]
            for key in (
                "id",
                "title",
                "prompt",
                "base_commit",
                "visible_checks",
                "critical",
                "source_digest",
            )
        }


class TaskSetRegistry:
    def __init__(self, home: Path):
        home = Path(home).expanduser().absolute()
        _no_links(home)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        self.home, self.root = home, home / "tasksets"
        self.versions = self.root / "versions"
        for path in (self.root, self.versions):
            _no_links(path)
            path.mkdir(mode=0o700, exist_ok=True)
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise TaskSetError(
                    "Task set registry directories must be private (0700)"
                )

    def _version_path(self, taskset_id: str) -> Path:
        if not isinstance(taskset_id, str) or _DIGEST.fullmatch(taskset_id) is None:
            raise TaskSetError("Invalid task set ID")
        path = self.versions / taskset_id
        _no_links(path)
        return path

    def import_local(self, path: Path) -> dict:
        try:
            source = _local_path(path)
            _check_location(source, self.home, project=True)
            config, _ = _read_json(source / "taskset.json", MAX_CONFIG_BYTES)
            if set(config) - {
                "name",
                "category",
                "split",
                "minimum_pass_rate",
                "tasks",
            }:
                raise TaskSetError("Unsupported task set fields")
            top = _top(config)
            inputs = config.get("tasks")
            if not isinstance(inputs, list) or not 5 <= len(inputs) <= 10:
                raise TaskSetError("Task set requires 5 to 10 tasks")
            planned, used_ids = [], set()
            for task in inputs:
                description = _description(task, imported=False)
                if set(task) - (
                    _TASK_FIELDS - {"base_commit"}
                    | {"repo_path", "base_ref", "verifier_path"}
                ):
                    raise TaskSetError("Unsupported task fields")
                if description["id"].casefold() in used_ids:
                    raise TaskSetError("Task IDs must be unique")
                used_ids.add(description["id"].casefold())
                repository = _local_path(task.get("repo_path"), source)
                _check_location(repository, self.home, project=True)
                repository = validate_repository(repository)
                verifier = _local_path(task.get("verifier_path"), source)
                _check_location(verifier, self.home)
                ref = _text(task.get("base_ref", "HEAD"), "base_ref", 256)
                planned.append((description, repository, ref, verifier))
            for _, _, _, verifier in planned:
                if any(_overlaps(verifier, repo) for _, repo, _, _ in planned):
                    raise TaskSetError(
                        "Verifier directories must be separate from every source repository"
                    )
            with tempfile.TemporaryDirectory(
                prefix=".import-", dir=self.versions
            ) as temporary:
                staging = Path(temporary) / "version"
                context = staging / "context"
                context.mkdir(parents=True, mode=0o700)
                staging.chmod(0o700)
                tasks, provenance = [], {}
                total = 0
                entries = 0
                for description, repository, ref, verifier in sorted(
                    planned, key=lambda value: value[0]["id"]
                ):
                    task_root = context / "tasks" / description["id"]
                    task_root.mkdir(parents=True, mode=0o700)
                    (context / "tasks").chmod(0o700)
                    exported = Workspace.create(repository, ref, task_root / "source")
                    source_info, source_files = _snapshot(
                        exported.path, "source", private=True
                    )
                    verifier_info, verifier_files = _snapshot(verifier, "verifier")
                    if _snapshot(verifier, "verifier")[0] != verifier_info:
                        raise TaskSetError("Verifier changed during import")
                    write_files(verifier_files, task_root / "verifier")
                    for name in verifier_info["verifier_directories"]:
                        (task_root / "verifier" / name).mkdir(
                            parents=True, exist_ok=True, mode=0o700
                        )
                    task = {
                        **description,
                        "base_commit": exported.base_commit,
                        **source_info,
                        **verifier_info,
                    }
                    tasks.append(task)
                    provenance[description["id"]] = {
                        "repo_path": str(repository),
                        "requested_ref": ref,
                    }
                    total += sum(
                        len(content.data)
                        for content in (
                            *source_files.values(),
                            *verifier_files.values(),
                        )
                    )
                    entries += (
                        sum(
                            len(task[f"{kind}_{suffix}"])
                            for kind in ("source", "verifier")
                            for suffix in ("files", "directories")
                        )
                        + 3
                    )
                    if total > MAX_SUITE_BYTES or entries > MAX_SUITE_ENTRIES:
                        raise TaskSetError("Task set aggregate resource limit exceeded")
                identity = _identity(top, tasks)
                taskset_id = _sha(_json(identity))
                report = {
                    **identity,
                    "taskset_id": taskset_id,
                    "provenance": provenance,
                    "created_at": datetime.now(UTC).isoformat(),
                }
                report_bytes = _json(report)
                if len(report_bytes) > MAX_REPORT_BYTES:
                    raise TaskSetError("Task set report size limit exceeded")
                report_path = staging / "report.json"
                with report_path.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(report_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                lock_path = self.root / ".lock"
                _no_links(lock_path)
                lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    if (
                        not stat.S_ISREG(os.fstat(lock).st_mode)
                        or stat.S_IMODE(os.fstat(lock).st_mode) != 0o600
                    ):
                        raise TaskSetError(
                            "Task set lock must be a private regular file"
                        )
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    destination = self._version_path(taskset_id)
                    if destination.exists():
                        return self._load(taskset_id, None)[0]
                    staging.rename(destination)
                    return self._load(taskset_id, _sha(report_bytes))[0]
                finally:
                    os.close(lock)
        except (OSError, WorkspaceError, ValueError) as error:
            if isinstance(error, TaskSetError):
                raise
            raise TaskSetError(
                "Cannot safely import task set: " + str(error)
            ) from error

    def _load(
        self, taskset_id: str, expected_report_sha256: str | None
    ) -> tuple[dict, Path]:
        root = self._version_path(taskset_id)
        for path in (self.home, self.root, self.versions, root, root / "context"):
            _no_links(path)
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise TaskSetError(
                    "Task set storage directories must remain private (0700)"
                )
        if sorted(path.name for path in root.iterdir()) != ["context", "report.json"]:
            raise TaskSetError("Task set version contains unexpected entries")
        report, report_bytes = _read_json(
            root / "report.json", MAX_REPORT_BYTES, private=True
        )
        report_hash = _sha(report_bytes)
        if expected_report_sha256 is not None and expected_report_sha256 != report_hash:
            raise TaskSetError("Task set report no longer matches its saved hash")
        expected_fields = {
            "schema_version",
            "name",
            "category",
            "split",
            "minimum_pass_rate",
            "tasks",
            "visibility",
            "taskset_id",
            "provenance",
            "created_at",
        }
        if (
            set(report) != expected_fields
            or not isinstance(report["tasks"], list)
            or not 5 <= len(report["tasks"]) <= 10
        ):
            raise TaskSetError("Invalid task set report fields")
        tasks, names, directories = [], [], ["tasks"]
        used_ids = set()
        for saved in report["tasks"]:
            description = _description(saved, imported=True)
            if (
                set(saved) != _TASK_FIELDS | _SNAPSHOT_FIELDS
                or description["id"].casefold() in used_ids
            ):
                raise TaskSetError("Invalid frozen task fields or duplicate IDs")
            used_ids.add(description["id"].casefold())
            task_root = root / "context" / "tasks" / description["id"]
            task = dict(description)
            directories.append(f"tasks/{description['id']}")
            for kind in ("source", "verifier"):
                info, _ = _snapshot(task_root / kind, kind, private=True)
                task.update(info)
                prefix = f"tasks/{description['id']}/{kind}"
                directories.extend(
                    [
                        prefix,
                        *(f"{prefix}/{name}" for name in info[f"{kind}_directories"]),
                    ]
                )
                names.extend(
                    f"{prefix}/{entry['path']}" for entry in info[f"{kind}_files"]
                )
            tasks.append(task)
        actual_dirs, actual_files = _tree(
            root / "context", limit=MAX_SUITE_ENTRIES + 1, depth=104, private=True
        )
        if actual_dirs != sorted(directories) or actual_files != sorted(names):
            raise TaskSetError(
                "Task set context contains unexpected or missing entries"
            )
        identity = _identity(_top(report), tasks)
        if (
            report
            != {
                **identity,
                "taskset_id": taskset_id,
                "provenance": report["provenance"],
                "created_at": report["created_at"],
            }
            or _sha(_json(identity)) != taskset_id
        ):
            raise TaskSetError("Task set source, verifier, modes or metadata changed")
        provenance = report["provenance"]
        if not isinstance(provenance, dict) or set(provenance) != {
            task["id"] for task in tasks
        }:
            raise TaskSetError("Invalid task set provenance")
        for origin in provenance.values():
            if (
                not isinstance(origin, dict)
                or set(origin) != {"repo_path", "requested_ref"}
                or not isinstance(origin["repo_path"], str)
                or not Path(origin["repo_path"]).is_absolute()
            ):
                raise TaskSetError("Invalid task set repository provenance")
            _text(origin["requested_ref"], "provenance ref", 256)
        if not isinstance(report["created_at"], str):
            raise TaskSetError("Invalid task set import timestamp")
        return {
            **report,
            "report_sha256": report_hash,
            "context_path": str(root / "context"),
        }, root / "context"

    def inspect(self, taskset_id: str, expected_report_sha256: str) -> dict:
        try:
            if (
                not isinstance(expected_report_sha256, str)
                or _DIGEST.fullmatch(expected_report_sha256) is None
            ):
                raise TaskSetError("Task set requires its saved report hash")
            record, _ = self._load(taskset_id, expected_report_sha256)
            return {"valid": True, "errors": [], **record}
        except (OSError, ValueError, TypeError, KeyError) as error:
            return {"valid": False, "errors": [str(error)]}

    def context(
        self, taskset_id: str, expected_report_sha256: str, task_id: str
    ) -> TaskContext:
        status = self.inspect(taskset_id, expected_report_sha256)
        if not status["valid"]:
            raise TaskSetError(
                "Task set context is invalid: " + "; ".join(status["errors"])
            )
        task = next((task for task in status["tasks"] if task["id"] == task_id), None)
        if task is None:
            raise TaskSetError("Unknown task ID in this task set")
        root = Path(status["context_path"]) / "tasks" / task_id
        return TaskContext(
            taskset_id, task_id, root / "source", root / "verifier", task
        )
