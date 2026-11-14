import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from doonex import evidence, review
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.workspace import FileContent, Workspace, WorkspaceProcessUnknown
from sandbox_test_support import sandbox_context as sandbox_context_fixture

sandbox_context = sandbox_context_fixture


@pytest.fixture
def delivery(tmp_path, sandbox_context):
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "-q")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Deadline fixture")
    (repository / "main.py").write_text("answer = 1\n")
    git("add", ".")
    git("commit", "-qm", "synthetic baseline")
    run_dir = tmp_path / "run"
    workspace = Workspace.create(
        repository,
        "HEAD",
        run_dir / "workspace",
        baseline_path=run_dir / "baseline",
        operation_context={"run_id": sandbox_context.run_id, **sandbox_context.binding},
    )
    (workspace.path / "main.py").write_text("answer = 2\n")
    return workspace, run_dir


class RecordingSandbox:
    image = "synthetic-deadline-fixture"

    def __init__(self, action=None):
        self.calls = []
        self.run_ids = []
        self.action = action

    def run(self, workspace, command, timeout, run_id, should_stop, **kwargs):
        self.calls.append(kwargs)
        self.run_ids.append(run_id)
        result = {
            "exit_code": 0,
            "output": "synthetic pass\n",
            "cancelled": False,
            "timed_out": False,
            "cleanup_error": None,
        }
        if self.action:
            self.action(result)
        return result


