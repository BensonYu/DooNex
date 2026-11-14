"""Frozen, database-bound inputs for the read-only review role."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .deadline import check_deadline
from .evidence import (
    _json,
    _raise_execution_control,
    _read_artifact,
    _sha,
    _write_artifact,
    build_patch,
)
from .workspace import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    FileContent,
    Workspace,
    manifest,
    manifest_digest,
    read_files,
    write_files,
)

MAX_PATCH_BYTES = 200 * 1024 * 1024
HANDOFF_FIELDS = (
    "schema_version",
    "run_id",
    "base_commit",
    "snapshot_digest",
    "baseline_digest",
    "context_digest",
)


class ReviewError(ValueError):
    """The review input cannot be created or no longer matches its handoff."""


def _directories(path: Path, *, deadline: float | None = None) -> list[str]:
    """Include empty directories and reject links without following them."""
    found = []
    count = 0

    def walk(directory, prefix=""):
        nonlocal count
        check_deadline(deadline)
        with os.scandir(directory) as iterator:
            for entry in iterator:
                check_deadline(deadline)
                count += 1
                if count > 2 * MAX_FILES + 16:
                    raise ReviewError("Review context entry limit exceeded")
                relative = prefix + entry.name
                before = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (
                            before.st_dev,
                            before.st_ino,
                        ):
                            raise ReviewError(
                                "Review directory changed during inspection"
                            )
                        found.append(relative)
                        walk(child, relative + "/")
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(before.st_mode):
                    raise ReviewError("Review context contains a link or special file")

    check_deadline(deadline)
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(directory)
    finally:
        os.close(directory)
    check_deadline(deadline)
    return sorted(found)


def _expected_directories(
    files: dict[str, FileContent], *, deadline: float | None = None
) -> list[str]:
    check_deadline(deadline)
    directories = {"before", "after"}
    for name in files:
        check_deadline(deadline)
        parts = name.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    check_deadline(deadline)
    return sorted(directories)


def _context_digest(
    entries: list[dict], directories: list[str], *, deadline: float | None = None
) -> str:
    return _sha(
        _json({"files": entries, "directories": directories}, deadline=deadline),
        deadline=deadline,
    )


class Review:
    @staticmethod
    def prepare(
        workspace: Workspace,
        run_dir: Path,
        run_id: str,
        *,
        deadline: float | None = None,
    ) -> dict:
        """Atomically publish a deterministic handoff; never overwrite an old one."""
        check_deadline(deadline)
        if not isinstance(run_id, str) or not run_id or not workspace.base_commit:
            raise ReviewError("Review requires a run and an explicit starting commit")
        run_dir = Path(run_dir).absolute()
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = run_dir / "review"
        context = root / "context"
        before = workspace.base_files(deadline=deadline)
        after = read_files(workspace.path, deadline=deadline)
        patch = build_patch(before, after, deadline=deadline)
        if len(patch) > MAX_PATCH_BYTES:
            raise ReviewError("Review patch exceeds the bounded context size")
        handoff = {
            "schema_version": 1,
            "run_id": run_id,
            "base_commit": workspace.base_commit,
            "snapshot_digest": manifest_digest(
                manifest(after, deadline=deadline), deadline=deadline
            ),
            "baseline_digest": manifest_digest(
                manifest(before, deadline=deadline), deadline=deadline
            ),
        }
        files = {}
        for prefix, source in (("before", before), ("after", after)):
            for name, content in source.items():
                check_deadline(deadline)
                files[f"{prefix}/{name}"] = content
        files["diff.patch"] = FileContent(patch, 0o600)
        files["handoff.json"] = FileContent(
            _json(
                {**handoff, "diff_sha256": _sha(patch, deadline=deadline)},
                deadline=deadline,
            ),
            0o600,
        )
        entries = manifest(files, deadline=deadline)
        directories = _expected_directories(files, deadline=deadline)
        handoff["context_digest"] = _context_digest(
            entries, directories, deadline=deadline
        )
        report = {**handoff, "files": entries, "directories": directories}
        report_bytes = _json(report, deadline=deadline)
        result = {
            **handoff,
            "context_path": str(context),
            "report_sha256": _sha(report_bytes, deadline=deadline),
        }
        if root.exists() or root.is_symlink():
            status = Review.inspect(workspace, run_dir, result, deadline=deadline)
            if not status["valid"]:
                raise ReviewError(
                    "Existing review handoff is invalid: " + "; ".join(status["errors"])
                )
            return result
        with tempfile.TemporaryDirectory(
            prefix=".review-staging-", dir=run_dir
        ) as temporary:
            staging = Path(temporary) / "review"
            write_files(files, staging / "context", deadline=deadline)
            for name in ("before", "after"):
                check_deadline(deadline)
                (staging / "context" / name).mkdir(mode=0o700, exist_ok=True)
            report_path = staging / "report.json"
            _write_artifact(report_path, report_bytes, deadline=deadline)
            report_path.chmod(0o600)
            check_deadline(deadline)
            staging.rename(root)
        status = Review.inspect(workspace, run_dir, result, deadline=deadline)
        if not status["valid"]:
            raise ReviewError(
                "Published review handoff is invalid: " + "; ".join(status["errors"])
            )
        check_deadline(deadline)
        return result

    @staticmethod
    def inspect(
        workspace: Workspace,
        run_dir: Path,
        expected_handoff: dict,
        *,
        deadline: float | None = None,
    ) -> dict:
        """Validate the frozen input and the still-current executor snapshot."""
        check_deadline(deadline)
        errors = []
        report = None
        root = Path(run_dir).absolute() / "review"
        context = root / "context"
        try:
            if not isinstance(expected_handoff, dict) or not expected_handoff.get(
                "report_sha256"
            ):
                raise ReviewError("Review report requires its database-bound hash")
            if expected_handoff.get("context_path") != str(context):
                raise ReviewError("Review context path does not match this run")
            report_bytes = _read_artifact(root, "report.json", deadline=deadline)
            if (
                _sha(report_bytes, deadline=deadline)
                != expected_handoff["report_sha256"]
            ):
                raise ReviewError("Review report was changed")
            report = json.loads(report_bytes)
            check_deadline(deadline)
            if report["schema_version"] != 1 or any(
                report[field] != expected_handoff[field] for field in HANDOFF_FIELDS
            ):
                raise ReviewError("Review report does not match the saved handoff")
            if report["base_commit"] != workspace.base_commit:
                raise ReviewError("Review starting commit does not match the run")
            # Generated files are also included here: an added cache cannot evade
            # the exact review context manifest as it can a product snapshot.
            files = read_files(
                context,
                exclude_generated=False,
                max_files=2 * MAX_FILES + 16,
                max_file_bytes=MAX_PATCH_BYTES,
                max_total_bytes=2 * MAX_TOTAL_BYTES + MAX_PATCH_BYTES + 16_384,
                max_depth=101,
                deadline=deadline,
            )
            entries, directories = (
                manifest(files, deadline=deadline),
                _directories(context, deadline=deadline),
            )
            if entries != report["files"] or directories != report["directories"]:
                raise ReviewError(
                    "Review context files, permissions or directories changed"
                )
            if (
                _context_digest(entries, directories, deadline=deadline)
                != report["context_digest"]
            ):
                raise ReviewError("Review context digest changed")
            before, after = {}, {}
            for name, value in files.items():
                check_deadline(deadline)
                if name.startswith("before/"):
                    before[name.removeprefix("before/")] = value
                elif name.startswith("after/"):
                    after[name.removeprefix("after/")] = value
            if (
                manifest_digest(manifest(before, deadline=deadline), deadline=deadline)
                != report["baseline_digest"]
            ):
                raise ReviewError("Review baseline snapshot changed")
            if (
                manifest_digest(manifest(after, deadline=deadline), deadline=deadline)
                != report["snapshot_digest"]
            ):
                raise ReviewError("Reviewed code snapshot changed")
            if (
                manifest_digest(
                    manifest(
                        workspace.base_files(deadline=deadline), deadline=deadline
                    ),
                    deadline=deadline,
                )
                != report["baseline_digest"]
            ):
                raise ReviewError("Review baseline no longer matches the run")
            if (
                Workspace.snapshot_digest(workspace.path, deadline=deadline)
                != report["snapshot_digest"]
            ):
                raise ReviewError("Executor code changed after the review handoff")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            _raise_execution_control(error)
            errors.append(str(error))
        check_deadline(deadline)
        return {
            "valid": not errors,
            "errors": errors,
            "snapshot_digest": report.get("snapshot_digest")
            if isinstance(report, dict)
            else None,
        }
