import hashlib
import json
import os
import subprocess
import sys
import time

import controller_test_support
import pytest
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.workspace import Workspace, WorkspaceError, read_files

controller_registry = controller_test_support.controller_registry


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True
    ).stdout


@pytest.fixture
def repository(tmp_path, controller_registry):
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


def test_export_counts_generated_raw_entries_before_exclusion(
    repository, tmp_path, monkeypatch
):
    import doonex.workspace as module

    (repository / "cache.pyc").write_bytes(b"ignored")
    git(repository, "add", "cache.pyc")
    git(repository, "commit", "-qm", "generated")
    monkeypatch.setattr(module, "MAX_GIT_TREE_ENTRIES", 2)
    workspace = Workspace.create(repository, "HEAD", tmp_path / "exact")
    assert sorted(read_files(workspace.path)) == ["main.py"]
    monkeypatch.setattr(module, "MAX_GIT_TREE_ENTRIES", 1)
    with pytest.raises(WorkspaceError, match="raw tree entry"):
        Workspace.create(repository, "HEAD", tmp_path / "over")
    assert not (tmp_path / "over").exists()


def test_export_counts_generated_tree_bytes_before_exclusion(
    repository, tmp_path, monkeypatch
):
    import doonex.workspace as module

    (repository / "cache.pyc").write_bytes(b"ignored")
    git(repository, "add", "cache.pyc")
    git(repository, "commit", "-qm", "generated")
    raw = git(repository, "ls-tree", "-r", "-z", "-l", "HEAD")
    monkeypatch.setattr(module, "MAX_GIT_TREE_BYTES", len(raw))
    Workspace.create(repository, "HEAD", tmp_path / "exact")
    monkeypatch.setattr(module, "MAX_GIT_TREE_BYTES", len(raw) - 1)
    with pytest.raises(WorkspaceError, match="stdout limit"):
        Workspace.create(repository, "HEAD", tmp_path / "over")
    assert not (tmp_path / "over").exists()


@pytest.mark.parametrize(
    "limit,message",
    [
        ("MAX_GIT_PATH_BYTES", "project path limit"),
        ("MAX_GIT_TREE_RECORD_BYTES", "tree record limit"),
    ],
)
def test_export_rejects_long_generated_records(
    repository, tmp_path, monkeypatch, limit, message
):
    import doonex.workspace as module

    name = "generated-" + "x" * 210 + ".pyc"
    (repository / name).write_bytes(b"ignored")
    git(repository, "add", name)
    git(repository, "commit", "-qm", "long generated record")
    monkeypatch.setattr(module, limit, 200)
    with pytest.raises(WorkspaceError, match=message):
        Workspace.create(repository, "HEAD", tmp_path / "over")


def test_export_checks_config_once_and_reads_batch_objects(
    repository, tmp_path, monkeypatch
):
    import doonex.workspace as module

    (repository / "empty").write_bytes(b"")
    binary = repository / "二进制"
    binary.write_bytes(b"\0\n\xff" * 100_000)
    binary.chmod(0o755)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "batch fixtures")
    calls = []
    original = module._run_git_command

    def observe(command, environment, **kwargs):
        calls.append((command, kwargs))
        return original(command, environment, **kwargs)

    monkeypatch.setattr(module, "_run_git_command", observe)
    workspace = Workspace.create(repository, "HEAD", tmp_path / "task")
    assert (workspace.path / "empty").read_bytes() == b""
    assert (workspace.path / "二进制").read_bytes() == binary.read_bytes()
    assert (workspace.path / "二进制").stat().st_mode & 0o777 == 0o755
    assert sum("config" in call[0] for call in calls) == 1
    assert sum("cat-file" in call[0] for call in calls) == 1
    assert next(options for command, options in calls if "ls-tree" in command)[
        "consume"
    ]
    # Config has its smaller local deadline; all other commands share export's.
    assert (
        len(
            {
                options["deadline"]
                for command, options in calls
                if "config" not in command
            }
        )
        == 1
    )


def test_export_keeps_resolved_commit_after_branch_moves(
    repository, tmp_path, monkeypatch
):
    import doonex.workspace as module

    first = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "main.py").write_text("new commit")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "second")
    second = git(repository, "rev-parse", "HEAD").decode().strip()
    git(repository, "reset", "--hard", first)
    original = module._export_files

    def move_then_export(operation, commit):
        git(repository, "reset", "--hard", second)
        return original(operation, commit)

    monkeypatch.setattr(module, "_export_files", move_then_export)
    workspace = Workspace.create(repository, "HEAD", tmp_path / "task")
    assert workspace.base_commit == first
    assert (workspace.path / "main.py").read_text() == 'print("baseline")\n'
    assert git(repository, "rev-parse", "HEAD").decode().strip() == second


