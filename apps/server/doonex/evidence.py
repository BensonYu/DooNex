"""Immutable code snapshots, independent checks and verifiable delivery artifacts."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

from .sandbox import Sandbox, _now
from .workspace import (
    GENERATED_NAMES,
    FileContent,
    Workspace,
    WorkspaceError,
    _git,
    manifest,
    manifest_digest,
    read_files,
    write_files,
)


class EvidenceError(RuntimeError):
    pass


def _json(data: object) -> bytes:
    return (
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_artifact(root: Path, relative: str) -> bytes:
    if relative.startswith("/") or any(
        part in ("", ".", "..") for part in relative.split("/")
    ):
        raise EvidenceError("Unsafe evidence artifact path")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            import stat

            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise EvidenceError("Evidence artifact is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                return source.read(200 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _tar(files: dict[str, FileContent], target: Path) -> None:
    with (
        target.open("xb") as output,
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(content.data)
            member.mode = content.mode & 0o777
            member.mtime = 0
            member.uid = member.gid = 0
            archive.addfile(member, io.BytesIO(content.data))


class Evidence:
    @staticmethod
    def capture(
        workspace: Workspace,
        run_dir: Path,
        commands: list[str],
        sandbox: Sandbox,
        timeout: float,
        run_id: str,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> dict:
        if not isinstance(commands, list) or any(
            not isinstance(command, str) or not command for command in commands
        ):
            raise EvidenceError(
                "Acceptance commands must be a fixed list of nonempty strings"
            )
        if len(commands) > 50:
            raise EvidenceError("Too many acceptance commands")
        root = Path(run_dir) / "evidence"
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        files = read_files(workspace.path)
        entries = manifest(files)
        digest = manifest_digest(entries)
        snapshot = root / "snapshot"
        write_files(files, snapshot)
        (root / "manifest.json").write_bytes(_json(entries))
        _tar(files, root / "snapshot.tar.gz")
        with tempfile.TemporaryDirectory(prefix="doonex-diff-") as temporary:
            temporary = Path(temporary)
            if workspace.repository is None or workspace.base_commit is None:
                raise EvidenceError("Delivery requires an explicit Git starting commit")
            Workspace.create(
                workspace.repository, workspace.base_commit, temporary / "a"
            )
            write_files(files, temporary / "b")
            diff = _git(
                temporary,
                "diff",
                "--no-index",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--src-prefix=",
                "--dst-prefix=",
                "a",
                "b",
                check=False,
            )
            if diff.returncode not in (0, 1):
                raise EvidenceError("Could not generate delivery diff")
            (root / "diff.patch").write_bytes(diff.stdout)
        checks = []
        artifacts = ["manifest.json", "snapshot.tar.gz", "diff.patch"]
        for index, command in enumerate(tuple(commands)):
            if should_stop():
                break
            with tempfile.TemporaryDirectory(prefix="doonex-check-") as temporary:
                check_path = Path(temporary) / "workspace"
                write_files(files, check_path)
                result = sandbox.run(check_path, command, timeout, run_id, should_stop)
            log_name = f"check-{index + 1}.log"
            (root / log_name).write_text(result.pop("output"), encoding="utf-8")
            artifacts.append(log_name)
            checks.append({"command": command, "log": log_name, **result})
            if (
                result.get("timed_out")
                or result.get("cancelled")
                or result.get("cleanup_error")
            ):
                break
        verified = (
            bool(commands)
            and len(checks) == len(commands)
            and all(
                item["exit_code"] == 0
                and not item.get("timed_out")
                and not item.get("cancelled")
                and not item.get("cleanup_error")
                for item in checks
            )
        )
        # An unexpected concurrent writer invalidates the result, even if checks passed.
        stale = Workspace.snapshot_digest(workspace.path) != digest
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": _now(),
            "base_commit": workspace.base_commit,
            "snapshot_digest": digest,
            "commands": list(commands),
            "acceptance_digest": _sha(_json(commands)),
            "image": sandbox.image,
            "checks": checks,
            "verified": verified and not stale,
            "stale": stale,
            "excluded_generated_names": sorted(GENERATED_NAMES),
            "artifacts": {name: _sha(_read_artifact(root, name)) for name in artifacts},
        }
        report_bytes = _json(report)
        (root / "report.json").write_bytes(report_bytes)
        report_sha = _sha(report_bytes)
        (root / "report.sha256").write_text(report_sha + "\n", encoding="ascii")
        return {**report, "report_sha256": report_sha}

    @staticmethod
    def inspect(
        workspace: Workspace, run_dir: Path, expected_report_sha256: str | None = None
    ) -> dict:
        root = Path(run_dir) / "evidence"
        errors = []
        report = None
        stale = False
        try:
            report_bytes = _read_artifact(root, "report.json")
            stored_hash = _read_artifact(root, "report.sha256").decode("ascii").strip()
            if _sha(report_bytes) != stored_hash or (
                expected_report_sha256 is not None
                and stored_hash != expected_report_sha256
            ):
                raise EvidenceError("Delivery report was changed")
            report = json.loads(report_bytes)
            required = {"manifest.json", "diff.patch", "snapshot.tar.gz"}
            required.update(check["log"] for check in report["checks"])
            if not required.issubset(report["artifacts"]):
                raise EvidenceError("Delivery artifact index is incomplete")
            for name, expected in report["artifacts"].items():
                if _sha(_read_artifact(root, name)) != expected:
                    errors.append(f"Evidence artifact changed: {name}")
            entries = json.loads(_read_artifact(root, "manifest.json"))
            if manifest_digest(entries) != report["snapshot_digest"]:
                errors.append("Manifest does not match the code snapshot")
            if (
                Workspace.snapshot_digest(root / "snapshot")
                != report["snapshot_digest"]
            ):
                errors.append("Fixed code snapshot changed")
            stale = (
                Workspace.snapshot_digest(workspace.path) != report["snapshot_digest"]
            )
            if _sha(_json(report["commands"])) != report["acceptance_digest"]:
                errors.append("Acceptance commands changed")
            if report["verified"] and (
                not report["commands"]
                or len(report["checks"]) != len(report["commands"])
                or any(
                    check["command"] != report["commands"][index]
                    or check["exit_code"] != 0
                    or check.get("timed_out")
                    or check.get("cancelled")
                    or check.get("cleanup_error")
                    for index, check in enumerate(report["checks"])
                )
            ):
                errors.append("Verified status contradicts check results")
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            WorkspaceError,
            EvidenceError,
        ) as error:
            errors.append(str(error))
        return {
            "valid": not errors and not stale,
            "stale": stale,
            "errors": errors,
            "report": report,
            "verified": not errors
            and not stale
            and bool(report and report["verified"]),
        }
