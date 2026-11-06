import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from doonex.tasksets import TaskSetError, TaskSetRegistry


@pytest.fixture
def taskset_source(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args):
        return (
            subprocess.check_output(["git", "-C", str(repository), *args])
            .decode()
            .strip()
        )

    git("init", "-q")
    git("config", "user.name", "Task set fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "main.py").write_text("answer = 1\n")
    (repository / "run.sh").write_text("#!/bin/sh\nprintf 'fixture'\n")
    (repository / "run.sh").chmod(0o755)
    git("add", ".")
    git("commit", "-qm", "fixed baseline")
    commit = git("rev-parse", "HEAD")
    checker = tmp_path / "checker"
    checker.mkdir()
    (checker / "verify.py").write_text("assert True\n")
    (checker / "fixtures").mkdir()
    (checker / "fixtures/input.bin").write_bytes(b"\x00\xfffixture")
    (checker / "empty").mkdir()
    source = tmp_path / "suite"
    source.mkdir()
    config = {
        "name": "Controlled coding tasks",
        "category": "python",
        "split": "heldout",
        "tasks": [
            {
                "id": f"task-{index}",
                "title": f"Task {index}",
                "prompt": "Fix the controlled source.",
                "repo_path": "../repository",
                "base_ref": "HEAD",
                "visible_checks": ["python main.py"],
                "verifier_path": "../checker",
                "verifier_command": "python /verifier/verify.py",
                "critical": index == 0,
            }
            for index in range(5)
        ],
    }
    (source / "taskset.json").write_text(json.dumps(config))
    return source, config, repository, checker, commit


@pytest.fixture
def registry(tmp_path):
    return TaskSetRegistry(tmp_path / "private")


def write_config(source, config):
    (source / "taskset.json").write_text(json.dumps(config))


def test_import_freezes_git_objects_independent_verifiers_and_provenance(
    taskset_source, registry
):
    source, _, repository, checker, commit = taskset_source
    (repository / "main.py").write_text("dirty user contents\n")
    (repository / "untracked.txt").write_text("untracked user contents\n")
    before = subprocess.check_output(
        ["git", "-C", str(repository), "status", "--porcelain"]
    )
    record = registry.import_local(source)
    assert record["minimum_pass_rate"] == 0.8
    assert record["visibility"] == "private"
    assert record["split"] == "heldout"
    assert record["provenance"] == {
        f"task-{index}": {"repo_path": str(repository), "requested_ref": "HEAD"}
        for index in range(5)
    }
    assert (
        subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"])
        == before
    )
    context = registry.context(record["taskset_id"], record["report_sha256"], "task-0")
    assert context.metadata["base_commit"] == commit
    assert context.source_path.is_absolute() and context.verifier_path.is_absolute()
    assert not context.verifier_path.is_relative_to(context.source_path)
    assert (context.source_path / "main.py").read_text() == "answer = 1\n"
    assert not (context.source_path / "untracked.txt").exists()
    assert not (context.source_path / ".git").exists()
    assert stat.S_IMODE((context.source_path / "run.sh").stat().st_mode) == 0o755
    assert (
        context.verifier_path / "fixtures/input.bin"
    ).read_bytes() == b"\x00\xfffixture"
    assert (context.verifier_path / "empty").is_dir()
    assert "verifier_command" not in context.public_metadata
    assert not any("verifier" in field for field in context.public_metadata)
    assert context.public_metadata["prompt"] == "Fix the controlled source."
    assert registry.inspect(record["taskset_id"], record["report_sha256"])["valid"]
    json.loads(json.dumps(record))
    repository.rename(repository.with_name("moved-repository"))
    checker.rename(checker.with_name("moved-checker"))
    source.rename(source.with_name("moved-suite"))
    assert (
        registry.context(record["taskset_id"], record["report_sha256"], "task-0")
        == context
    )


def test_reimport_ignores_path_aliases_timestamps_and_keeps_first_provenance(
    taskset_source, registry
):
    source, config, repository, checker, commit = taskset_source
    first = registry.import_local(source)
    os.utime(checker / "verify.py", (1, 1))
    relocated = repository.with_name("relocated")
    repository.rename(relocated)
    for task in config["tasks"]:
        task["repo_path"] = str(relocated)
        task["base_ref"] = commit
    config["tasks"].reverse()
    write_config(source, config)
    second = registry.import_local(source)
    assert second == first
    assert len(list(registry.versions.iterdir())) == 1
    assert second["provenance"]["task-0"]["repo_path"] == str(repository)


@pytest.mark.parametrize(
    "change",
    [
        "prompt",
        "threshold",
        "split",
        "critical",
        "source",
        "verifier",
        "mode",
        "directory",
    ],
)
def test_changes_create_new_taskset_versions(taskset_source, registry, change):
    source, config, repository, checker, _ = taskset_source
    first = registry.import_local(source)
    if change == "prompt":
        config["tasks"][0]["prompt"] = "A different task"
    elif change == "threshold":
        config["minimum_pass_rate"] = 0.9
    elif change == "split":
        config["split"] = "tuning"
    elif change == "critical":
        config["tasks"][0]["critical"] = False
    elif change == "source":
        (repository / "main.py").write_text("answer = 2\n")
        subprocess.check_call(
            ["git", "-C", str(repository), "commit", "-qam", "new baseline"]
        )
    elif change == "verifier":
        (checker / "verify.py").write_text("assert 1 == 1\n")
    elif change == "mode":
        (checker / "verify.py").chmod(0o755)
    else:
        (checker / "other-empty").mkdir()
    write_config(source, config)
    second = registry.import_local(source)
    assert first["taskset_id"] != second["taskset_id"]
    assert registry.inspect(first["taskset_id"], first["report_sha256"])["valid"]
    assert registry.inspect(second["taskset_id"], second["report_sha256"])["valid"]


@pytest.mark.parametrize(
    "tamper",
    [
        "source",
        "verifier",
        "mode",
        "directory_mode",
        "extra_source",
        "extra_verifier",
        "empty_directory",
        "unknown_file",
        "unknown_directory",
        "report",
        "report_mode",
        "provenance",
        "symlink",
        "parent_symlink",
        "extra_root",
        "missing",
    ],
)
def test_exact_context_and_report_changes_are_rejected(
    taskset_source, registry, tmp_path, tamper
):
    source, _, _, _, _ = taskset_source
    record = registry.import_local(source)
    context = registry.context(record["taskset_id"], record["report_sha256"], "task-0")
    root = Path(record["context_path"])
    report = root.parent / "report.json"
    if tamper == "source":
        (context.source_path / "main.py").write_text("changed")
    elif tamper == "verifier":
        (context.verifier_path / "verify.py").write_text("changed")
    elif tamper == "mode":
        (context.source_path / "run.sh").chmod(0o644)
    elif tamper == "directory_mode":
        (context.verifier_path / "fixtures").chmod(0o755)
    elif tamper == "extra_source":
        (context.source_path / "added.py").write_text("extra")
    elif tamper == "extra_verifier":
        (context.verifier_path / "added.py").write_text("extra")
    elif tamper == "empty_directory":
        (context.source_path / "extra-empty").mkdir()
    elif tamper == "unknown_file":
        (root / "unknown.txt").write_text("extra")
    elif tamper == "unknown_directory":
        (root / "unknown").mkdir()
    elif tamper == "report":
        report.write_text("{}")
    elif tamper == "report_mode":
        report.chmod(0o644)
    elif tamper == "provenance":
        saved = json.loads(report.read_text())
        saved["provenance"]["task-0"]["requested_ref"] = "different"
        report.write_text(json.dumps(saved))
    elif tamper == "symlink":
        (context.source_path / "main.py").unlink()
        (context.source_path / "main.py").symlink_to(
            context.verifier_path / "verify.py"
        )
    elif tamper == "parent_symlink":
        root.rename(tmp_path / "moved-context")
        root.symlink_to(tmp_path / "moved-context", target_is_directory=True)
    elif tamper == "extra_root":
        (root.parent / "unknown").mkdir()
    else:
        (context.verifier_path / "verify.py").unlink()
    status = registry.inspect(record["taskset_id"], record["report_sha256"])
    assert not status["valid"] and status["errors"]
    with pytest.raises(TaskSetError):
        registry.context(record["taskset_id"], record["report_sha256"], "task-0")
    if tamper == "provenance":
        # Provenance is intentionally outside version identity. The product DB
        # keeps the first report hash; repeat import must never rebind that row.
        repeated = registry.import_local(source)
        assert repeated["taskset_id"] == record["taskset_id"]
        assert repeated["report_sha256"] != record["report_sha256"]
        assert not registry.inspect(repeated["taskset_id"], record["report_sha256"])[
            "valid"
        ]
    else:
        with pytest.raises(TaskSetError):
            registry.import_local(source)


def test_import_never_executes_git_hooks_source_or_verifier(
    taskset_source, registry, tmp_path
):
    source, config, repository, checker, _ = taskset_source
    marker = tmp_path / "must-not-exist"
    (repository / ".git/hooks/post-checkout").write_text(
        f"#!/bin/sh\ntouch '{marker}'\n"
    )
    (repository / ".git/hooks/post-checkout").chmod(0o755)
    (checker / "verify.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    for task in config["tasks"]:
        task["visible_checks"] = [f"touch '{marker}'"]
        task["verifier_command"] = f"touch '{marker}'"
    write_config(source, config)
    registry.import_local(source)
    assert not marker.exists()


@pytest.mark.parametrize(
    "case",
    [
        "few",
        "many",
        "duplicate",
        "bad_id",
        "unknown_top",
        "unknown_task",
        "low_threshold",
        "nan_threshold",
        "bool_threshold",
        "bad_split",
        "bad_critical",
        "bad_checks",
        "bad_ref",
        "missing_prompt",
    ],
)
def test_input_contract_rejects_invalid_configuration(taskset_source, registry, case):
    source, config, _, _, _ = taskset_source
    if case == "few":
        config["tasks"].pop()
    elif case == "many":
        config["tasks"] *= 3
    elif case == "duplicate":
        config["tasks"][1]["id"] = "TASK-0"
    elif case == "bad_id":
        config["tasks"][0]["id"] = "../outside"
    elif case == "unknown_top":
        config["unknown"] = True
    elif case == "unknown_task":
        config["tasks"][0]["unknown"] = True
    elif case == "low_threshold":
        config["minimum_pass_rate"] = 0.79
    elif case == "nan_threshold":
        config["minimum_pass_rate"] = float("nan")
    elif case == "bool_threshold":
        config["minimum_pass_rate"] = True
    elif case == "bad_split":
        config["split"] = "training"
    elif case == "bad_critical":
        config["tasks"][0]["critical"] = "true"
    elif case == "bad_checks":
        config["tasks"][0]["visible_checks"] = "python main.py"
    elif case == "bad_ref":
        config["tasks"][0]["base_ref"] = "missing-ref"
    else:
        del config["tasks"][0]["prompt"]
    write_config(source, config)
    with pytest.raises(TaskSetError):
        registry.import_local(source)
    assert list(registry.versions.iterdir()) == []


@pytest.mark.parametrize(
    "case",
    [
        "inside_repository",
        "contains_repository",
        "private",
        "secret",
        "symlink",
        "fifo",
        "source_symlink",
        "config_symlink",
        "nested_repo",
    ],
)
def test_paths_links_special_files_and_secret_inputs_are_rejected(
    taskset_source, registry, tmp_path, case
):
    source, config, repository, checker, _ = taskset_source
    if case == "inside_repository":
        checker = repository / "checker"
        checker.mkdir()
        (checker / "verify.py").write_text("pass")
        config["tasks"][0]["verifier_path"] = str(checker)
    elif case == "contains_repository":
        config["tasks"][0]["verifier_path"] = str(tmp_path)
    elif case == "private":
        config["tasks"][0]["verifier_path"] = str(registry.home)
    elif case == "secret":
        (checker / ".env").write_text("synthetic-secret-fixture")
    elif case == "symlink":
        (checker / "linked.py").symlink_to(checker / "verify.py")
    elif case == "fifo":
        os.mkfifo(checker / "fifo")
    elif case == "source_symlink":
        link = tmp_path / "linked-repo"
        link.symlink_to(repository, target_is_directory=True)
        config["tasks"][0]["repo_path"] = str(link)
    elif case == "config_symlink":
        (source / "taskset.json").rename(source / "actual.json")
        (source / "taskset.json").symlink_to(source / "actual.json")
    else:
        nested = repository / "nested"
        nested.mkdir()
        config["tasks"][0]["repo_path"] = str(nested)
    if case != "config_symlink":
        write_config(source, config)
    with pytest.raises(TaskSetError):
        registry.import_local(source)
    assert list(registry.versions.iterdir()) == []


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_CONFIG_BYTES",
        "MAX_REPORT_BYTES",
        "MAX_SUITE_BYTES",
        "MAX_SUITE_ENTRIES",
        "MAX_SKILL_FILE_BYTES",
    ],
)
def test_resource_bounds_fail_without_partial_publication(
    taskset_source, registry, monkeypatch, limit
):
    from doonex import tasksets

    source, _, _, _, _ = taskset_source
    monkeypatch.setattr(tasksets, limit, 1)
    with pytest.raises(TaskSetError):
        registry.import_local(source)
    assert list(registry.versions.iterdir()) == []