@pytest.mark.parametrize(
    "corruption", ["identity", "type", "size", "content", "truncated", "extra"]
)
def test_batch_rejects_mismatched_or_partial_objects(
    repository, tmp_path, monkeypatch, corruption
):
    import doonex.workspace as module

    original = module._run_git_command

    def corrupt(command, environment, **kwargs):
        if "cat-file" not in command:
            return original(command, environment, **kwargs)
        consume = kwargs["consume"]
        raw = bytearray()
        result = original(command, environment, **{**kwargs, "consume": raw.extend})
        header, data = bytes(raw).split(b"\n", 1)
        fields = header.split(b" ")
        if corruption == "identity":
            fields[0] = b"0" * len(fields[0])
        elif corruption == "type":
            fields[1] = b"tree"
        elif corruption == "size":
            fields[2] = str(int(fields[2]) + 1).encode()
        elif corruption == "content":
            data = bytes([data[0] ^ 1]) + data[1:]
        output = b" ".join(fields) + b"\n" + data
        if corruption == "truncated":
            output = output[:-1]
        elif corruption == "extra":
            output += b"extra"
        for byte in output:
            consume(bytes([byte]))
        return result

    monkeypatch.setattr(module, "_run_git_command", corrupt)
    with pytest.raises(WorkspaceError, match="Git object"):
        Workspace.create(repository, "HEAD", tmp_path / "task")
    assert not (tmp_path / "task").exists()


def test_git_rejects_replace_objects_and_host_environment(
    repository, tmp_path, monkeypatch
):
    import doonex.workspace as module

    original_blob = git(repository, "rev-parse", "HEAD:main.py").decode().strip()
    replacement = repository / "replacement"
    replacement.write_bytes(b"attacker")
    replacement_blob = (
        git(repository, "hash-object", "-w", "replacement").decode().strip()
    )
    git(repository, "replace", original_blob, replacement_blob)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    workspace = Workspace.create(repository, "HEAD", tmp_path / "task")
    assert (workspace.path / "main.py").read_text() == 'print("baseline")\n'
    assert (
        module._git(repository, "rev-parse", "--is-bare-repository").stdout
        == b"false\n"
    )


def test_export_retains_file_and_total_size_limits(repository, tmp_path, monkeypatch):
    import doonex.workspace as module

    size = (repository / "main.py").stat().st_size
    monkeypatch.setattr(module, "MAX_FILE_BYTES", size)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", size)
    Workspace.create(repository, "HEAD", tmp_path / "exact")
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", size - 1)
    with pytest.raises(WorkspaceError, match="resource limit"):
        Workspace.create(repository, "HEAD", tmp_path / "over-total")
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", size)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", size - 1)
    with pytest.raises(WorkspaceError, match="resource limit"):
        Workspace.create(repository, "HEAD", tmp_path / "over-file")


def test_export_expired_outer_deadline_starts_no_git(repository, tmp_path, monkeypatch):
    import doonex.workspace as module

    def forbidden(*args, **kwargs):
        pytest.fail("An expired export must not spawn Git")

    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    with pytest.raises(ExecutionDeadlineExceeded):
        Workspace.create(
            repository, "HEAD", tmp_path / "task", deadline=time.monotonic() - 1
        )
    assert not (tmp_path / "task").exists()


@pytest.mark.parametrize("outer", [False, True])
def test_export_deadline_includes_copy_and_preserves_local_limit(
    repository, tmp_path, monkeypatch, outer
):
    import doonex.workspace as module

    original = module.write_files

    def expire_before_copy(files, destination, *, deadline):
        # Cross the existing allowance after real Git export, then let the
        # actual copy admission enforce it before creating a destination.
        time.sleep(max(deadline - time.monotonic(), 0) + 0.01)
        return original(files, destination, deadline=deadline)

    monkeypatch.setattr(module, "write_files", expire_before_copy)
    monkeypatch.setattr(module, "GIT_OPERATION_TIMEOUT", 0.2 if not outer else 30)
    deadline = time.monotonic() + (0.2 if outer else 30)
    expected = ExecutionDeadlineExceeded if outer else WorkspaceError
    with pytest.raises(expected):
        Workspace.create(repository, "HEAD", tmp_path / "task", deadline=deadline)
    assert not (tmp_path / "task").exists()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_git_pipe_overflow_terminates_and_reaps_child(
    monkeypatch, stream, tmp_path, controller_registry
):
    import doonex.workspace as module

    processes = []
    original = module.subprocess.Popen

    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", observe)
    monkeypatch.setattr(module, "MAX_GIT_STDERR_BYTES", 32)
    output_fd = 1 if stream == "stdout" else 2
    code = f"import os,time; os.write({output_fd}, b'x'*65536); time.sleep(30)"
    deadline = time.monotonic() + 3
    with (
        pytest.raises(WorkspaceError, match=stream + " limit"),
        module._controller_group(
            tmp_path, "git_api_read", deadline, intent=("controlled-pipe-overflow",)
        ) as controller,
    ):
        module._run_git_command(
            [sys.executable, "-c", code],
            os.environ.copy(),
            deadline=deadline,
            stdout_limit=32,
            controller=controller,
        )
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


