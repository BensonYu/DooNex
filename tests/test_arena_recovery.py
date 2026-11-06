"""Recovery keeps unknown reservations and separates human-assisted results."""

import pytest
from doonex.config import Settings
from doonex.evidence import Evidence
from doonex.sandbox import Sandbox
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from doonex.workspace import Workspace
from test_m3_pipeline import prepare
from test_worker import response

pytestmark = pytest.mark.docker


@pytest.fixture
def evaluation(settings, store, tmp_path):
    model = settings.model.model_copy(
        update={"model_id": "gpt-4.1-mini", "max_input_tokens": 16384}
    )
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    arena, batch, _ = prepare(settings, store, tmp_path)
    return settings, arena, batch, batch["attempts"][0]["run_id"]


def interrupt_model(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("Synthetic transport interruption")

    monkeypatch.setattr(GuardedLLM, "_transport_call", interrupted)


def replies(monkeypatch, *items):
    remaining = list(items)

    def transport(*args, **kwargs):
        assert remaining, "Unexpected extra model call"
        item = remaining.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    return remaining


def workspace(settings, run_id):
    return run_directory(settings, run_id) / "workspace"


def attempt(arena, batch, run_id):
    return next(
        a for a in arena.detail(batch["id"])["attempts"] if a["run_id"] == run_id
    )


@pytest.mark.parametrize("answer", [42, 41])
def test_manual_edit_on_resume_never_counts_as_autonomous(
    evaluation, store, monkeypatch, answer
):
    settings, arena, batch, run_id = evaluation
    interrupt_model(monkeypatch)
    Worker(settings).run(once=True)
    before = store.get(run_id)
    assert before["state"] == "interrupted"
    assert before["workspace_checkpoint_digest"]
    assert (
        Workspace.snapshot_digest(workspace(settings, run_id))
        == before["baseline_digest"]
    )
    (workspace(settings, run_id) / "hello.py").write_text(f"answer = {answer}\n")
    arena.resume(batch["id"], True)
    resumed = store.get(run_id)
    assert resumed["arena_assisted"] is True and resumed["arena_assistance"]
    assert resumed["first_started_at"] == before["first_started_at"]
    assert resumed["usage"] == before["usage"]
    assert not store.pending(run_id)
    remaining = replies(monkeypatch, response("finish", {"message": "Finished"}, 1))
    Worker(settings).run(once=True)
    result = attempt(arena, batch, run_id)
    assert not remaining, result
    assert result["state"] == ("completed_verified" if answer == 42 else "failed")
    assert result["classification"] == "assisted"
    assert result["evidence_valid"] is True and result["assistance"]
    detail = arena.detail(batch["id"])
    assert detail["metrics"]["counts"]["assisted"] == 1
    assert detail["metrics"]["counts"]["valid"] == 0
    assert detail["metrics"]["counts"]["passed"] == 0
    assert all(not values for values in detail["metrics"]["recommendations"].values())
    assert detail["usage"]["unknown_calls"] == result["usage"]["unknown_calls"] == 1
    assert result["usage"]["reserved_tokens"] == before["usage"]["reserved_tokens"]


def test_unchanged_workspace_with_only_unknown_model_remains_autonomous(
    evaluation, store, monkeypatch
):
    settings, arena, batch, run_id = evaluation
    remaining = replies(
        monkeypatch,
        response("doonex_shell", {"command": "printf 'answer = 42\\n' > hello.py"}, 0),
        KeyboardInterrupt("Synthetic next request interruption"),
    )
    Worker(settings).run(once=True)
    before = store.get(run_id)
    assert not remaining and before["state"] == "interrupted"
    assert before["workspace_checkpoint_digest"]
    assert (workspace(settings, run_id) / "hello.py").read_text() == "answer = 42\n"
    pending = store.pending(run_id)
    assert len(pending) == 1 and pending[0]["kind"] == "model"
    assert pending[0]["status"] == "unknown"
    assert before["usage"]["calls"] == 2 and before["usage"]["unknown_calls"] == 1
    assert before["usage"]["reserved_tokens"] > 0
    arena.resume(batch["id"], True)
    resumed = store.get(run_id)
    assert not resumed.get("arena_assisted")
    assert resumed["usage"] == before["usage"]
    assert resumed["first_started_at"] == before["first_started_at"]
    remaining = replies(monkeypatch, response("finish", {"message": "Finished"}, 2))
    Worker(settings).run(once=True)
    result = attempt(arena, batch, run_id)
    assert not remaining, result
    assert result["state"] == "completed_verified"
    assert result["classification"] == "passed" and result["evidence_valid"]
    assert not result["assistance"]
    assert result["usage"]["unknown_calls"] == 1
    assert result["usage"]["reserved_tokens"] == before["usage"]["reserved_tokens"]
    detail = arena.detail(batch["id"])
    assert detail["metrics"]["counts"]["valid"] == 1
    assert detail["metrics"]["counts"]["assisted"] == 0
    assert detail["usage"] == store.get(run_id)["usage"]


@pytest.mark.parametrize(
    "change", ["generated_file", "empty_directory", "directory_mode"]
)
def test_export_exclusions_do_not_hide_intervention_on_resume(
    evaluation, store, monkeypatch, change
):
    settings, arena, batch, run_id = evaluation
    interrupt_model(monkeypatch)
    Worker(settings).run(once=True)
    root = workspace(settings, run_id)
    exported_digest = Workspace.snapshot_digest(root)
    command = "printf 'answer = 42\\n' > hello.py"
    if change == "generated_file":
        helper = root / "__pycache__" / "manual_fix.txt"
        helper.parent.mkdir(mode=0o700)
        helper.write_text("answer = 42\n")
        command = "cp __pycache__/manual_fix.txt hello.py"
    elif change == "empty_directory":
        (root / "answer-is-42").mkdir(mode=0o700)
        command = (
            'python -B -c "from pathlib import Path; '
            "hint = next(Path('.').glob('answer-is-*')).name.rsplit('-', 1)[-1]; "
            "Path('hello.py').write_text('answer = ' + hint + '\\n')\""
        )
    else:
        assert root.stat().st_mode & 0o777 == 0o700
        root.chmod(0o750)
    # Export exclusion does not make these files invisible to the executor.
    assert Workspace.snapshot_digest(root) == exported_digest
    arena.resume(batch["id"], True)
    remaining = replies(
        monkeypatch,
        response("doonex_shell", {"command": command}, 1),
        response("finish", {"message": "Finished"}, 2),
    )
    Worker(settings).run(once=True)
    result = attempt(arena, batch, run_id)
    assert not remaining, result
    assert result["state"] == "completed_verified" and result["evidence_valid"]
    assert store.get(run_id)["arena_assisted"] is True
    assert result["classification"] == "assisted"
    assert arena.detail(batch["id"])["metrics"]["counts"]["valid"] == 0


@pytest.mark.parametrize("shell_status", ["pending", "unknown"])
def test_unresolved_shell_is_assisted_even_when_files_match_checkpoint(
    evaluation, store, monkeypatch, shell_status
):
    settings, arena, batch, run_id = evaluation
    original_run = Sandbox.run

    def interrupted_after_container(self, *args, **kwargs):
        original_run(self, *args, **kwargs)
        raise KeyboardInterrupt("Synthetic crash before recording tool result")

    monkeypatch.setattr(Sandbox, "run", interrupted_after_container)
    remaining = replies(
        monkeypatch, response("doonex_shell", {"command": "python -B -c 'pass'"}, 0)
    )
    Worker(settings).run(once=True)
    assert not remaining
    run = store.get(run_id)
    assert run["state"] == "interrupted"
    assert run["workspace_checkpoint_digest"]
    assert (
        Workspace.snapshot_digest(workspace(settings, run_id)) == run["baseline_digest"]
    )
    pending = store.pending(run_id)
    assert len(pending) == 1 and pending[0]["kind"] == "shell"
    assert pending[0]["status"] == "unknown"
    if shell_status == "pending":
        # Exercise a persisted action from before startup reconciliation.
        with store.connect() as connection:
            connection.execute(
                "UPDATE actions SET status='pending' WHERE id=%s", (pending[0]["id"],)
            )
    arena.resume(batch["id"], True)
    resumed = store.get(run_id)
    assert resumed["arena_assisted"] is True and resumed["arena_assistance"]
    assert resumed["usage"] == run["usage"]
    monkeypatch.setattr(Sandbox, "run", original_run)
    remaining = replies(
        monkeypatch,
        response("doonex_shell", {"command": "printf 'answer = 42\\n' > hello.py"}, 1),
        response("finish", {"message": "Finished"}, 2),
    )
    Worker(settings).run(once=True)
    result = attempt(arena, batch, run_id)
    assert not remaining
    assert result["state"] == "completed_verified"
    assert result["classification"] == "assisted" and result["evidence_valid"]


def test_assistance_is_sticky_across_a_second_unchanged_resume(
    evaluation, store, monkeypatch
):
    settings, arena, batch, run_id = evaluation
    interrupt_model(monkeypatch)
    Worker(settings).run(once=True)
    (workspace(settings, run_id) / "hello.py").write_text("answer = 42\n")
    arena.resume(batch["id"], True)
    first = store.get(run_id)
    assert first["arena_assisted"] is True
    remaining = replies(
        monkeypatch,
        response("doonex_shell", {"command": "python -B -c 'pass'"}, 1),
        KeyboardInterrupt("Synthetic second model interruption"),
    )
    Worker(settings).run(once=True)
    assert not remaining
    before = store.get(run_id)
    assert before["state"] == "interrupted"
    assert before["workspace_checkpoint_digest"] != first["workspace_checkpoint_digest"]
    assert all(action["kind"] == "model" for action in store.pending(run_id))
    arena.resume(batch["id"], True)
    after = store.get(run_id)
    assert after["arena_assisted"] is True
    assert after["arena_assistance"] == first["arena_assistance"]
    assert after["usage"] == before["usage"]
    assert after["usage"]["unknown_calls"] == 2
    assert after["first_started_at"] == first["first_started_at"]
    assert after["assignments"]["executor"]["session_generation"] == 2


@pytest.mark.parametrize("failure_kind", ["assertion", "timeout"])
def test_observed_check_failure_survives_interruption_and_later_assistance(
    evaluation, store, monkeypatch, failure_kind
):
    settings, arena, batch, run_id = evaluation
    original_inspect = Evidence.inspect
    original_run = Sandbox.run

    def interrupted_inspection(*args, **kwargs):
        raise KeyboardInterrupt("Synthetic crash after capturing failed checks")

    monkeypatch.setattr(Evidence, "inspect", interrupted_inspection)
    if failure_kind == "timeout":

        def bounded_verifier(self, path, command, timeout, run_id, *args, **kwargs):
            if kwargs.get("verifier_path"):
                timeout = 0.1
            return original_run(self, path, command, timeout, run_id, *args, **kwargs)

        monkeypatch.setattr(Sandbox, "run", bounded_verifier)
        remaining = replies(
            monkeypatch,
            response(
                "doonex_shell",
                {
                    "command": "printf 'import time\\ntime.sleep(30)\\nanswer = 1\\n' > hello.py"
                },
                0,
            ),
            response("finish", {"message": "Finished"}, 1),
        )
    else:
        remaining = replies(monkeypatch, response("finish", {"message": "Finished"}, 0))
    Worker(settings).run(once=True)
    assert not remaining
    before = store.get(run_id)
    assert before["state"] == "interrupted" and before["phase"] == "verification"
    failure = before["arena_observed_failure"]
    assert failure["report_sha256"] == before["evidence"]["report_sha256"]
    assert before["evidence"]["checks"][0]["exit_code"] != 0
    assert before["evidence"]["checks"][0]["timed_out"] == (failure_kind == "timeout")
    assert before["assignments"]["executor"]["state"] == "completed"
    monkeypatch.setattr(Evidence, "inspect", original_inspect)
    monkeypatch.setattr(Sandbox, "run", original_run)
    (workspace(settings, run_id) / "hello.py").write_text("answer = 42\n")
    arena.resume(batch["id"], True)
    assert store.get(run_id)["arena_assisted"] is True

    def forbidden_model(*args, **kwargs):
        pytest.fail("Completed execution must not be replayed during verification")

    monkeypatch.setattr(GuardedLLM, "_transport_call", forbidden_model)
    Worker(settings).run(once=True)
    final = store.get(run_id)
    result = attempt(arena, batch, run_id)
    assert final["state"] == "completed_verified" and result["evidence_valid"]
    assert final["arena_observed_failure"] == failure
    assert final["evidence"]["report_sha256"] != failure["report_sha256"]
    assert list(run_directory(settings, run_id).glob("evidence-attempt-*"))
    assert result["classification"] == "failed" and result["regression_failed"]
    detail = arena.detail(batch["id"])
    assert detail["metrics"]["counts"]["valid"] == 1
    assert detail["metrics"]["counts"]["failed"] == 1
    assert detail["metrics"]["counts"]["assisted"] == 0


def test_safety_failure_is_not_hidden_by_an_assistance_marker(
    evaluation, store, monkeypatch
):
    settings, arena, batch, run_id = evaluation
    remaining = replies(
        monkeypatch, response("doonex_shell", {"command": "cat /verifier/check.py"}, 0)
    )
    Worker(settings).run(once=True)
    assert not remaining and store.get(run_id)["state"] == "failed"
    with store.locked(run_id) as (_, current):
        current["arena_assisted"] = True
    result = attempt(arena, batch, run_id)
    assert result["classification"] == "failed" and result["safety_violation"]
    assert arena.detail(batch["id"])["metrics"]["counts"]["assisted"] == 0
