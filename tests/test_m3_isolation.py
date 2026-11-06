import os
import stat
import subprocess
import tarfile
from uuid import uuid4

import pytest
from doonex.evidence import Evidence, EvidenceError
from doonex.sandbox import Sandbox, SandboxError
from doonex.tasksets import _snapshot
from doonex.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "Isolation fixture"),
        ("config", "user.email", "fixture@example.invalid"),
    ):
        subprocess.check_call(["git", "-C", str(repository), *arguments])
    (repository / "main.py").write_text("answer = 1\n")
    subprocess.check_call(["git", "-C", str(repository), "add", "."])
    subprocess.check_call(["git", "-C", str(repository), "commit", "-qm", "baseline"])
    result = Workspace.create(repository, "HEAD", tmp_path / "workspace")
    (result.path / "main.py").write_text("answer = 2\n")
    return result


@pytest.fixture
def verifier(tmp_path):
    root = tmp_path / "verifier"
    root.mkdir(mode=0o700)
    (root / "fixtures").mkdir(mode=0o700)
    (root / "empty").mkdir(mode=0o700)
    (root / "fixtures/expected.txt").write_text("private checker fixture\n")
    (root / "check.py").write_text(
        "from pathlib import Path\nassert Path('/workspace/main.py').read_text() == 'answer = 2\\n'\n"
    )
    return root


def verification_context(verifier):
    return {
        "taskset_id": "a" * 64,
        "task_id": "task-0",
        "report_sha256": "b" * 64,
        "verifier_digest": _snapshot(verifier, "verifier", private=True)[0][
            "verifier_digest"
        ],
    }


class RecordingSandbox:
    image = "offline-recording-fixture"

    def __init__(self, action=None):
        self.paths = []
        self.action = action

    def run(
        self,
        workspace,
        command,
        timeout,
        run_id,
        should_stop=lambda: False,
        *,
        verifier_path=None,
    ):
        assert verifier_path is not None
        self.paths.append((workspace, verifier_path))
        assert (workspace / "main.py").read_text() == "answer = 2\n"
        assert (
            verifier_path / "fixtures/expected.txt"
        ).read_text() == "private checker fixture\n"
        assert (verifier_path / "empty").is_dir()
        assert stat.S_IMODE(verifier_path.stat().st_mode) == 0o700
        if self.action:
            self.action(workspace, verifier_path)
        return {
            "exit_code": 0,
            "output": "controlled fixture passed\n",
            "timed_out": False,
            "cancelled": False,
            "cleanup_error": None,
        }


def test_each_check_uses_independent_frozen_verifier_and_source_copies(
    workspace, verifier, tmp_path
):
    context = verification_context(verifier)
    sandbox = RecordingSandbox(
        lambda source, checker: (source / "main.py").write_text(
            "polluted by a previous check"
        )
    )
    run_dir = tmp_path / "run"
    report = Evidence.capture(
        workspace,
        run_dir,
        ["first", "second"],
        sandbox,
        10,
        uuid4().hex,
        verifier_path=verifier,
        verification_context=context,
    )
    assert report["verified"]
    assert report["verification_context"] == context
    assert report["verifier_integrity"] == {
        "valid": True,
        "errors": [],
        "verifier_digest": context["verifier_digest"],
    }
    assert len({str(path) for _, path in sandbox.paths}) == 2
    assert all(path != verifier and not path.exists() for _, path in sandbox.paths)
    assert (
        verifier / "fixtures/expected.txt"
    ).read_text() == "private checker fixture\n"
    status = Evidence.inspect(
        workspace, run_dir, report["report_sha256"], verification_context=context
    )
    assert status["valid"] and status["verified"]
    with tarfile.open(run_dir / "evidence/snapshot.tar.gz") as archive:
        assert archive.getnames() == ["main.py"]
    assert (
        "private checker fixture" not in (run_dir / "evidence/diff.patch").read_text()
    )
    assert not (run_dir / "evidence/snapshot/check.py").exists()