@pytest.mark.parametrize("outer", [False, True])
def test_git_timeout_reaps_child_and_preserves_outer_marker(
    monkeypatch, outer, tmp_path, controller_registry
):
    import doonex.workspace as module

    processes = []
    original = module.subprocess.Popen

    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", observe)
    deadline = time.monotonic() + 0.1
    expected = ExecutionDeadlineExceeded if outer else WorkspaceError
    with (
        pytest.raises(expected),
        module._controller_group(
            tmp_path, "git_api_read", deadline, intent=("controlled-timeout",)
        ) as controller,
    ):
        module._run_git_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            os.environ.copy(),
            deadline=deadline,
            stdout_limit=32,
            outer_deadline=deadline if outer else None,
            controller=controller,
        )
    assert processes[0].returncode is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_reaped_git_leader_is_not_signaled_again(monkeypatch):
    import doonex.workspace as module

    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    process.wait(timeout=3)

    def forbidden(*args, **kwargs):
        pytest.fail("A reaped leader's numeric group must not be signaled")

    monkeypatch.setattr(module.os, "killpg", forbidden)
    module._terminate_git(process)
    assert process.stdout.closed and process.stderr.closed


def test_pipe_eof_unknown_records_reaped_child(
    monkeypatch, tmp_path, controller_registry
):
    import doonex.workspace as module

    child_ids = []
    code = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
        "print(p.pid,flush=True)"
    )

    def consume(chunk):
        child_ids.extend(int(value) for value in chunk.split())

    try:
        deadline = time.monotonic() + 0.2
        with (
            pytest.raises(module.WorkspaceProcessUnknown) as captured,
            module._controller_group(
                tmp_path,
                "git_api_read",
                deadline,
                intent=("controlled-inherited-pipe",),
            ) as controller,
        ):
            # Keep the persisted allowance's integer contract; only the actual
            # test cleanup is shortened to expose the inherited-pipe boundary.
            monkeypatch.setattr(module, "GIT_CLEANUP_TIMEOUT", 0.1)
            module._run_git_command(
                [sys.executable, "-c", code],
                os.environ.copy(),
                deadline=deadline,
                stdout_limit=128,
                consume=consume,
                controller=controller,
            )
        evidence = captured.value.cleanup_evidence
        assert evidence["local_process_unknown"] is True
        assert evidence["processes"][0]["reaped"] is True
        assert evidence["processes"][0]["pipe_eof"] is False
    finally:
        for child_id in child_ids:
            try:
                os.kill(child_id, 9)
            except ProcessLookupError:
                pass


def test_manifest_and_execution_digest_keep_canonical_bytes(tmp_path):
    import doonex.workspace as module

    (tmp_path / "文件").write_bytes(b"\0binary" * 20_000)
    files = read_files(tmp_path)
    entries = module.manifest(files)
    canonical = json.dumps(
        entries, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    assert module.manifest_digest(entries) == hashlib.sha256(canonical).hexdigest()
    directories = []
    read_files(tmp_path, exclude_generated=False, directories=directories)
    canonical = json.dumps(
        {"files": entries, "directories": directories},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert Workspace.execution_digest(tmp_path) == hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize("helper", ["read", "write", "manifest", "digest", "execution"])
def test_file_helpers_check_deadline_between_chunks(tmp_path, monkeypatch, helper):
    import doonex.workspace as module

    data = b"x" * (module.IO_CHUNK_BYTES * 5)
    source = tmp_path / "source"
    source.mkdir()
    (source / "large").write_bytes(data)
    checks = 0
    expire_at = 7 if helper in ("read", "execution") else 5
    read_bytes = 0
    original_read = module.os.read

    def observe_read(descriptor, size):
        nonlocal read_bytes
        result = original_read(descriptor, size)
        read_bytes += len(result)
        return result

    def limit(deadline):
        nonlocal checks
        assert deadline == 123
        checks += 1
        if checks == expire_at:
            raise ExecutionDeadlineExceeded("fixture expired")

    monkeypatch.setattr(module, "_check_deadline", limit)
    monkeypatch.setattr(module.os, "read", observe_read)
    files = {"large": module.FileContent(data, 0o644)}
    with pytest.raises(ExecutionDeadlineExceeded):
        if helper == "read":
            read_files(source, deadline=123)
        elif helper == "write":
            module.write_files(files, tmp_path / "copy", deadline=123)
        elif helper == "manifest":
            module.manifest(files, deadline=123)
        elif helper == "digest":
            module.manifest_digest([{"path": "large", "size": len(data)}], deadline=123)
        else:
            Workspace.execution_digest(source, deadline=123)
    assert checks == expire_at
    if helper in ("read", "execution"):
        assert 0 < read_bytes < len(data)
    if helper == "write":
        assert (tmp_path / "copy/large").stat().st_size < len(data)