def test_artifact_json_hash_and_archive_keep_existing_bytes(tmp_path):
    data = {"files": [{"path": "文件", "size": 4}], "empty": []}
    expected = (
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode()
    assert evidence._json(data) == expected
    assert evidence._sha(expected) == hashlib.sha256(expected).hexdigest()
    files = {
        "a": FileContent(b"binary\0" * 10000, 0o755),
        "empty": FileContent(b"", 0o644),
    }
    target = tmp_path / "snapshot.tar.gz"
    evidence._tar(files, target, deadline=time.monotonic() + 5)
    output = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(content.data), content.mode
            member.mtime = member.uid = member.gid = 0
            archive.addfile(member, io.BytesIO(content.data))
    assert target.read_bytes() == output.getvalue()


@pytest.mark.parametrize(
    "operation", ["read", "write", "hash", "json", "tar", "patch", "directories"]
)
def test_expired_helper_starts_no_new_file_work(tmp_path, operation):
    expired = time.monotonic() - 1
    target = tmp_path / "artifact"
    files = {"a": FileContent(b"test", 0o644)}
    with pytest.raises(ExecutionDeadlineExceeded):
        if operation == "read":
            evidence._read_artifact(tmp_path / "missing", "artifact", deadline=expired)
        elif operation == "write":
            evidence._write_artifact(target, b"test", deadline=expired)
        elif operation == "hash":
            evidence._sha(b"test", deadline=expired)
        elif operation == "json":
            evidence._json({"a": "test"}, deadline=expired)
        elif operation == "tar":
            evidence._tar(files, target, deadline=expired)
        elif operation == "patch":
            evidence.build_patch(files, files, deadline=expired)
        else:
            review._directories(tmp_path / "missing", deadline=expired)
    assert not target.exists()


@pytest.mark.parametrize("operation", ["read", "write", "hash", "tar"])
def test_payload_deadline_is_checked_after_partial_progress(
    tmp_path, monkeypatch, operation
):
    payload = os.urandom(evidence.IO_CHUNK_BYTES * 8)
    target = tmp_path / "artifact"
    checks = 0
    read_count = 0
    original_read = evidence.os.read

    def observed_read(*args):
        nonlocal read_count
        data = original_read(*args)
        read_count += len(data)
        return data

    def expire(deadline):
        nonlocal checks
        assert deadline == 123
        checks += 1
        if checks == (8 if operation == "tar" else 4):
            raise ExecutionDeadlineExceeded("synthetic mid-payload expiry")

    monkeypatch.setattr(evidence, "check_deadline", expire)
    monkeypatch.setattr(evidence.os, "read", observed_read)
    with pytest.raises(ExecutionDeadlineExceeded):
        if operation == "read":
            target.write_bytes(payload)
            evidence._read_artifact(tmp_path, "artifact", deadline=123)
        elif operation == "write":
            evidence._write_artifact(target, payload, deadline=123)
        elif operation == "hash":
            evidence._sha(payload, deadline=123)
        else:
            evidence._tar({"large": FileContent(payload, 0o644)}, target, deadline=123)
    if operation == "read":
        assert 0 < read_count < len(payload)
    elif operation in {"write", "tar"}:
        assert target.exists() and target.stat().st_size < len(payload)


def test_artifact_read_preserves_size_and_mutation_checks(tmp_path, monkeypatch):
    target = tmp_path / "artifact"
    target.write_bytes(b"x" * 100)
    monkeypatch.setattr(evidence, "MAX_ARTIFACT_BYTES", 100)
    assert evidence._read_artifact(tmp_path, "artifact") == b"x" * 100
    target.write_bytes(b"x" * 101)
    with pytest.raises(evidence.EvidenceError, match="size limit"):
        evidence._read_artifact(tmp_path, "artifact")
    target.write_bytes(b"x" * 100)
    original_read = evidence.os.read
    changed = False

    def mutate(*args):
        nonlocal changed
        data = original_read(*args)
        if not changed:
            changed = True
            target.write_bytes(b"y" * 100)
        return data

    monkeypatch.setattr(evidence.os, "read", mutate)
    with pytest.raises(evidence.EvidenceError, match="changed"):
        evidence._read_artifact(tmp_path, "artifact")


@pytest.mark.postgres
def test_capture_and_review_forward_exact_deadline(delivery, monkeypatch):
    workspace, run_dir = delivery
    deadline = time.monotonic() + 30
    recorded = []
    for module, name in [
        (evidence, "read_files"),
        (evidence, "write_files"),
        (evidence, "_tar"),
        (evidence, "build_patch"),
        (evidence, "_read_artifact"),
        (review, "read_files"),
        (review, "write_files"),
        (review, "build_patch"),
        (review, "_directories"),
    ]:
        original = getattr(module, name)

        def observe(*args, _original=original, _name=name, **kwargs):
            recorded.append((_name, kwargs.get("deadline")))
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, observe)
    handoff = review.Review.prepare(
        workspace, run_dir, workspace.operation_context["run_id"], deadline=deadline
    )
    assert review.Review.inspect(workspace, run_dir, handoff, deadline=deadline)[
        "valid"
    ]
    sandbox = RecordingSandbox()
    report = evidence.Evidence.capture(
        workspace,
        run_dir,
        ["synthetic check"],
        sandbox,
        10,
        workspace.operation_context["run_id"],
        deadline=deadline,
    )
    binding = dict(workspace.operation_context)
    run_id = binding.pop("run_id")
    assert binding["expected_run_owner"] is None
    assert sandbox.run_ids == [run_id]
    assert sandbox.calls == [{**binding, "deadline": deadline}]
    assert evidence.Evidence.inspect(
        workspace, run_dir, report["report_sha256"], deadline=deadline
    )["verified"]
    assert recorded and all(value == deadline for _, value in recorded)


@pytest.mark.postgres
def test_build_patch_forwards_deadline_and_enforces_finite_output(
    monkeypatch, sandbox_context
):
    deadline = time.monotonic() + 10
    calls = []
    original = evidence._git

    def observe(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence, "_git", observe)
    patch = evidence.build_patch(
        {"a": FileContent(b"old", 0o644)},
        {"a": FileContent(b"new", 0o644)},
        deadline=deadline,
        operation_context={"run_id": sandbox_context.run_id, **sandbox_context.binding},
    )
    assert b"-old" in patch and b"+new" in patch
    assert calls[0]["deadline"] == deadline
    assert calls[0]["stdout_limit"] == 200 * 1024 * 1024
    assert calls[0]["operation_context"] == {
        "run_id": sandbox_context.run_id,
        **sandbox_context.binding,
    }