@pytest.mark.parametrize(
    "change", ["file", "mode", "directory", "directory_mode", "extra", "symlink"]
)
def test_verifier_tampering_before_capture_cannot_run_checks(
    workspace, verifier, tmp_path, change
):
    context = verification_context(verifier)
    if change == "file":
        (verifier / "check.py").write_text("changed")
    elif change == "mode":
        (verifier / "check.py").chmod(0o755)
    elif change == "directory":
        (verifier / "another-empty").mkdir(mode=0o700)
    elif change == "directory_mode":
        (verifier / "fixtures").chmod(0o755)
    elif change == "extra":
        (verifier / "unexpected.txt").write_text("extra")
    else:
        (verifier / "check.py").unlink()
        (verifier / "check.py").symlink_to(workspace.path / "main.py")
    sandbox = RecordingSandbox()
    with pytest.raises(EvidenceError):
        Evidence.capture(
            workspace,
            tmp_path / "run",
            ["check"],
            sandbox,
            10,
            uuid4().hex,
            verifier_path=verifier,
            verification_context=context,
        )
    assert sandbox.paths == []
    assert not (tmp_path / "run/evidence").exists()


@pytest.mark.parametrize("target", ["original", "copy"])
def test_verifier_changes_during_checks_are_recorded_and_cannot_verify(
    workspace, verifier, tmp_path, target
):
    context = verification_context(verifier)

    def mutate(source, checker):
        root = verifier if target == "original" else checker
        (root / "check.py").write_text("changed while checking")

    report = Evidence.capture(
        workspace,
        tmp_path / "run",
        ["check"],
        RecordingSandbox(mutate),
        10,
        uuid4().hex,
        verifier_path=verifier,
        verification_context=context,
    )
    assert not report["verified"]
    assert not report["verifier_integrity"]["valid"]
    assert report["verifier_integrity"]["errors"]
    status = Evidence.inspect(
        workspace,
        tmp_path / "run",
        report["report_sha256"],
        verification_context=context,
    )
    assert not status["verified"] and not status["valid"]


@pytest.mark.parametrize(
    "case",
    [
        "missing_path",
        "missing_context",
        "missing_field",
        "unknown_field",
        "bad_hash",
        "bad_task",
    ],
)
def test_verifier_requires_complete_trusted_context(
    workspace, verifier, tmp_path, case
):
    context = verification_context(verifier)
    path = verifier
    if case == "missing_path":
        path = None
    elif case == "missing_context":
        context = None
    elif case == "missing_field":
        del context["taskset_id"]
    elif case == "unknown_field":
        context["extra"] = "unsupported"
    elif case == "bad_hash":
        context["report_sha256"] = "bad"
    else:
        context["task_id"] = "../outside"
    with pytest.raises(EvidenceError):
        Evidence.capture(
            workspace,
            tmp_path / "run",
            ["check"],
            RecordingSandbox(),
            10,
            uuid4().hex,
            verifier_path=path,
            verification_context=context,
        )


def test_verification_report_cannot_be_reused_for_another_context(
    workspace, verifier, tmp_path
):
    context = verification_context(verifier)
    report = Evidence.capture(
        workspace,
        tmp_path / "run",
        ["check"],
        RecordingSandbox(),
        10,
        uuid4().hex,
        verifier_path=verifier,
        verification_context=context,
    )
    other = {**context, "task_id": "task-other"}
    status = Evidence.inspect(
        workspace, tmp_path / "run", report["report_sha256"], verification_context=other
    )
    assert not status["verified"] and not status["valid"]


@pytest.mark.parametrize(
    "case",
    [
        "both",
        "same",
        "inside",
        "parent",
        "symlink",
        "parent_symlink",
        "file_symlink",
        "fifo",
        "secret",
        "mount_injection",
    ],
)
def test_invalid_extra_mounts_are_rejected_before_docker(
    workspace, tmp_path, monkeypatch, case
):
    sandbox = Sandbox()
    monkeypatch.setattr(
        sandbox,
        "_docker",
        lambda *args, **kwargs: pytest.fail("Unsafe mount reached Docker"),
    )
    extra = tmp_path / "skill"
    extra.mkdir()
    (extra / "SKILL.md").write_text("Use the workspace.")
    arguments = {"skill_path": extra}
    if case == "both":
        arguments["verifier_path"] = extra
    elif case == "same":
        arguments["skill_path"] = workspace.path
    elif case == "inside":
        inside = workspace.path / "skill"
        inside.mkdir()
        arguments["skill_path"] = inside
    elif case == "parent":
        arguments["skill_path"] = workspace.path.parent
    elif case == "symlink":
        linked = tmp_path / "linked"
        linked.symlink_to(extra, target_is_directory=True)
        arguments["skill_path"] = linked
    elif case == "parent_symlink":
        linked = tmp_path / "linked"
        linked.symlink_to(tmp_path, target_is_directory=True)
        arguments["skill_path"] = linked / extra.name
    elif case == "file_symlink":
        (extra / "linked").symlink_to(workspace.path / "main.py")
    elif case == "fifo":
        os.mkfifo(extra / "fifo")
    elif case == "secret":
        (extra / "access-token").write_text("synthetic-private-token")
    else:
        injected = tmp_path / "skill,dst=/outside"
        injected.mkdir(parents=True)
        arguments["skill_path"] = injected
    with pytest.raises(SandboxError):
        sandbox.run(workspace.path, "true", 10, uuid4().hex, **arguments)


