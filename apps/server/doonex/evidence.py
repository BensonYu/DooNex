"""Immutable code snapshots, independent checks and verifiable delivery artifacts."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

from .deadline import ExecutionDeadlineExceeded, check_deadline
from .sandbox import Sandbox, _now
from .skills import _no_links
from .tasksets import _overlaps, _snapshot
from .workspace import (
    GENERATED_NAMES,
    IO_CHUNK_BYTES,
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


MAX_ARTIFACT_BYTES = 200 * 1024 * 1024


def _raise_execution_control(error: BaseException) -> None:
    """Integrity inspection must not turn a stop/unknown into an invalid report."""
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if (
            isinstance(current, ExecutionDeadlineExceeded)
            or getattr(current, "cleanup_unknown", False)
            or getattr(current, "cleanup_evidence", None) is not None
        ):
            raise error
        current = current.__cause__ or current.__context__


def _check_cleanup_result(result: dict) -> None:
    """Keep a returned unknown cleanup visible before any later IO can expire."""
    if result.get("cleanup_unknown"):
        error = EvidenceError("Verification process cleanup could not be confirmed")
        error.cleanup_unknown = True
        error.cleanup_evidence = result.get("cleanup_evidence") or {
            "errors": ["verification_cleanup_unknown"],
            "local_process_unknown": True,
        }
        raise error


def _json(data: object, *, deadline: float | None = None) -> bytes:
    check_deadline(deadline)
    output = bytearray()
    encoder = json.JSONEncoder(sort_keys=True, indent=2, ensure_ascii=True)
    for chunk in encoder.iterencode(data):
        check_deadline(deadline)
        output.extend(chunk.encode())
    output.extend(b"\n")
    check_deadline(deadline)
    return bytes(output)


def _sha(data: bytes, *, deadline: float | None = None) -> str:
    check_deadline(deadline)
    digest = hashlib.sha256()
    for offset in range(0, len(data), IO_CHUNK_BYTES):
        check_deadline(deadline)
        digest.update(memoryview(data)[offset : offset + IO_CHUNK_BYTES])
    check_deadline(deadline)
    return digest.hexdigest()


def _read_artifact(
    root: Path, relative: str, *, deadline: float | None = None
) -> bytes:
    check_deadline(deadline)
    if relative.startswith("/") or any(
        part in ("", ".", "..") for part in relative.split("/")
    ):
        raise EvidenceError("Unsafe evidence artifact path")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            check_deadline(deadline)
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise EvidenceError("Evidence artifact is not a regular file")
            if before.st_size > MAX_ARTIFACT_BYTES:
                raise EvidenceError("Evidence artifact size limit exceeded")
            content = bytearray()
            while len(content) <= MAX_ARTIFACT_BYTES:
                check_deadline(deadline)
                chunk = os.read(
                    descriptor,
                    min(IO_CHUNK_BYTES, MAX_ARTIFACT_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            after = os.fstat(descriptor)
            if len(content) > MAX_ARTIFACT_BYTES or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise EvidenceError(
                    "Evidence artifact changed or exceeds its size limit"
                )
            if len(content) != before.st_size:
                raise EvidenceError("Evidence artifact changed during reading")
            check_deadline(deadline)
            return bytes(content)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _write_artifact(
    target: Path, content: bytes, *, deadline: float | None = None
) -> None:
    check_deadline(deadline)
    with target.open("wb") as output:
        for offset in range(0, len(content), IO_CHUNK_BYTES):
            check_deadline(deadline)
            output.write(memoryview(content)[offset : offset + IO_CHUNK_BYTES])
    check_deadline(deadline)


class _ArchiveReader(io.BytesIO):
    def __init__(self, content: bytes, deadline: float | None):
        super().__init__(content)
        self.deadline = deadline

    def read(self, size=-1):
        check_deadline(self.deadline)
        content = super().read(size)
        check_deadline(self.deadline)
        return content


class _ArchiveWriter:
    def __init__(self, output, deadline: float | None):
        self.output = output
        self.deadline = deadline

    def write(self, content):
        check_deadline(self.deadline)
        count = self.output.write(content)
        check_deadline(self.deadline)
        return count

    def tell(self):
        check_deadline(self.deadline)
        return self.output.tell()

    def flush(self):
        check_deadline(self.deadline)
        self.output.flush()
        check_deadline(self.deadline)


def _tar(
    files: dict[str, FileContent], target: Path, *, deadline: float | None = None
) -> None:
    check_deadline(deadline)
    with (
        target.open("xb") as output,
        gzip.GzipFile(
            fileobj=_ArchiveWriter(output, deadline), mode="wb", mtime=0, filename=""
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name, content in sorted(files.items()):
            check_deadline(deadline)
            member = tarfile.TarInfo(name)
            member.size = len(content.data)
            member.mode = content.mode & 0o777
            member.mtime = 0
            member.uid = member.gid = 0
            archive.addfile(member, _ArchiveReader(content.data, deadline))
    check_deadline(deadline)


def build_patch(
    before: dict[str, FileContent],
    after: dict[str, FileContent],
    *,
    deadline: float | None = None,
) -> bytes:
    """Generate the binary-capable patch from two already bounded file sets."""
    check_deadline(deadline)
    with tempfile.TemporaryDirectory(prefix="doonex-diff-") as temporary:
        temporary = Path(temporary)
        write_files(before, temporary / "a", deadline=deadline)
        write_files(after, temporary / "b", deadline=deadline)
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
            deadline=deadline,
            stdout_limit=MAX_ARTIFACT_BYTES,
        )
        if diff.returncode not in (0, 1):
            raise EvidenceError("Could not generate delivery diff")
        check_deadline(deadline)
        patch = diff.stdout
    check_deadline(deadline)
    return patch


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
    path: Path, expected: dict, *, deadline: float | None = None
) -> tuple[dict, dict[str, FileContent]]:
    try:
        check_deadline(deadline)
        path = Path(path).expanduser().absolute()
        _no_links(path)
        check_deadline(deadline)
        snapshot, files = _snapshot(path, "verifier", private=True, deadline=deadline)
        if snapshot["verifier_digest"] != expected["verifier_digest"]:
            raise EvidenceError("Verifier no longer matches the task set digest")
        check_deadline(deadline)
        return snapshot, files
    except (OSError, ValueError) as error:
        _raise_execution_control(error)
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
        deadline: float | None = None,
    ) -> dict:
        check_deadline(deadline)
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
                verifier_path, verification_context, deadline=deadline
            )
        check_deadline(deadline)
        root = Path(run_dir) / "evidence"
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        files = read_files(workspace.path, deadline=deadline)
        entries = manifest(files, deadline=deadline)
        digest = manifest_digest(entries, deadline=deadline)
        snapshot = root / "snapshot"
        write_files(files, snapshot, deadline=deadline)
        _write_artifact(
            root / "manifest.json", _json(entries, deadline=deadline), deadline=deadline
        )
        _tar(files, root / "snapshot.tar.gz", deadline=deadline)
        _write_artifact(
            root / "diff.patch",
            build_patch(
                workspace.base_files(deadline=deadline), files, deadline=deadline
            ),
            deadline=deadline,
        )
        checks = []
        verifier_errors = []
        artifacts = ["manifest.json", "snapshot.tar.gz", "diff.patch"]
        for index, command in enumerate(tuple(commands)):
            check_deadline(deadline)
            if should_stop():
                break
            with tempfile.TemporaryDirectory(prefix="doonex-check-") as temporary:
                # The trusted OS temp root may use /var -> /private/var on macOS.
                # Resolve our freshly created directory before adding either mount.
                temporary_root = Path(temporary).resolve(strict=True)
                check_path = temporary_root / "workspace"
                write_files(files, check_path, deadline=deadline)
                execution_options = (
                    {"deadline": deadline} if deadline is not None else {}
                )
                if verifier_info is None:
                    result = sandbox.run(
                        check_path,
                        command,
                        timeout,
                        run_id,
                        should_stop,
                        **execution_options,
                    )
                    _check_cleanup_result(result)
                else:
                    checker_path = temporary_root / "verifier"
                    write_files(verifier_files, checker_path, deadline=deadline)
                    for name in verifier_info["verifier_directories"]:
                        check_deadline(deadline)
                        (checker_path / name).mkdir(
                            mode=0o700, parents=True, exist_ok=True
                        )
                    if (
                        _verifier_snapshot(
                            checker_path, verification_context, deadline=deadline
                        )[0]
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
                        **execution_options,
                    )
                    _check_cleanup_result(result)
                    try:
                        _verifier_snapshot(
                            checker_path, verification_context, deadline=deadline
                        )
                    except EvidenceError as error:
                        _raise_execution_control(error)
                        verifier_errors.append(
                            "Verifier check copy changed: " + str(error)
                        )
            log_name = f"check-{index + 1}.log"
            _write_artifact(
                root / log_name, result.pop("output").encode("utf-8"), deadline=deadline
            )
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
        stale = Workspace.snapshot_digest(workspace.path, deadline=deadline) != digest
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": _now(),
            "base_commit": workspace.base_commit,
            "snapshot_digest": digest,
            "commands": list(commands),
            "acceptance_digest": _sha(
                _json(commands, deadline=deadline), deadline=deadline
            ),
            "image": sandbox.image,
            "checks": checks,
            "verified": verified and not stale,
            "stale": stale,
            "excluded_generated_names": sorted(GENERATED_NAMES),
            "artifacts": {
                name: _sha(
                    _read_artifact(root, name, deadline=deadline), deadline=deadline
                )
                for name in artifacts
            },
        }
        if verification_context is not None:
            integrity = {
                "valid": not verifier_errors,
                "errors": list(verifier_errors),
                "verifier_digest": verification_context["verifier_digest"],
            }
            try:
                if (
                    _verifier_snapshot(
                        verifier_path, verification_context, deadline=deadline
                    )[0]
                    != verifier_info
                ):
                    raise EvidenceError("Verifier tree changed during final checks")
            except (OSError, ValueError, EvidenceError) as error:
                _raise_execution_control(error)
                integrity["valid"] = False
                integrity["errors"].append(str(error))
            report.update(
                verification_context=verification_context, verifier_integrity=integrity
            )
            report["verified"] = report["verified"] and integrity["valid"]
        report_bytes = _json(report, deadline=deadline)
        _write_artifact(root / "report.json", report_bytes, deadline=deadline)
        report_sha = _sha(report_bytes, deadline=deadline)
        _write_artifact(
            root / "report.sha256",
            (report_sha + "\n").encode("ascii"),
            deadline=deadline,
        )
        check_deadline(deadline)
        return {**report, "report_sha256": report_sha}

    @staticmethod
    def inspect(
        workspace: Workspace,
        run_dir: Path,
        expected_report_sha256: str | None = None,
        *,
        verification_context: dict | None = None,
        deadline: float | None = None,
    ) -> dict:
        check_deadline(deadline)
        root = Path(run_dir) / "evidence"
        errors = []
        report = None
        stale = False
        try:
            report_bytes = _read_artifact(root, "report.json", deadline=deadline)
            stored_hash = (
                _read_artifact(root, "report.sha256", deadline=deadline)
                .decode("ascii")
                .strip()
            )
            if _sha(report_bytes, deadline=deadline) != stored_hash or (
                expected_report_sha256 is not None
                and stored_hash != expected_report_sha256
            ):
                raise EvidenceError("Delivery report was changed")
            report = json.loads(report_bytes)
            check_deadline(deadline)
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
            for check in report["checks"]:
                check_deadline(deadline)
                required.add(check["log"])
            if not required.issubset(report["artifacts"]):
                raise EvidenceError("Delivery artifact index is incomplete")
            for name, expected in report["artifacts"].items():
                check_deadline(deadline)
                if (
                    _sha(
                        _read_artifact(root, name, deadline=deadline), deadline=deadline
                    )
                    != expected
                ):
                    errors.append(f"Evidence artifact changed: {name}")
            entries = json.loads(
                _read_artifact(root, "manifest.json", deadline=deadline)
            )
            if manifest_digest(entries, deadline=deadline) != report["snapshot_digest"]:
                errors.append("Manifest does not match the code snapshot")
            if (
                Workspace.snapshot_digest(root / "snapshot", deadline=deadline)
                != report["snapshot_digest"]
            ):
                errors.append("Fixed code snapshot changed")
            stale = (
                Workspace.snapshot_digest(workspace.path, deadline=deadline)
                != report["snapshot_digest"]
            )
            if (
                _sha(_json(report["commands"], deadline=deadline), deadline=deadline)
                != report["acceptance_digest"]
            ):
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
            _raise_execution_control(error)
            errors.append(str(error))
        check_deadline(deadline)
        return {
            "valid": not errors and not stale,
            "stale": stale,
            "errors": errors,
            "report": report,
            "verified": not errors
            and not stale
            and bool(report and report["verified"]),
        }
