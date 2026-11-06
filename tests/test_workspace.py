import os
import subprocess

import pytest
from doonex.workspace import Workspace, WorkspaceError, read_files


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True
    ).stdout


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "main.py").write_text('print("baseline")\n')
    git(root, "add", "main.py")
    git(root, "commit", "-qm", "baseline")
    return root


def test_export_preserves_original_dirty_and_staged_state(repository, tmp_path):
    commit = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "main.py").write_text("staged\n")
    git(repository, "add", "main.py")
    (repository / "main.py").write_text("unstaged\n")
    (repository / "private.txt").write_text("untracked\n")
    status = git(repository, "status", "--porcelain=v1")
    staged = git(repository, "diff", "--cached", "--binary")
    unstaged = git(repository, "diff", "--binary")
    workspace = Workspace.create(repository, commit, tmp_path / "task")
    assert (workspace.path / "main.py").read_text() == 'print("baseline")\n'
    assert not (workspace.path / "private.txt").exists()
    assert not (workspace.path / ".git").exists()
    assert status == git(repository, "status", "--porcelain=v1")
    assert staged == git(repository, "diff", "--cached", "--binary")
    assert unstaged == git(repository, "diff", "--binary")


def test_export_does_not_run_hooks_or_checkout_filters(repository, tmp_path):
    sentinel = tmp_path / "hook-ran"
    hook = repository / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    hook.chmod(0o755)
    git(repository, "config", "filter.attack.smudge", f"touch {sentinel}")
    (repository / ".gitattributes").write_text("*.py filter=attack\n")
    git(repository, "add", ".gitattributes")
    git(repository, "commit", "-qm", "attributes")
    Workspace.create(repository, "HEAD", tmp_path / "task")
    assert not sentinel.exists()


def test_reject_nested_root_invalid_revision_and_secret(repository, tmp_path):
    nested = repository / "nested"
    nested.mkdir()
    with pytest.raises(WorkspaceError):
        Workspace.validate_repository(nested)
    with pytest.raises(WorkspaceError):
        Workspace.create(repository, "not-a-commit", tmp_path / "bad")
    (repository / ".env").write_text("SYNTHETIC_SECRET=never-export\n")
    git(repository, "add", ".env")
    git(repository, "commit", "-qm", "unsafe fixture")
    with pytest.raises(WorkspaceError, match="Sensitive"):
        Workspace.create(repository, "HEAD", tmp_path / "secret-task")


def test_reject_git_symlink_and_runtime_symlink(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("synthetic secret")
    os.symlink(outside, repository / "link")
    git(repository, "add", "link")
    git(repository, "commit", "-qm", "link fixture")
    with pytest.raises(WorkspaceError, match="Symlink"):
        Workspace.create(repository, "HEAD", tmp_path / "bad")
    clean = tmp_path / "clean"
    clean.mkdir()
    os.symlink(outside, clean / "link")
    with pytest.raises(WorkspaceError, match="Symlink"):
        read_files(clean)


def test_reject_special_files_and_resource_limits(tmp_path, monkeypatch):
    import doonex.workspace as module

    root = tmp_path / "task"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(WorkspaceError, match="special"):
        read_files(root)
    (root / "pipe").unlink()
    (root / "large.txt").write_bytes(b"12345")
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 4)
    with pytest.raises(WorkspaceError, match="limit"):
        read_files(root)


def test_snapshot_digest_tracks_content_new_deleted_and_mode(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    file = root / "main"
    file.write_text("one")
    first = Workspace.snapshot_digest(root)
    file.write_text("two")
    assert Workspace.snapshot_digest(root) != first
    second = Workspace.snapshot_digest(root)
    file.chmod(0o755)
    assert Workspace.snapshot_digest(root) != second
    third = Workspace.snapshot_digest(root)
    (root / "new").write_text("new")
    assert Workspace.snapshot_digest(root) != third
    file.unlink()
    assert Workspace.snapshot_digest(root) != third


def test_generated_caches_are_excluded_but_symlinks_are_still_rejected(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    (root / "main.py").write_text("pass")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "main.pyc").write_bytes(b"generated")
    assert sorted(read_files(root)) == ["main.py"]
    (root / ".venv").symlink_to(cache)
    with pytest.raises(WorkspaceError, match="Symlink"):
        read_files(root)


def test_fifo_replacement_does_not_block_snapshot(tmp_path, monkeypatch):
    import doonex.workspace as module

    root = tmp_path / "task"
    root.mkdir()
    target = root / "main"
    target.write_text("safe")
    original_open = module.os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if path == "main":
            target.unlink()
            os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", replace_before_open)
    with pytest.raises(WorkspaceError, match="changed"):
        read_files(root)
