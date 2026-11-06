import errno
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from doonex.evidence import Evidence
from doonex.review import Review, ReviewError
from doonex.sandbox import Sandbox
from doonex.workspace import Workspace, read_files


@pytest.fixture
def review_workspace(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    git("init", "-q")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Review fixture")
    (repository / "main.py").write_text("answer = 1\n")
    (repository / "deleted.txt").write_text("before only\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    run_dir = tmp_path / "run"
    workspace = Workspace.create(
        repository,
        "HEAD",
        run_dir / "workspace",
        baseline_path=run_dir / "baseline",
    )
    (workspace.path / "main.py").write_text("answer = 2\n")
    (workspace.path / "deleted.txt").unlink()
    (workspace.path / "added.bin").write_bytes(b"\x00binary\xff")
    return workspace, run_dir


def test_handoff_is_complete_database_bound_and_idempotent(review_workspace):
    workspace, run_dir = review_workspace
    run_id = str(uuid4())
    handoff = Review.prepare(workspace, run_dir, run_id)
    assert isinstance(handoff["context_path"], str)
    context = Path(handoff["context_path"])
    assert context.is_absolute()
    assert handoff["snapshot_digest"] == Workspace.snapshot_digest(workspace.path)
    assert handoff["baseline_digest"] == workspace.baseline_digest
    assert (context / "before/main.py").read_text() == "answer = 1\n"
    assert (context / "after/main.py").read_text() == "answer = 2\n"
    assert (context / "before/deleted.txt").is_file()
    assert not (context / "after/deleted.txt").exists()
    assert (context / "after/added.bin").read_bytes() == b"\x00binary\xff"
    assert "GIT binary patch" in (context / "diff.patch").read_text()
    metadata = json.loads((context / "handoff.json").read_text())
    assert metadata["run_id"] == run_id
    assert metadata["snapshot_digest"] == handoff["snapshot_digest"]
    assert str(workspace.repository) not in (context / "handoff.json").read_text()
    assert sorted(path.name for path in context.iterdir()) == [
        "after",
        "before",
        "diff.patch",
        "handoff.json",
    ]
    assert Review.inspect(workspace, run_dir, handoff)["valid"]
    assert Review.prepare(workspace, run_dir, run_id) == handoff
    assert not Review.inspect(workspace, run_dir, {})["valid"]
    assert not Review.inspect(workspace, run_dir, {**handoff, "run_id": "other"})[
        "valid"
    ]


@pytest.mark.parametrize(
    "case",
    [
        "before",
        "after",
        "patch",
        "metadata",
        "report",
        "mode",
        "missing",
        "extra",
        "cache",
        "directory",
        "symlink",
        "parent_symlink",
    ],
)
def test_context_changes_are_rejected(review_workspace, tmp_path, case):
    workspace, run_dir = review_workspace
    handoff = Review.prepare(workspace, run_dir, str(uuid4()))
    context = Path(handoff["context_path"])
    if case in {"before", "after"}:
        (context / case / "main.py").write_text("tampered\n")
    elif case == "patch":
        (context / "diff.patch").write_text("tampered\n")
    elif case == "metadata":
        (context / "handoff.json").write_text("{}")
    elif case == "report":
        report = run_dir / "review/report.json"
        report.write_text(
            report.read_text().replace(handoff["snapshot_digest"], "0" * 64)
        )
    elif case == "mode":
        (context / "after/main.py").chmod(0o755)
    elif case == "missing":
        (context / "before/main.py").unlink()
    elif case == "extra":
        (context / "after/unbound.txt").write_text("extra")
    elif case == "cache":
        (context / "after/__pycache__").mkdir()
        (context / "after/__pycache__/injected.pyc").write_bytes(b"extra")
    elif case == "directory":
        (context / "after/unbound-directory").mkdir()
    elif case == "symlink":
        (context / "after/main.py").unlink()
        (context / "after/main.py").symlink_to(workspace.path / "main.py")
    else:
        context.rename(tmp_path / "moved-context")
        context.symlink_to(tmp_path / "moved-context", target_is_directory=True)
    status = Review.inspect(workspace, run_dir, handoff)
    assert not status["valid"] and status["errors"]
    with pytest.raises(ReviewError):
        Review.prepare(workspace, run_dir, handoff["run_id"])


@pytest.mark.parametrize("target", ["workspace", "baseline"])
def test_executor_or_initial_copy_changes_invalidate_handoff(review_workspace, target):
    workspace, run_dir = review_workspace
    handoff = Review.prepare(workspace, run_dir, str(uuid4()))
    original_report = (run_dir / "review/report.json").read_bytes()
    root = workspace.path if target == "workspace" else workspace.baseline_path
    (root / "main.py").write_text("changed after handoff\n")
    status = Review.inspect(workspace, run_dir, handoff)
    assert not status["valid"]
    with pytest.raises(ValueError):
        Review.prepare(workspace, run_dir, handoff["run_id"])
    assert (run_dir / "review/report.json").read_bytes() == original_report


@pytest.mark.docker
def test_review_container_cannot_write_delete_or_rename_context(review_workspace):
    workspace, run_dir = review_workspace
    run_id = str(uuid4())
    handoff = Review.prepare(workspace, run_dir, run_id)
    context = Path(handoff["context_path"])
    before = read_files(context, exclude_generated=False)
    script = """import errno, json, os
from pathlib import Path
results = []
for name in ['before/main.py', 'after/main.py', 'diff.patch', 'handoff.json']:
    path = Path('/workspace') / name
    for operation in [lambda: path.write_text('overwritten'), lambda: path.unlink(), lambda: path.rename(str(path) + '.moved')]:
        try:
            operation()
            results.append(None)
        except OSError as error:
            results.append(error.errno)
for operation in [lambda: Path('/workspace/after/new.py').write_text('new'), lambda: Path('/workspace/after/newdir').mkdir()]:
    try:
        operation()
        results.append(None)
    except OSError as error:
        results.append(error.errno)
Path('/tmp/review-scratch').write_text('scratch is isolated')
print(json.dumps(results))
"""
    sandbox = Sandbox()
    result = sandbox.run(
        context, "python - <<'PY'\n" + script + "\nPY", 10, run_id, read_only=True
    )
    assert result["exit_code"] == 0, result
    assert json.loads(result["output"]) == [errno.EROFS] * 14
    assert read_files(context, exclude_generated=False) == before
    assert Review.inspect(workspace, run_dir, handoff)["valid"]
    assert sandbox.containers(run_id) == []


@pytest.mark.docker
def test_review_and_final_delivery_survive_original_repository_move(
    review_workspace, tmp_path
):
    workspace, run_dir = review_workspace
    workspace.repository.rename(tmp_path / "moved-source")
    # Reconstruct exactly as a restarted worker does from its saved baseline hash.
    workspace = Workspace(
        workspace.path,
        workspace.repository,
        workspace.base_commit,
        workspace.baseline_path,
        workspace.baseline_digest,
    )
    run_id = str(uuid4())
    handoff = Review.prepare(workspace, run_dir, run_id)
    assert Review.inspect(workspace, run_dir, handoff)["valid"]
    report = Evidence.capture(
        workspace,
        run_dir,
        ['python -c "import main; assert main.answer == 2"'],
        Sandbox(),
        10,
        run_id,
    )
    assert report["verified"] and report["checks"][0]["exit_code"] == 0
    assert report["snapshot_digest"] == handoff["snapshot_digest"]
    assert Evidence.inspect(workspace, run_dir, report["report_sha256"])["verified"]
    assert "-answer = 1" in (run_dir / "evidence/diff.patch").read_text()


def test_baseline_requires_its_saved_hash_and_is_not_writable_task_content(
    review_workspace,
):
    workspace, _ = review_workspace
    with pytest.raises(ValueError, match="persisted content digest"):
        Workspace(workspace.path, baseline_path=workspace.baseline_path).base_files()
    with pytest.raises(ValueError, match="separate"):
        Workspace.create(
            workspace.repository,
            workspace.base_commit,
            workspace.path / "nested-task",
            baseline_path=workspace.path,
        )
