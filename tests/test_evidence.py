import subprocess
import tarfile
import uuid

import pytest
from doonex.evidence import Evidence
from doonex.sandbox import Sandbox
from doonex.workspace import Workspace


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True
    ).stdout


@pytest.fixture
def workspace(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "config", "user.name", "Fixture")
    (repository / "main.py").write_text('print("old")\n')
    git(repository, "add", "main.py")
    git(repository, "commit", "-qm", "baseline")
    task = Workspace.create(repository, "HEAD", tmp_path / "workspace")
    (task.path / "main.py").write_text('print("new")\n')
    (task.path / "added.py").write_text("answer = 42\n")
    return task


@pytest.mark.docker
def test_fixed_snapshot_fresh_checks_and_delivery_integrity(workspace, tmp_path):
    run_dir = tmp_path / "run"
    report = Evidence.capture(
        workspace,
        run_dir,
        [
            "python -c \"from pathlib import Path; Path('main.py').write_text('polluted')\"",
            "python -c \"from pathlib import Path; assert Path('main.py').read_text() == 'print(\\\"new\\\")\\n'; import added; assert added.answer == 42\"",
        ],
        Sandbox(),
        10,
        uuid.uuid4().hex,
    )
    assert report["verified"], report
    status = Evidence.inspect(workspace, run_dir, report["report_sha256"])
    assert status["valid"] and status["verified"], status
    root = run_dir / "evidence"
    patch = (root / "diff.patch").read_text()
    assert "diff --git a/main.py b/main.py" in patch
    assert "added.py" in patch and "+answer = 42" in patch
    git(workspace.repository, "apply", "--check", str(root / "diff.patch"))
    with tarfile.open(root / "snapshot.tar.gz") as archive:
        assert sorted(archive.getnames()) == ["added.py", "main.py"]
    assert (workspace.path / "main.py").read_text() == 'print("new")\n'
    (workspace.path / "added.py").write_text("answer = 0\n")
    status = Evidence.inspect(workspace, run_dir)
    assert status["stale"] and not status["verified"]


@pytest.mark.docker
def test_failed_timeout_and_missing_checks_cannot_verify(workspace, tmp_path):
    failure = Evidence.capture(
        workspace, tmp_path / "failed", ["exit 7"], Sandbox(), 10, uuid.uuid4().hex
    )
    assert not failure["verified"] and failure["checks"][0]["exit_code"] == 7
    timeout = Evidence.capture(
        workspace, tmp_path / "timeout", ["sleep 30"], Sandbox(), 0.4, uuid.uuid4().hex
    )
    assert not timeout["verified"] and timeout["checks"][0]["timed_out"]
    unchecked = Evidence.capture(
        workspace, tmp_path / "unchecked", [], Sandbox(), 10, uuid.uuid4().hex
    )
    assert not unchecked["verified"]


@pytest.mark.parametrize(
    "artifact", ["diff.patch", "manifest.json", "snapshot.tar.gz", "report.json"]
)
def test_missing_or_changed_artifact_invalidates_evidence(
    workspace, tmp_path, artifact
):
    run_dir = tmp_path / "run"
    report = Evidence.capture(workspace, run_dir, [], Sandbox(), 10, uuid.uuid4().hex)
    path = run_dir / "evidence" / artifact
    path.write_bytes(path.read_bytes() + b"tampered")
    assert not Evidence.inspect(workspace, run_dir, report["report_sha256"])["valid"]
    path.unlink()
    assert not Evidence.inspect(workspace, run_dir)["valid"]


def test_fixed_snapshot_and_log_symlinks_are_rejected(workspace, tmp_path):
    run_dir = tmp_path / "run"
    Evidence.capture(workspace, run_dir, [], Sandbox(), 10, uuid.uuid4().hex)
    snapshot = run_dir / "evidence" / "snapshot"
    (snapshot / "main.py").unlink()
    (snapshot / "main.py").symlink_to(workspace.path / "main.py")
    status = Evidence.inspect(workspace, run_dir)
    assert not status["valid"] and status["errors"]


@pytest.mark.docker
def test_missing_or_replaced_check_log_invalidates_evidence(workspace, tmp_path):
    run_dir = tmp_path / "run"
    Evidence.capture(
        workspace, run_dir, ["printf verified"], Sandbox(), 10, uuid.uuid4().hex
    )
    log = run_dir / "evidence" / "check-1.log"
    log.write_text("replaced")
    assert not Evidence.inspect(workspace, run_dir)["valid"]
    log.unlink()
    log.symlink_to(workspace.path / "main.py")
    assert not Evidence.inspect(workspace, run_dir)["valid"]