def test_report_binding_unknown_task_and_invalid_ids_fail_closed(
    taskset_source, registry
):
    source, _, _, _, _ = taskset_source
    record = registry.import_local(source)
    for taskset_id in ("bad", "../outside", "A" * 64):
        assert not registry.inspect(taskset_id, record["report_sha256"])["valid"]
    for saved_hash in (None, "", "0" * 64):
        assert not registry.inspect(record["taskset_id"], saved_hash)["valid"]
    with pytest.raises(TaskSetError, match="Unknown task"):
        registry.context(record["taskset_id"], record["report_sha256"], "missing-task")


def test_explicit_git_project_in_managed_worktree_is_allowed(
    taskset_source, registry, tmp_path, monkeypatch
):
    source, config, repository, _, _ = taskset_source
    fake_home = tmp_path / "user-home"
    project = fake_home / ".codex/worktrees/controlled/repository"
    project.parent.mkdir(parents=True)
    repository.rename(project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    for task in config["tasks"]:
        task["repo_path"] = str(project)
    write_config(source, config)
    record = registry.import_local(source)
    assert record["provenance"]["task-0"]["repo_path"] == str(project)
    assert registry.inspect(record["taskset_id"], record["report_sha256"])["valid"]


def test_codex_management_folder_is_not_a_verifier_source(
    taskset_source, registry, tmp_path, monkeypatch
):
    source, config, _, checker, _ = taskset_source
    fake_home = tmp_path / "user-home"
    private_checker = fake_home / ".codex/private-checker"
    private_checker.parent.mkdir(parents=True)
    checker.rename(private_checker)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    config["tasks"][0]["verifier_path"] = str(private_checker)
    write_config(source, config)
    with pytest.raises(TaskSetError, match="management"):
        registry.import_local(source)


def test_management_directory_inside_verifier_is_rejected(taskset_source, registry):
    source, _, _, checker, _ = taskset_source
    (checker / ".codex").mkdir()
    (checker / ".codex/auth.json").write_text('{"synthetic": "private-fixture"}')
    with pytest.raises(TaskSetError, match="management"):
        registry.import_local(source)


def test_case_alias_cannot_hide_verifier_repository_overlap(taskset_source, registry):
    source, config, repository, _, _ = taskset_source
    alias = repository.with_name(repository.name.upper())
    if not alias.exists() or not alias.samefile(repository):
        pytest.skip("Filesystem is case-sensitive")
    nested = repository / "hidden-checker"
    nested.mkdir()
    (nested / "verify.py").write_text("pass")
    config["tasks"][0]["verifier_path"] = str(alias / "hidden-checker")
    write_config(source, config)
    with pytest.raises(TaskSetError, match="separate"):
        registry.import_local(source)
