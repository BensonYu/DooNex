"""Host Git operations never execute a registered project's content commands."""

import errno
import os
import shlex
import subprocess
import sys
import threading
import time

import controller_test_support
import pytest
from doonex import controller_operations as co
from doonex.api import create_app
from doonex.api import git as api_git
from doonex.workspace import Workspace, WorkspaceError, _git
from fastapi.testclient import TestClient

controller_registry = controller_test_support.controller_registry


def git(repository, *arguments):
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def repository(tmp_path, controller_registry):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Offline fixture")
    (root / "sample.txt").write_text("tracked fixture\n")
    (root / ".gitattributes").write_text("*.txt filter=Probe.Driver diff=probe\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    return root


@pytest.fixture
def probe(tmp_path):
    marker = tmp_path / "host-command-ran"
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('invoked')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    command = shlex.quote(sys.executable) + " " + shlex.quote(str(script))
    return marker, command


@pytest.mark.parametrize("setting", ["clean", "process"])
def test_status_disables_filters_and_preserves_dirty_state(repository, probe, setting):
    marker, command = probe
    git(repository, "config", f"filter.Probe.Driver.{setting}", command)
    git(repository, "config", "filter.Probe.Driver.required", "true")
    assert not _git(repository, "status", "--porcelain=v1").stdout
    # Equal-length edits force Git to compare content rather than only stat size.
    (repository / "sample.txt").write_text("altered fixture\n")
    assert _git(repository, "status", "--porcelain=v1").stdout == b" M sample.txt\n"
    assert not marker.exists()


def test_disabled_smudge_and_required_pass_original_bytes(repository, probe):
    marker, command = probe
    git(repository, "config", "filter.Probe.Driver.smudge", command)
    git(repository, "config", "filter.Probe.Driver.required", "true")
    (repository / "sample.txt").unlink()
    _git(repository, "checkout-index", "--all")
    assert (repository / "sample.txt").read_text() == "tracked fixture\n"
    assert not marker.exists()


@pytest.mark.parametrize("conditional", [False, True])
def test_filters_in_effective_includes_are_disabled(
    repository, probe, tmp_path, conditional
):
    marker, command = probe
    included = tmp_path / "included.config"
    git(
        repository,
        "config",
        "--file",
        str(included),
        "filter.Probe.Driver.clean",
        command,
    )
    key = f"includeIf.gitdir:{repository}/.git.path" if conditional else "include.path"
    git(repository, "config", key, str(included))
    (repository / "sample.txt").write_text("altered fixture\n")
    assert api_git(repository, "status", "--porcelain=v1") == "M sample.txt"
    assert not marker.exists()


def test_filter_from_worktree_config_is_disabled(repository, probe, tmp_path):
    marker, command = probe
    git(repository, "config", "extensions.worktreeConfig", "true")
    linked = tmp_path / "linked"
    git(repository, "worktree", "add", "-q", "-b", "linked", str(linked))
    git(linked, "config", "--worktree", "filter.Probe.Driver.clean", command)
    git(linked, "config", "--worktree", "filter.Probe.Driver.required", "true")
    (linked / "sample.txt").write_text("altered fixture\n")
    assert _git(linked, "status", "--porcelain=v1").stdout == b" M sample.txt\n"
    assert not marker.exists()


def test_filter_subsection_with_equals_is_disabled(repository, probe):
    marker, command = probe
    (repository / ".gitattributes").write_text("*.txt filter=equals=driver\n")
    git(repository, "add", ".gitattributes")
    git(repository, "commit", "-qm", "attribute with equals")
    git(repository, "config", "filter.equals=driver.clean", command)
    git(repository, "config", "filter.equals=driver.required", "true")
    (repository / "sample.txt").write_text("altered fixture\n")
    assert _git(repository, "status", "--porcelain=v1").stdout == b" M sample.txt\n"
    assert not marker.exists()


@pytest.mark.parametrize(
    "setting", ["diff.probe.command", "diff.probe.textconv", "diff.external"]
)
@pytest.mark.parametrize("operation", ["diff", "show", "log"])
def test_diff_commands_do_not_execute_external_drivers(
    repository, probe, setting, operation
):
    marker, command = probe
    git(repository, "config", setting, command)
    (repository / "sample.txt").write_text("altered fixture\n")
    result = _git(repository, operation, "-p")
    assert result.returncode == 0 and b"tracked fixture" in result.stdout
    assert not marker.exists()


def test_filter_configuration_is_not_cached(repository, probe):
    marker, command = probe
    assert not _git(repository, "status", "--porcelain=v1").stdout
    git(repository, "config", "filter.Probe.Driver.clean", command)
    (repository / "sample.txt").write_text("altered fixture\n")
    assert _git(repository, "status", "--porcelain=v1").stdout
    assert not marker.exists()


def test_environment_cannot_add_filter_commands(repository, probe, monkeypatch):
    marker, command = probe
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.Probe.Driver.clean")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    (repository / "sample.txt").write_text("altered fixture\n")
    assert _git(repository, "status", "--porcelain=v1").stdout
    assert _git(repository, "diff").stdout
    assert not marker.exists()


def test_config_key_output_limit_fails_closed(repository, probe, monkeypatch):
    import doonex.workspace as module

    marker, command = probe
    git(repository, "config", "filter.Probe.Driver.clean", command)
    (repository / "sample.txt").write_text("altered fixture\n")
    monkeypatch.setattr(module, "MAX_GIT_CONFIG_BYTES", 8)
    deadline = time.monotonic() + module.GIT_OPERATION_TIMEOUT
    # Keep this case focused on real configuration output: the public _git
    # intent's separate byte limit uses the same deliberately reduced constant.
    with (
        pytest.raises(WorkspaceError, match="configuration key limit"),
        module._controller_group(
            repository, "git_api_read", deadline, intent=()
        ) as controller,
    ):
        module._GitOperation(repository, deadline, controller=controller).run(
            "status", "--porcelain=v1", check=False
        )
    assert not marker.exists()


@pytest.mark.parametrize("limit", ["MAX_GIT_FILTERS", "MAX_GIT_FILTER_NAME_BYTES"])
def test_filter_override_limits_fail_closed(repository, probe, monkeypatch, limit):
    import doonex.workspace as module

    marker, command = probe
    git(repository, "config", "filter.Probe.Driver.clean", command)
    monkeypatch.setattr(module, limit, 0)
    with pytest.raises(WorkspaceError, match="filter configuration limit"):
        _git(repository, "status", "--porcelain=v1", check=False)
    assert not marker.exists()


def test_config_include_timeout_fails_closed(
    repository, tmp_path, monkeypatch, controller_registry
):
    import doonex.workspace as module

    fifo = tmp_path / "blocking-config"
    os.mkfifo(fifo)
    git(repository, "config", "include.path", str(fifo))
    # Observe Git opening the include, instead of assuming PG admission and
    # Git startup finish in 100ms. Hold its writer open without providing data.
    monkeypatch.setattr(module, "GIT_CONFIG_TIMEOUT", 3)
    stop = threading.Event()
    opened, failures, commands, processes = [], [], [], []
    original_run, original_popen = module._run_git_command, module.subprocess.Popen

    def hold_writer():
        descriptor = None
        try:
            while not stop.is_set():
                try:
                    descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    opened.append(time.monotonic())
                    stop.wait(10)
                    return
                except OSError as error:
                    if error.errno != errno.ENXIO:
                        raise
                    stop.wait(0.01)
        except OSError as error:
            failures.append(error)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def observe_run(command, *args, **kwargs):
        commands.append((command, kwargs["deadline"]))
        return original_run(command, *args, **kwargs)

    def observe_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module, "_run_git_command", observe_run)
    monkeypatch.setattr(module.subprocess, "Popen", observe_popen)
    writer = threading.Thread(target=hold_writer, daemon=True)
    writer.start()
    try:
        with pytest.raises(WorkspaceError, match="configuration inspection timed out"):
            _git(repository, "status", "--porcelain=v1", check=False)
        assert len(commands) == len(processes) == 1
        assert "config" in commands[0][0] and "status" not in commands[0][0]
        assert len(opened) == 1 and opened[0] < commands[0][1]
        assert processes[0].returncode == -9
        assert processes[0].stdout.closed and processes[0].stderr.closed
        with controller_registry.store.connect() as connection:
            record = connection.execute(
                "SELECT data FROM controller_operations"
            ).fetchone()["data"]
            assert not co.fenced(connection)
        assert record["state"] == "terminal"
        assert record["terminal_evidence"]["outcome"] == "failed"
        assert (
            commands[0][1] <= record["business_deadline_monotonic_ns"] / 1_000_000_000
        )
        assert len(record["processes"]) == 1
        assert record["processes"][0]["pid"] == processes[0].pid
        assert record["processes"][0]["reaped"] and record["processes"][0]["pipe_eof"]
    finally:
        stop.set()
        writer.join(timeout=2)
    assert not writer.is_alive() and failures == []


