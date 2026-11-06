"""Private, immutable Skill snapshots; importing never executes or interprets code.

Only file bytes, exact file modes, directory names and declared metadata define a
version. Import timestamps do not. The caller must bind the returned report hash
in its durable product state before offering a version to a Run. SKILL.md is
returned as opaque UTF-8 text: frontmatter cannot install tools or grant rights.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .workspace import FileContent, WorkspaceError, _allowed_path, manifest, read_files

MAX_SKILL_ENTRIES = 200
MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SKILL_DEPTH = 8
MAX_METADATA_BYTES = 64 * 1024
MAX_REPORT_BYTES = MAX_METADATA_BYTES + 128 * 1024
_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_METADATA_FIELDS = frozenset(
    {"source", "license_status", "license_text", "commit", "scope", "permissions"}
)
_MANAGEMENT_NAMES = frozenset(
    {
        ".codex",
        ".claude",
        ".cursor",
        ".openhands",
        ".docker",
        ".kube",
        "access-token",
        "service.json",
        "model.json",
        "compose.env",
    }
)


class SkillError(ValueError):
    """A Skill cannot safely be imported or no longer matches its version."""


def _json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path(value: Path) -> Path:
    """Keep symlink checks meaningful; do not silently resolve any component."""
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise SkillError("Parent traversal is forbidden in a Skill path")
    return path.absolute()


def _no_links(path: Path) -> None:
    for ancestor in reversed((path, *path.parents)):
        try:
            information = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(information.st_mode):
            raise SkillError("Skill paths cannot contain symlinks")
        if ancestor != path and not stat.S_ISDIR(information.st_mode):
            raise SkillError("Skill parent path is not a directory")


def _protected_roots(extra=()) -> tuple[Path, ...]:
    user_home = Path.home()
    roots = [
        Path(os.environ.get("DOONEX_HOME", "~/Library/Application Support/DooNex")),
        *(
            user_home / name
            for name in (
                ".codex",
                ".config",
                ".ssh",
                ".aws",
                ".gnupg",
                ".docker",
                ".kube",
                ".claude",
                ".cursor",
                ".openhands",
            )
        ),
        *extra,
    ]
    model_path = os.environ.get("DOONEX_MODEL_CONFIG")
    if model_path:
        roots.append(Path(model_path).expanduser().parent)
    # Resolving protected paths also catches aliases to private management roots.
    return tuple(_path(root).resolve() for root in roots)


def _check_source(path: Path, protected_roots=()) -> None:
    _no_links(path)
    if not path.is_dir():
        raise SkillError("Skill import requires a local directory")
    for root in _protected_roots(protected_roots):
        if path.is_relative_to(root) or root.is_relative_to(path):
            raise SkillError("Skill source overlaps private management storage")


def _metadata(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - _METADATA_FIELDS:
        raise SkillError("Unsupported Skill metadata fields")
    result = {
        "source": value.get("source", "local"),
        "license_status": value.get("license_status", "unknown"),
        "license_text": value.get("license_text"),
        "commit": value.get("commit"),
        "scope": value.get("scope", []),
        "permissions": value.get("permissions", []),
    }
    source = result["source"]
    if not isinstance(source, str) or not source.strip() or len(source) > 4096:
        raise SkillError("Skill source must be a bounded provenance description")
    if any(ord(character) < 32 or ord(character) == 127 for character in source):
        raise SkillError("Skill source contains control characters")
    if "://" in source:
        try:
            parsed = urlsplit(source)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise SkillError(
                    "Skill source URLs cannot include credentials or queries"
                )
        except ValueError as error:
            raise SkillError("Invalid Skill source URL") from error
    license_status = result["license_status"]
    if not isinstance(license_status, str) or license_status not in {
        "unknown",
        "declared",
        "verified",
        "restricted",
    }:
        raise SkillError("Invalid Skill license status")
    license_text = result["license_text"]
    if license_text is not None and not isinstance(license_text, str):
        raise SkillError("Skill license text must be text or null")
    if result["license_status"] != "unknown" and not (license_text or "").strip():
        raise SkillError("A declared Skill license needs its text or identifier")
    commit = result["commit"]
    if commit is not None and (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit) is None
    ):
        raise SkillError("Skill commit must be a full Git object ID or null")
    if commit is not None:
        result["commit"] = commit.lower()
    for field in ("scope", "permissions"):
        values = result[field]
        if (
            not isinstance(values, (list, tuple))
            or len(values) > 100
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > 512
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in item
                )
                for item in values
            )
        ):
            raise SkillError(f"Skill {field} must be a bounded list of declarations")
        # These declarations are sets; their presentation order is not identity.
        result[field] = sorted(set(values))
    if len(_json(result)) > MAX_METADATA_BYTES:
        raise SkillError("Skill metadata size limit exceeded")
    return result


def _check_name(name: str) -> None:
    _allowed_path(name)
    if any(part.lower() in _MANAGEMENT_NAMES for part in name.split("/")):
        raise SkillError("Private management files cannot enter a Skill")
    # Avoid platform-dependent collisions and alternate path interpretation.
    if "\\" in name or ":" in name:
        raise SkillError("Unsupported Skill path characters")


def _directories(path: Path) -> tuple[str, ...]:
    """Retain empty directories, reject links, and keep every directory private."""
    directories: list[str] = []
    count = 0

    def walk(descriptor: int, prefix: str = "") -> None:
        nonlocal count
        for name in sorted(os.listdir(descriptor)):
            count += 1
            if count > MAX_SKILL_ENTRIES:
                raise SkillError("Skill entry limit exceeded")
            relative = prefix + name
            _check_name(relative)
            information = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(information.st_mode):
                if relative.count("/") > MAX_SKILL_DEPTH:
                    raise SkillError("Skill directory depth limit exceeded")
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    current = os.fstat(child)
                    if (current.st_dev, current.st_ino) != (
                        information.st_dev,
                        information.st_ino,
                    ):
                        raise SkillError("Skill directory changed during inspection")
                    directories.append(relative)
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif not stat.S_ISREG(information.st_mode):
                raise SkillError("Skill contains a symlink or special file")

    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(descriptor)
    finally:
        os.close(descriptor)
    return tuple(sorted(directories))


def _files(path: Path) -> dict[str, FileContent]:
    files = read_files(
        path,
        exclude_generated=False,
        max_files=MAX_SKILL_ENTRIES,
        max_file_bytes=MAX_SKILL_FILE_BYTES,
        max_total_bytes=MAX_SKILL_TOTAL_BYTES,
        max_depth=MAX_SKILL_DEPTH,
    )
    for name in files:
        _check_name(name)
    return files


def _instructions(files: dict[str, FileContent]) -> str:
    if "SKILL.md" not in files:
        raise SkillError("Skill requires a root SKILL.md")
    try:
        text = files["SKILL.md"].data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillError("SKILL.md must be UTF-8 text") from error
    if not text.strip() or "\x00" in text:
        raise SkillError("SKILL.md must contain nonempty plain text")
    return text


def _validate_content(
    files: dict[str, FileContent], directories: tuple[str, ...]
) -> None:
    if not isinstance(files, dict) or not isinstance(directories, tuple):
        raise SkillError("Invalid prepared Skill content")
    if tuple(sorted(set(directories))) != directories:
        raise SkillError("Invalid prepared Skill directories")
    if len(files) + len(directories) > MAX_SKILL_ENTRIES:
        raise SkillError("Skill entry limit exceeded")
    total = 0
    names = set()
    for name in (*directories, *files):
        if not isinstance(name, str):
            raise SkillError("Invalid Skill path")
        _check_name(name)
        if name.count("/") > MAX_SKILL_DEPTH:
            raise SkillError("Skill directory depth limit exceeded")
        key = unicodedata.normalize("NFC", name).casefold()
        if key in names:
            raise SkillError("Skill has a case or Unicode path collision")
        names.add(key)
        parts = name.split("/")
        if any(
            "/".join(parts[:index]) not in directories for index in range(1, len(parts))
        ):
            raise SkillError("Skill file parents do not match its directories")
    for content in files.values():
        if not isinstance(content, FileContent) or not isinstance(content.data, bytes):
            raise SkillError("Invalid prepared Skill file")
        if type(content.mode) is not int or content.mode < 0 or content.mode & ~0o777:
            raise SkillError("Invalid Skill file mode")
        total += len(content.data)
        if len(content.data) > MAX_SKILL_FILE_BYTES or total > MAX_SKILL_TOTAL_BYTES:
            raise SkillError("Skill file or total size limit exceeded")
    _instructions(files)


def _identity(metadata: dict, files: dict[str, FileContent], directories) -> dict:
    entries = manifest(files)
    return {
        "schema_version": _SCHEMA_VERSION,
        "metadata": metadata,
        "content_digest": _sha(
            _json({"files": entries, "directories": list(directories)})
        ),
        "files": entries,
        "directories": list(directories),
        "visibility": "private",
    }


@dataclass(frozen=True)
class PreparedSkill:
    version_id: str
    content_digest: str
    metadata: dict
    files: dict[str, FileContent]
    directories: tuple[str, ...]

    @property
    def manifest(self) -> list[dict]:
        return manifest(self.files)


@dataclass(frozen=True)
class SkillContext:
    version_id: str
    instructions: str
    path: Path
    assets: tuple[str, ...]
    metadata: dict


def prepare_import(path: Path, metadata: dict, *, protected_roots=()) -> PreparedSkill:
    """Read a bounded directory without executing scripts, Git or frontmatter."""
    try:
        path = _path(path)
        _check_source(path, protected_roots)
        normalized = _metadata(metadata)
        files = _files(path)
        directories = _directories(path)
        _validate_content(files, directories)
        # Read twice to detect ordinary concurrent changes to bytes or file modes.
        if _files(path) != files or _directories(path) != directories:
            raise SkillError("Skill source changed during import")
        identity = _identity(normalized, files, directories)
        return PreparedSkill(
            _sha(_json(identity)),
            identity["content_digest"],
            normalized,
            files,
            directories,
        )
    except (OSError, WorkspaceError) as error:
        raise SkillError("Cannot safely import Skill: " + str(error)) from error


class SkillRegistry:
    """Publish once under a private home; callers persist returned report bindings."""

    def __init__(self, home: Path):
        self.home = _path(home)
        _no_links(self.home)
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.home.chmod(0o700)
        self.root = self.home / "skills"
        self.versions = self.root / "versions"
        for path in (self.root, self.versions):
            _no_links(path)
            path.mkdir(mode=0o700, exist_ok=True)
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise SkillError("Skill registry directories must have mode 0700")

    def import_local(self, path: Path, metadata: dict) -> dict:
        return self.persist(
            prepare_import(path, metadata, protected_roots=(self.home,))
        )

    def _version_path(self, version_id: str) -> Path:
        if not isinstance(version_id, str) or _DIGEST.fullmatch(version_id) is None:
            raise SkillError("Invalid Skill version ID")
        path = self.versions / version_id
        _no_links(path)
        return path

    def persist(self, prepared: PreparedSkill) -> dict:
        """Publish atomically and idempotently; never repair a changed version."""
        if not isinstance(prepared, PreparedSkill):
            raise SkillError("A prepared Skill snapshot is required")
        _validate_content(prepared.files, prepared.directories)
        identity = _identity(
            _metadata(prepared.metadata), prepared.files, prepared.directories
        )
        if (
            _sha(_json(identity)) != prepared.version_id
            or identity["content_digest"] != prepared.content_digest
        ):
            raise SkillError("Prepared Skill no longer matches its version")
        _instructions(prepared.files)
        destination = self._version_path(prepared.version_id)
        lock_path = self.root / ".lock"
        _no_links(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if (
                not stat.S_ISREG(os.fstat(descriptor).st_mode)
                or stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600
            ):
                raise SkillError("Skill registry lock must be a private regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if destination.exists():
                return self._load(prepared.version_id, None)[0]
            with tempfile.TemporaryDirectory(
                prefix=".import-", dir=self.versions
            ) as temporary:
                staging = Path(temporary) / "version"
                staging.mkdir(mode=0o700)
                context = staging / "context"
                context.mkdir(mode=0o700)
                for name in prepared.directories:
                    _check_name(name)
                    (context / name).mkdir(mode=0o700, parents=True, exist_ok=True)
                for name, content in sorted(prepared.files.items()):
                    _check_name(name)
                    target = context / name
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with target.open("xb") as output:
                        os.fchmod(output.fileno(), content.mode)
                        output.write(content.data)
                        output.flush()
                        os.fsync(output.fileno())
                report = {
                    **identity,
                    "version_id": prepared.version_id,
                    "created_at": datetime.now(UTC).isoformat(),
                }
                report_bytes = _json(report)
                if len(report_bytes) > MAX_REPORT_BYTES:
                    raise SkillError("Skill report size limit exceeded")
                report_path = staging / "report.json"
                with report_path.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(report_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                staging.rename(destination)
            return self._load(prepared.version_id, _sha(report_bytes))[0]
        except (OSError, WorkspaceError) as error:
            raise SkillError("Cannot persist Skill snapshot: " + str(error)) from error
        finally:
            os.close(descriptor)

    def _load(
        self, version_id: str, expected_report_sha256: str | None
    ) -> tuple[dict, dict[str, FileContent]]:
        root = self._version_path(version_id)
        for path in (self.home, self.root, self.versions, root, root / "context"):
            _no_links(path)
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise SkillError("Skill storage directories are no longer private")
        if sorted(path.name for path in root.iterdir()) != ["context", "report.json"]:
            raise SkillError("Skill version contains unexpected entries")
        report_path = root / "report.json"
        descriptor = os.open(report_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            information = os.fstat(descriptor)
            if (
                not stat.S_ISREG(information.st_mode)
                or stat.S_IMODE(information.st_mode) != 0o600
            ):
                raise SkillError("Skill report must be a private regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                report_bytes = source.read(MAX_REPORT_BYTES + 1)
            if len(report_bytes) > MAX_REPORT_BYTES:
                raise SkillError("Skill report size limit exceeded")
        finally:
            os.close(descriptor)
        report_hash = _sha(report_bytes)
        if expected_report_sha256 is not None and report_hash != expected_report_sha256:
            raise SkillError("Skill report no longer matches its saved hash")
        report = json.loads(report_bytes)
        expected_fields = {
            "schema_version",
            "metadata",
            "content_digest",
            "files",
            "directories",
            "visibility",
            "version_id",
            "created_at",
        }
        if not isinstance(report, dict) or set(report) != expected_fields:
            raise SkillError("Invalid Skill report fields")
        files = _files(root / "context")
        directories = _directories(root / "context")
        for name in directories:
            if stat.S_IMODE((root / "context" / name).stat().st_mode) != 0o700:
                raise SkillError("Skill context directories are no longer private")
        _validate_content(files, directories)
        identity = _identity(_metadata(report["metadata"]), files, directories)
        if (
            report
            != {
                **identity,
                "version_id": version_id,
                "created_at": report["created_at"],
            }
            or _sha(_json(identity)) != version_id
        ):
            raise SkillError(
                "Skill files, modes or metadata no longer match its version"
            )
        if not isinstance(report["created_at"], str):
            raise SkillError("Invalid Skill import timestamp")
        _instructions(files)
        return {
            **report,
            "report_sha256": report_hash,
            "context_path": str(root / "context"),
        }, files

    def inspect(self, version_id: str, expected_report_sha256: str) -> dict:
        try:
            if (
                not isinstance(expected_report_sha256, str)
                or _DIGEST.fullmatch(expected_report_sha256) is None
            ):
                raise SkillError("Skill requires its saved report hash")
            record, _ = self._load(version_id, expected_report_sha256)
            return {"valid": True, "errors": [], **record}
        except (OSError, ValueError, KeyError, TypeError, WorkspaceError) as error:
            return {"valid": False, "errors": [str(error)]}

    def context(self, version_id: str, expected_report_sha256: str) -> SkillContext:
        """Return text and an asset root, never parse or execute Skill frontmatter."""
        status = self.inspect(version_id, expected_report_sha256)
        if not status["valid"]:
            raise SkillError("Skill context is invalid: " + "; ".join(status["errors"]))
        record, files = self._load(version_id, expected_report_sha256)
        return SkillContext(
            version_id,
            _instructions(files),
            Path(record["context_path"]),
            tuple(name for name in sorted(files) if name != "SKILL.md"),
            record["metadata"],
        )