@pytest.mark.postgres
def test_frozen_verifier_keeps_same_deadline_through_every_copy(
    delivery, tmp_path, monkeypatch
):
    workspace, run_dir = delivery
    verifier = tmp_path / "verifier"
    verifier.mkdir(mode=0o700)
    (verifier / "check.py").write_text("assert True\n")
    (verifier / "check.py").chmod(0o600)
    (verifier / "empty").mkdir(mode=0o700)
    expected, _ = evidence._snapshot(verifier, "verifier", private=True)
    context = {
        "taskset_id": "a" * 64,
        "task_id": "task-0",
        "report_sha256": "b" * 64,
        "verifier_digest": expected["verifier_digest"],
    }
    deadline = time.monotonic() + 10
    calls = []
    original = evidence._snapshot

    def observe(path, kind, **kwargs):
        calls.append((Path(path), kwargs))
        return original(path, kind, **kwargs)

    monkeypatch.setattr(evidence, "_snapshot", observe)
    sandbox = RecordingSandbox()
    report = evidence.Evidence.capture(
        workspace,
        run_dir,
        ["first", "second"],
        sandbox,
        10,
        workspace.operation_context["run_id"],
        verifier_path=verifier,
        verification_context=context,
        deadline=deadline,
    )
    assert report["verified"] and report["verifier_integrity"]["valid"]
    assert len(calls) == 6  # Original before/after, each isolated copy before/after.
    assert all(
        options == {"private": True, "deadline": deadline} for _, options in calls
    )
    assert len(sandbox.calls) == 2
    assert sandbox.run_ids == [workspace.operation_context["run_id"]] * 2
    assert all(options["deadline"] == deadline for options in sandbox.calls)
    assert sandbox.calls[0]["verifier_path"] != sandbox.calls[1]["verifier_path"]


def test_verifier_snapshot_does_not_wrap_expiry_or_cleanup_unknown(
    tmp_path, monkeypatch
):
    def fail(*args, **kwargs):
        raise ExecutionDeadlineExceeded("synthetic snapshot expiry")

    monkeypatch.setattr(evidence, "_snapshot", fail)
    with pytest.raises(ExecutionDeadlineExceeded):
        evidence._verifier_snapshot(tmp_path, {"verifier_digest": "a" * 64})


@pytest.mark.parametrize(
    "step", ["capture", "prepare", "inspect-evidence", "inspect-review"]
)
def test_expired_entry_cannot_publish_or_convert_to_invalid(tmp_path, step):
    # The expired entry must refuse before a registry, Run or workspace is needed.
    workspace, run_dir = Workspace(tmp_path / "workspace"), tmp_path / "run"
    expired = time.monotonic() - 1
    with pytest.raises(ExecutionDeadlineExceeded):
        if step == "capture":
            evidence.Evidence.capture(
                workspace,
                run_dir,
                [],
                RecordingSandbox(),
                10,
                "never-dispatched",
                deadline=expired,
            )
        elif step == "prepare":
            review.Review.prepare(
                workspace, run_dir, "never-dispatched", deadline=expired
            )
        elif step == "inspect-evidence":
            evidence.Evidence.inspect(workspace, run_dir, deadline=expired)
        else:
            review.Review.inspect(workspace, run_dir, {}, deadline=expired)
    assert not (run_dir / "evidence").exists()
    assert not (run_dir / "review").exists()


