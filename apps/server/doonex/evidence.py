"""Immutable code snapshots, independent checks and verifiable delivery artifacts."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

from .sandbox import Sandbox, _now
from .skills import _no_links
from .tasksets import _overlaps, _snapshot
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


def build_patch(before: dict[str, FileContent], after: dict[str, FileContent]) -> bytes:
    """Generate the binary-capable patch from two already bounded file sets."""
    with tempfile.TemporaryDirectory(prefix="doonex-diff-") as temporary:
        temporary = Path(temporary)
        write_files(before, temporary / "a")
        write_files(after, temporary / "b")
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
        return diff.stdout


def _verification_context(value: dict) -> dict:
    fields = {"taskset_id", "task_id", "report_sha256", "verifier_digest"}
    if not isinstance(value, dict) or set(value) != fields:
        raise EvidenceError("Verifier requires a complete task set context")
    for field in fields - {"task_id"}:
        if (
            not isinstance(value[field], str)
            or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None
        ):
            raise EvidenceError("Verifier context contains an invalid digest")
    if (
        not isinstance(value["task_id"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value["task_id"]) is None
    ):
        raise EvidenceError("Verifier context contains an invalid task ID")
    return dict(value)


def _verifier_snapshot(
    path: Path, expected: dict
) -> tuple[dict, dict[str, FileContent]]:
    try:
        path = Path(path).expanduser().absolute()
        _no_links(path)
        snapshot, files = _snapshot(path, "verifier", private=True)
        if snapshot["verifier_digest"] != expected["verifier_digest"]:
            raise EvidenceError("Verifier no longer matches the task set digest")
        return snapshot, files
    except (OSError, ValueError) as error:
        raise EvidenceError("Unsafe verifier input: " + str(error)) from error


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
        *,
        verifier_path: Path | None = None,
        verification_context: dict | None = None,
    ) -> dict:
        if not isinstance(commands, list) or any(
            not isinstance(command, str) or not command for command in commands
        ):
            raise EvidenceError(
                "Acceptance commands must be a fixed list of nonempty strings"
            )
        if len(commands) > 50:
            raise EvidenceError("Too many acceptance commands")
        commands = list(commands)
        if (verifier_path is None) != (verification_context is None):
            raise EvidenceError(
                "Verifier path and its trusted context must be supplied together"
            )
        verifier_info, verifier_files = None, None
        if verifier_path is not None:
            verification_context = _verification_context(verification_context)
            verifier_path = Path(verifier_path).expanduser().absolute()
            if _overlaps(workspace.path, verifier_path):
                raise EvidenceError(
                    "Verifier must be separate from the executor workspace"
                )
            verifier_info, verifier_files = _verifier_snapshot(
                verifier_path, verification_context
            )
        root = Path(run_dir) / "evidence"
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        files = read_files(workspace.path)
        entries = manifest(files)
        digest = manifest_digest(entries)
        snapshot = root / "snapshot"
        write_files(files, snapshot)
        (root / "manifest.json").write_bytes(_json(entries))
        _tar(files, root / "snapshot.tar.gz")
        (root / "diff.patch").write_bytes(build_patch(workspace.base_files(), files))
        checks = []
        verifier_errors = []
        artifacts = ["manifest.json", "snapshot.tar.gz", "diff.patch"]
        for index, command in enumerate(tuple(commands)):
            if should_stop():
                break
            with tempfile.TemporaryDirectory(prefix="doonex-check-") as temporary:
                # The trusted OS temp root may use /var -> /private/var on macOS.
                # Resolve our freshly created directory before adding either mount.
                temporary_root = Path(temporary).resolve(strict=True)
                check_path = temporary_root / "workspace"
                write_files(files, check_path)
                if verifier_info is None:
                    result = sandbox.run(
                        check_path, command, timeout, run_id, should_stop
                    )
                else:
                    checker_path = temporary_root / "verifier"
                    write_files(verifier_files, checker_path)
                    for name in verifier_info["verifier_directories"]:
                        (checker_path / name).mkdir(
                            mode=0o700, parents=True, exist_ok=True
                        )
                    if (
                        _verifier_snapshot(checker_path, verification_context)[0]
                        != verifier_info
                    ):
                        raise EvidenceError(
                            "Copied verifier does not match the task set"
                        )
                    result = sandbox.run(
                        check_path,
                        command,
                        timeout,
                        run_id,
                        should_stop,
                        verifier_path=checker_path,
                    )
                    try:
                        _verifier_snapshot(checker_path, verification_context)
                    except EvidenceError as error:
                        verifier_errors.append(
                            "Verifier check copy changed: " + str(error)
                        )
            log_name = f"check-{index + 1}.log"
            (root / log_name).write_text(result.pop("output"), encoding="utf-8")
            artifacts.append(log_name)
            checks.append({"command": command, "log": log_name, **result})
            if (
                result.get("timed_out")
                or result.get("cancelled")
                or result.get("cleanup_error")
                or verifier_errors
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
        if verification_context is not None:
            integrity = {
                "valid": not verifier_errors,
                "errors": list(verifier_errors),
                "verifier_digest": verification_context["verifier_digest"],
            }
            try:
                if (
                    _verifier_snapshot(verifier_path, verification_context)[0]
                    != verifier_info
                ):
                    raise EvidenceError("Verifier tree changed during final checks")
            except (OSError, ValueError, EvidenceError) as error:
                integrity["valid"] = False
                integrity["errors"].append(str(error))
            report.update(
                verification_context=verification_context, verifier_integrity=integrity
            )
            report["verified"] = report["verified"] and integrity["valid"]
        report_bytes = _json(report)
        (root / "report.json").write_bytes(report_bytes)
        report_sha = _sha(report_bytes)
        (root / "report.sha256").write_text(report_sha + "\n", encoding="ascii")
        return {**report, "report_sha256": report_sha}

    @staticmethod
    def inspect(
        workspace: Workspace,
        run_dir: Path,
        expected_report_sha256: str | None = None,
        *,
        verification_context: dict | None = None,
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
            if verification_context is not None and report.get(
                "verification_context"
            ) != _verification_context(verification_context):
                errors.append("Delivery report belongs to a different task set context")
            if "verification_context" in report:
                saved_context = _verification_context(report["verification_context"])
                integrity = report.get("verifier_integrity")
                if not isinstance(integrity, dict) or integrity != {
                    "valid": True,
                    "errors": [],
                    "verifier_digest": saved_context["verifier_digest"],
                }:
                    errors.append("Verifier input integrity failed during final checks")
            elif "verifier_integrity" in report:
                errors.append("Verifier integrity lacks its task set context")
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