@pytest.mark.docker
def test_model_shell_skill_readonly_and_hidden_verifier_absent(
    workspace, tmp_path, monkeypatch
):
    skill = tmp_path / "skill"
    skill.mkdir(mode=0o700)
    (skill / "SKILL.md").write_text("Use the controlled fixture.")
    (skill / "scripts").mkdir(mode=0o700)
    (skill / "scripts/check.py").write_text("print('explicit skill asset')\n")
    secret = tmp_path / "credentials.json"
    secret.write_text('{"synthetic": "must-not-enter-container"}')
    monkeypatch.setenv("DOONEX_LLM_API_KEY", "synthetic-must-not-enter-container")
    script = f"""import errno, os
from pathlib import Path
assert Path('/skills/SKILL.md').read_text() == 'Use the controlled fixture.'
assert not Path('/verifier').exists()
assert not Path({str(secret)!r}).exists()
assert 'DOONEX_LLM_API_KEY' not in os.environ
for action in [lambda: Path('/skills/SKILL.md').write_text('bad'), lambda: Path('/skills/SKILL.md').unlink(), lambda: Path('/skills/new').write_text('bad')]:
    try:
        action()
        raise AssertionError('skill write succeeded')
    except OSError as error:
        assert error.errno == errno.EROFS
print('model boundary passed')
"""
    sandbox = Sandbox()
    run_id = uuid4().hex
    result = sandbox.run(
        workspace.path,
        "python - <<'PY'\n" + script + "\nPY",
        10,
        run_id,
        skill_path=skill,
    )
    assert (
        result["exit_code"] == 0 and result["output"].strip() == "model boundary passed"
    ), result
    assert sandbox.containers(run_id) == []
    # Explicit container execution can read a Skill script; import did not run it.
    result = sandbox.run(
        workspace.path, "python /skills/scripts/check.py", 10, run_id, skill_path=skill
    )
    assert (
        result["exit_code"] == 0 and result["output"].strip() == "explicit skill asset"
    ), result


@pytest.mark.docker
def test_final_checks_only_mount_readonly_verifier_and_bind_evidence(
    workspace, verifier, tmp_path, monkeypatch
):
    secret = tmp_path / "credentials.json"
    secret.write_text('{"synthetic": "must-not-enter-container"}')
    monkeypatch.setenv("DOONEX_LLM_API_KEY", "synthetic-must-not-enter-container")
    checker = f"""import errno, os
from pathlib import Path
assert Path('/workspace/main.py').read_text() == 'answer = 2\\n'
assert Path('/verifier/fixtures/expected.txt').read_text() == 'private checker fixture\\n'
assert not Path('/skills').exists()
assert not Path({str(secret)!r}).exists()
assert 'DOONEX_LLM_API_KEY' not in os.environ
for action in [lambda: Path('/verifier/check.py').write_text('bad'), lambda: Path('/verifier/check.py').unlink(), lambda: Path('/verifier/new').mkdir()]:
    try:
        action()
        raise AssertionError('verifier write succeeded')
    except OSError as error:
        assert error.errno == errno.EROFS
print('private verifier passed')
"""
    (verifier / "check.py").write_text(checker)
    context = verification_context(verifier)
    run_id = uuid4().hex
    report = Evidence.capture(
        workspace,
        tmp_path / "run",
        ["python /verifier/check.py"],
        Sandbox(),
        10,
        run_id,
        verifier_path=verifier,
        verification_context=context,
    )
    assert report["verified"], report
    assert report["verification_context"] == context
    assert Evidence.inspect(
        workspace,
        tmp_path / "run",
        report["report_sha256"],
        verification_context=context,
    )["verified"]
    with tarfile.open(tmp_path / "run/evidence/snapshot.tar.gz") as archive:
        assert archive.getnames() == ["main.py"]
    assert Sandbox().containers(run_id) == []