def test_config_failure_does_not_print_values(repository, tmp_path):
    included = tmp_path / "malformed-config"
    included.write_text('[filter "secret-value-must-not-appear"\n')
    git(repository, "config", "include.path", str(included))
    with pytest.raises(WorkspaceError, match="cannot be inspected safely") as error:
        _git(repository, "status", "--porcelain=v1", check=False)
    assert "secret-value-must-not-appear" not in str(error.value)


def test_export_still_uses_committed_bytes_without_filters(repository, probe, tmp_path):
    marker, command = probe
    git(repository, "config", "filter.Probe.Driver.clean", command)
    git(repository, "config", "filter.Probe.Driver.smudge", command)
    (repository / "sample.txt").write_text("altered fixture\n")
    workspace = Workspace.create(repository, "HEAD", tmp_path / "task")
    assert (workspace.path / "sample.txt").read_text() == "tracked fixture\n"
    assert (repository / "sample.txt").read_text() == "altered fixture\n"
    assert not marker.exists()


@pytest.mark.parametrize("setting", ["clean", "process"])
def test_api_registration_and_adoption_disable_filters(
    repository, probe, settings, setting
):
    marker, command = probe
    git(repository, "config", f"filter.Probe.Driver.{setting}", command)
    git(repository, "config", "filter.Probe.Driver.required", "true")
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        registered = client.post("/api/projects", json={"path": str(repository)})
        assert registered.status_code == 200, registered.text
        assert registered.json()["dirty"] is False
        run = client.post("/api/runs", json={"prompt": "offline fixture"})
        assert run.status_code == 200, run.text
        (repository / "sample.txt").write_text("altered fixture\n")
        registered = client.post("/api/projects", json={"path": str(repository)})
        assert registered.status_code == 200, registered.text
        assert registered.json()["dirty"] is True
        adoption = client.post(f"/api/runs/{run.json()['id']}/adoption-check")
        assert adoption.status_code == 200
        assert adoption.json()["safe"] is False
        assert "未提交修改" in adoption.json()["reason"]
    assert not marker.exists()