@pytest.mark.parametrize(
    "kind", ["deadline", "wrapped-deadline", "workspace-unknown", "wrapped-unknown"]
)
@pytest.mark.parametrize("inspector", ["evidence", "review"])
def test_inspection_preserves_control_exceptions(
    tmp_path, monkeypatch, kind, inspector
):
    # The first artifact read raises; no subprocess or database work is involved.
    workspace, run_dir = Workspace(tmp_path / "workspace"), tmp_path / "run"
    if kind in {"deadline", "wrapped-deadline"}:
        failure = ExecutionDeadlineExceeded("synthetic expiry")
    else:
        process = SimpleNamespace(
            pid=12345,
            returncode=0,
            stdout=SimpleNamespace(closed=True),
            stderr=SimpleNamespace(closed=False),
        )
        failure = WorkspaceProcessUnknown(process)
    if kind in {"wrapped-unknown", "wrapped-deadline"}:
        wrapped = ValueError("wrapper")
        wrapped.__cause__ = failure
        failure = wrapped

    def fail(*args, **kwargs):
        raise failure

    module = evidence if inspector == "evidence" else review
    monkeypatch.setattr(module, "_read_artifact", fail)
    with pytest.raises(type(failure)) as result:
        if inspector == "evidence":
            evidence.Evidence.inspect(workspace, run_dir)
        else:
            review.Review.inspect(
                workspace,
                run_dir,
                {
                    "report_sha256": "a" * 64,
                    "context_path": str(run_dir.absolute() / "review/context"),
                },
            )
    assert result.value is failure


@pytest.mark.postgres
def test_capture_cleanup_unknown_survives_later_expiry(delivery, monkeypatch):
    workspace, run_dir = delivery
    expired = False
    cleanup = {"errors": ["synthetic-unknown"], "local_process_unknown": True}
    actual_check = evidence.check_deadline

    def stop_after_cleanup(deadline):
        if expired:
            raise ExecutionDeadlineExceeded("synthetic post-result expiry")
        actual_check(deadline)

    def action(result):
        nonlocal expired
        expired = True
        result.update(cleanup_unknown=True, cleanup_evidence=cleanup)

    monkeypatch.setattr(evidence, "check_deadline", stop_after_cleanup)
    with pytest.raises(evidence.EvidenceError) as captured:
        evidence.Evidence.capture(
            workspace,
            run_dir,
            ["first", "never"],
            RecordingSandbox(action),
            10,
            workspace.operation_context["run_id"],
        )
    assert captured.value.cleanup_unknown is True
    assert captured.value.cleanup_evidence == cleanup
    assert not (run_dir / "evidence/report.json").exists()


@pytest.mark.postgres
def test_capture_expiry_after_first_check_cannot_dispatch_second(delivery, monkeypatch):
    workspace, run_dir = delivery
    expired = False
    actual_check = evidence.check_deadline

    def guard(deadline):
        if expired:
            raise ExecutionDeadlineExceeded("synthetic exhausted shared allowance")
        actual_check(deadline)

    def finish_first(result):
        nonlocal expired
        expired = True

    sandbox = RecordingSandbox(finish_first)
    monkeypatch.setattr(evidence, "check_deadline", guard)
    with pytest.raises(ExecutionDeadlineExceeded):
        evidence.Evidence.capture(
            workspace,
            run_dir,
            ["first", "never"],
            sandbox,
            10,
            workspace.operation_context["run_id"],
            deadline=time.monotonic() + 10,
        )
    assert len(sandbox.calls) == 1
    assert not (run_dir / "evidence/report.json").exists()


@pytest.mark.postgres
def test_review_expiry_before_publish_preserves_existing_workspace(
    delivery, monkeypatch
):
    workspace, run_dir = delivery
    baseline = (workspace.baseline_path / "main.py").read_bytes()
    code = (workspace.path / "main.py").read_bytes()
    original = review._write_artifact

    def stop_before_publish(*args, **kwargs):
        original(*args, **kwargs)
        raise ExecutionDeadlineExceeded("synthetic pre-publication expiry")

    monkeypatch.setattr(review, "_write_artifact", stop_before_publish)
    with pytest.raises(ExecutionDeadlineExceeded):
        review.Review.prepare(
            workspace,
            run_dir,
            workspace.operation_context["run_id"],
            deadline=time.monotonic() + 10,
        )
    assert not (run_dir / "review").exists()
    assert (workspace.baseline_path / "main.py").read_bytes() == baseline
    assert (workspace.path / "main.py").read_bytes() == code
