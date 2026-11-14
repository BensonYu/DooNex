"""Actual Worker.run timelines; only the provider response is synthetic.

These finite local fixtures measure admission commits, not a release latency
promise. Observers always call the real method, including SDK close and Docker.
"""

import json
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import controller_test_support
import pytest
from doonex import sdk
from doonex.arena import Arena, EvaluationInput
from doonex.budget import Ledger
from doonex.discovery import MIT_BODY
from doonex.generation import Generation, GenerationInput, StartInput
from doonex.library import Library
from doonex.sandbox import Sandbox
from doonex.scheduling import Scheduler
from doonex.session_store import SessionFileStore
from doonex.store import Store
from doonex.worker import Worker, run_directory
from doonex.workspace import Workspace
from test_api import repository
from test_scheduling import action_rows, claimed
from test_scheduling import taskset_source as taskset_fixture
from test_scheduling import tuning as tuning_fixture
from test_worker import response

controller_registry = controller_test_support.controller_registry
taskset_source = taskset_fixture
tuning = tuning_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]


@pytest.mark.parametrize(
    "background_stage", ["model", "shell", "verifier", "preparation"]
)
@pytest.mark.parametrize("foreground_kind", ["ordinary", "arena", "generation"])
def test_actual_worker_wait_timeline(
    tuning,
    taskset_source,
    store,
    request_data,
    tmp_path,
    monkeypatch,
    background_stage,
    foreground_kind,
):
    settings, background, background_body, repository_path = tuning
    if background_stage == "verifier":
        source, _, _, checker, _ = taskset_source
        (checker / "verify.py").write_text(
            "from pathlib import Path\nimport sys, time\n"
            "Path('/workspace/verifier-entered').write_text('entered')\n"
            "time.sleep(3)\n"
            "sys.path.insert(0, '/workspace')\nfrom main import answer\n"
            "assert answer == 42\n"
            "Path('/workspace/verifier-finished').write_text('finished')\n"
        )
        taskset = background.library.import_taskset(source)
        background_body["taskset_id"] = taskset["id"]
    settings, _, _, batch = claimed(tuning, store)
    background_id = batch["attempts"][0]["run_id"]
    workspace_path = run_directory(settings, background_id) / "workspace"

    if foreground_kind == "ordinary":
        foreground_source = tmp_path / "foreground-source"
        foreground_source.mkdir()
        repo, git = repository(foreground_source)
        foreground_request = deepcopy(request_data)
        foreground_request.update(
            repo_path=str(repo),
            base_commit=git("rev-parse", "HEAD"),
            checks=["python -c 'assert True'"],
        )
        foreground_request["model"].update(model_id="gpt-4.1-mini")

        def submit():
            return [store.create_run(foreground_request)]

    elif foreground_kind == "arena":
        foreground_arena = Arena(settings, store)
        foreground_input = EvaluationInput(
            **{
                key: background_body[key]
                for key in (
                    "taskset_id",
                    "skill_version_ids",
                    "model_profile_id",
                    "repeats",
                    "run_budget",
                    "batch_budget",
                )
            }
        )

        def submit():
            created = foreground_arena.create(foreground_input)
            return [{"id": attempt["run_id"]} for attempt in created["attempts"]]

    else:
        # setup_generation forbids every TaskSet filesystem read globally, so
        # use an actual licensed synthetic parent with the real tuning taskset.
        library = Library(settings, store)
        parent_path = tmp_path / "generation-parent"
        parent_path.mkdir()
        (parent_path / "SKILL.md").write_text(
            "---\nname: timeline-parent\nlicense: LICENSE\n---\nInspect carefully.\n"
        )
        (parent_path / "LICENSE").write_text(
            "MIT License\n\nCopyright (c) 2026 Synthetic Fixture Authors\n\n"
            + MIT_BODY
            + "\n"
        )
        parent = library.import_skill(
            parent_path,
            {
                "source": "synthetic-worker-timeline-parent",
                "license_status": "declared",
                "license_text": "MIT",
            },
        )
        library.review(parent["version_id"], "approved", "Synthetic test fixture")
        project = store.set_project(
            {
                "id": str(uuid4()),
                "name": "Synthetic generation timeline",
                "path": str(repository_path),
                "head": "0" * 40,
                "dirty": False,
            }
        )
        generation = Generation(settings, store)
        draft = generation.create(
            GenerationInput(
                project_id=project["id"],
                parent_version_id=parent["version_id"],
                taskset_id=background_body["taskset_id"],
                task_ids=["task-0"],
                model_profile_id="legacy",
                budget=background_body["run_budget"],
            )
        )

        def submit():
            started = generation.start(
                draft["id"],
                StartInput(
                    expected_revision=draft["revision"],
                    confirm_model_usage=True,
                    confirm_derivative_rights=True,
                ),
            )
            return [{"id": started["run_id"]}]

    worker = Worker(settings)
    entered, release, finished = (threading.Event() for _ in range(3))
    errors, observations, calls, order, active, checkpoint = ([] for _ in range(6))
    foreground, selected = [], []
    enqueue = []
    admission = {}
    running = {}
    container_workspaces = {}

    def observe(kind, run_id=None, **details):
        observations.append(
            {"kind": kind, "run_id": run_id, "monotonic": time.monotonic(), **details}
        )

    original_execute = worker.execute
    original_locked = Store.locked
    original_reserve = Ledger.reserve
    original_settle = Ledger.settle
    original_intent = Ledger.tool_intent
    original_finish_action = Store.finish_action
    original_sandbox = Sandbox.run
    original_digest = Workspace.execution_digest
    original_save = Scheduler.save_yield
    original_close = sdk._M1Conversation.close
    original_store_close = SessionFileStore.close

    @contextmanager
    def locked(current_store, run_id, *args, **kwargs):
        with original_locked(current_store, run_id, *args, **kwargs) as value:
            connection, current = value
            prior_state = current["state"]
            prior_checks = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM actions WHERE run_id=%s AND kind='verification'",
                    (run_id,),
                )
            }
            yield value
            new_checks = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM actions WHERE run_id=%s AND kind='verification'",
                    (run_id,),
                )
            } - prior_checks
        # The actual Store context has now exited and committed. An event
        # insert or Store.action return inside it is not a commit timestamp.
        if prior_state == "queued" and current["state"] == "running":
            running.setdefault(run_id, time.monotonic())
            observe(
                "running_commit_confirmed",
                run_id,
                first_started_at=current["first_started_at"],
            )
        for identifier in sorted(new_checks):
            observe(
                "verification_admission_commit_confirmed", run_id, action_id=identifier
            )

    def reserve(ledger, *args, **kwargs):
        value = original_reserve(ledger, *args, **kwargs)
        admission.setdefault(ledger.run_id, time.monotonic())
        observe(
            "model_admission_commit_confirmed", ledger.run_id, action_id=value["id"]
        )
        return value

    def settle(ledger, reservation, actual):
        value = original_settle(ledger, reservation, actual)
        observe(
            "model_settlement_commit_confirmed",
            ledger.run_id,
            action_id=reservation["id"],
        )
        return value

    def intent(ledger, *args, **kwargs):
        value = original_intent(ledger, *args, **kwargs)
        observe("shell_admission_commit_confirmed", ledger.run_id, action_id=value)
        return value

    def finish_action(current_store, action_id, *args, **kwargs):
        value = original_finish_action(current_store, action_id, *args, **kwargs)
        with current_store.connect() as connection:
            action = connection.execute(
                "SELECT run_id,kind,status FROM actions WHERE id=%s", (action_id,)
            ).fetchone()
        observe(
            "action_settlement_commit_confirmed",
            str(action["run_id"]),
            action_id=action_id,
            action_kind=action["kind"],
            status=action["status"],
        )
        return value

    def sandbox_run(sandbox, path, command, timeout, run_id, *args, **kwargs):
        if run_id == background_id:
            stage = "verifier" if kwargs.get("verifier_path") else "shell"
            container_workspaces[stage] = path
        observe("sandbox_enter", run_id, command=command)
        value = original_sandbox(
            sandbox, path, command, timeout, run_id, *args, **kwargs
        )
        assert not value.get("cleanup_unknown")
        observe(
            "sandbox_returned",
            run_id,
            command=command,
            cleanup_evidence=value.get("cleanup_evidence"),
        )
        return value

    def digest(path, **kwargs):
        value = original_digest(path, **kwargs)
        if (
            background_stage == "preparation"
            and path == workspace_path
            and not entered.is_set()
        ):
            observe("background_preparation_digest_returned", background_id)
            entered.set()
            assert release.wait(20), "Foreground did not release preparation gate"
        return value

    def save(scheduler, run_id, *args, **kwargs):
        before = time.monotonic()
        value = original_save(scheduler, run_id, *args, **kwargs)
        saved = store.get(run_id)
        assert saved["state"] == "yielded"
        observe(
            "yield_receipt_commit_confirmed",
            run_id,
            started_monotonic=before,
            workspace_digest=saved["scheduling"]["workspace_digest"],
        )
        return value

    def close(conversation, *args, **kwargs):
        value = original_close(conversation, *args, **kwargs)
        if active:
            observe("sdk_close_returned", active[0])
        return value

    def close_store(session_store, *args, **kwargs):
        value = original_store_close(session_store, *args, **kwargs)
        assert session_store._fd is None
        if active:
            observe("session_store_closed", active[0])
        return value

    def execute(run_id):
        if run_id != background_id:
            assert not store.get(run_id)["request"].get("background"), (
                "A background sibling was selected before the foreground"
            )
        order.append(run_id)
        active[:] = [run_id]
        observe("execute_enter", run_id)
        try:
            original_execute(run_id)
            if run_id == background_id:
                checkpoint.append(store.get(run_id))
                expected = (
                    "completed_verified"
                    if background_stage == "verifier"
                    else "yielded"
                )
                if checkpoint[-1]["state"] != expected:
                    worker.stopping = True
            else:
                selected.append(run_id)
                worker.stopping = True
        finally:
            observe("execute_return", run_id)
            active.clear()

    def transport(*args, **kwargs):
        assert active
        run_id = active[0]
        calls.append(run_id)
        observe("synthetic_transport_enter", run_id)
        if run_id == background_id:
            assert calls.count(run_id) == 1
            if background_stage == "model":
                entered.set()
                assert release.wait(20), "Foreground did not release the model gate"
            command = "printf 'answer = 42\\n' > main.py"
            if background_stage == "model":
                command = "touch must-not-run"
            if background_stage == "shell":
                command = (
                    "printf entered > shell-entered; sleep 3; "
                    "printf finished > shell-finished; " + command
                )
            first = response("doonex_shell", {"command": command}, 0)
            if background_stage == "shell":
                first.choices[0].message.tool_calls.extend(
                    response(
                        "doonex_shell", {"command": "touch sibling-must-not-run"}, 1
                    )
                    .choices[0]
                    .message.tool_calls
                )
            first.choices[0].message.tool_calls.extend(
                response("finish", {"message": "Synthetic background complete"}, 2)
                .choices[0]
                .message.tool_calls
            )
            return first
        assert not store.get(run_id)["request"].get("background")
        if foreground_kind == "arena":
            first = response(
                "doonex_shell", {"command": "printf 'answer = 42\\n' > main.py"}, 10
            )
            first.choices[0].message.tool_calls.extend(
                response("finish", {"message": "Synthetic foreground complete"}, 11)
                .choices[0]
                .message.tool_calls
            )
            return first
        message = (
            json.dumps(
                {
                    "append_text": "Inspect the requested changes and verify the visible checks."
                }
            )
            if foreground_kind == "generation"
            else "Synthetic foreground complete"
        )
        return response("finish", {"message": message}, 12)

    def run():
        try:
            worker.run()
        except BaseException as error:  # noqa: BLE001 - assert actual thread failures
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(Store, "locked", locked)
    monkeypatch.setattr(Ledger, "reserve", reserve)
    monkeypatch.setattr(Ledger, "settle", settle)
    monkeypatch.setattr(Ledger, "tool_intent", intent)
    monkeypatch.setattr(Store, "finish_action", finish_action)
    monkeypatch.setattr(Sandbox, "run", sandbox_run)
    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(digest))
    monkeypatch.setattr(Scheduler, "save_yield", save)
    monkeypatch.setattr(sdk._M1Conversation, "close", close)
    monkeypatch.setattr(SessionFileStore, "close", close_store)
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        if background_stage in {"model", "preparation"}:
            assert entered.wait(20), "Background did not reach its controlled stage"
        else:
            limit = time.monotonic() + 25
            while True:
                assert not finished.is_set(), errors
                assert time.monotonic() < limit, "Real container command did not enter"
                path = container_workspaces.get(background_stage)
                marker = path / (background_stage + "-entered") if path else None
                if marker is not None and marker.exists():
                    break
                time.sleep(0.02)
            assert not marker.with_name(
                marker.name.replace("entered", "finished")
            ).exists()
            observe("real_inflight_marker_observed", background_id)
        before = time.monotonic()
        foreground.extend(submit())
        after = time.monotonic()
        enqueue.append((before, after))
        for item in foreground:
            assert store.get(item["id"])["request"].get("background") is None
            observe("enqueue_commit_confirmed", item["id"])
        release.set()
        assert finished.wait(45), "Worker did not finish the selected foreground"
    finally:
        release.set()
        worker.stopping = True
        thread.join(timeout=20)
        print(
            "worker_wait_matrix_partial="
            + json.dumps(
                {
                    "background_stage": background_stage,
                    "foreground_kind": foreground_kind,
                    "thread_alive": thread.is_alive(),
                    "worker_error_types": [type(error).__name__ for error in errors],
                    "observations": observations,
                },
                sort_keys=True,
            )
        )
    assert not thread.is_alive(), "Actual Worker.run thread did not terminate"
    assert not errors, errors
    assert len(selected) == 1
    foreground_id = selected[0]
    assert foreground_id in {item["id"] for item in foreground}
    assert order == [background_id, foreground_id]
    assert calls == ([foreground_id] if background_stage == "preparation" else order)
    assert len(checkpoint) == 1
    paused = checkpoint[0]
    assert paused["state"] == (
        "completed_verified" if background_stage == "verifier" else "yielded"
    ), paused["reason"]
    assert paused["usage"]["calls"] == (0 if background_stage == "preparation" else 1)
    assert paused["usage"]["reserved_tokens"] == paused["usage"]["unknown_calls"] == 0
    if background_stage != "verifier":
        assert len(paused["scheduling"]["receipt"]["pending_actions"]) == (
            0 if background_stage == "preparation" else 2
        )
        current = store.get(background_id)
        assert current["scheduling"] == paused["scheduling"]
        assert current["usage"] == paused["usage"]
        assert current["first_started_at"] == paused["first_started_at"]
    assert not store.pending(background_id)
    background_actions = action_rows(store, background_id)
    assert [action["kind"] for action in background_actions] == {
        "preparation": [],
        "model": ["model"],
        "shell": ["model", "shell"],
        "verifier": ["model", "shell", "verification"],
    }[background_stage]
    assert all(action["status"] == "completed" for action in background_actions)
    final = store.get(foreground_id)
    assert final["state"] == (
        "completed_unverified"
        if foreground_kind == "generation"
        else "completed_verified"
    ), final["reason"]
    if foreground_kind == "generation":
        assert final["generation_candidate"]["valid"]
    assert final["usage"]["calls"] == 1
    assert final["usage"]["reserved_tokens"] == final["usage"]["unknown_calls"] == 0
    assert not store.pending(foreground_id)
    assert all(
        action["status"] == "completed" for action in action_rows(store, foreground_id)
    )
    assert not (
        run_directory(settings, background_id) / "workspace" / "must-not-run"
    ).exists()
    assert not (workspace_path / "sibling-must-not-run").exists()
    for attempt in batch["attempts"][1:]:
        assert not action_rows(store, attempt["run_id"])
    assert len(enqueue) == 1
    before, after = enqueue[0]
    first_admission = admission[foreground_id]
    assert before <= after
    assert before <= running[foreground_id] <= first_admission
    assert first_admission - before < 45  # finite fixture deadlock guard only
    for run_id in (background_id, foreground_id):
        kinds = [item["kind"] for item in observations if item["run_id"] == run_id]
        if run_id != background_id or background_stage != "preparation":
            assert "model_settlement_commit_confirmed" in kinds
        assert "sdk_close_returned" in kinds and "session_store_closed" in kinds
        assert run_id not in sdk._CALLBACKS
        assert run_id not in sdk._SCHEDULING
        assert run_id not in sdk._SESSION_STORES

    def observed_at(kind, run_id):
        return max(
            item["monotonic"]
            for item in observations
            if item["kind"] == kind and item["run_id"] == run_id
        )

    if background_stage == "model":
        assert after <= observed_at("model_settlement_commit_confirmed", background_id)
    if background_stage != "verifier":
        assert (
            observed_at("yield_receipt_commit_confirmed", background_id)
            <= observed_at("sdk_close_returned", background_id)
            <= observed_at("session_store_closed", background_id)
            <= observed_at("execute_return", background_id)
            <= first_admission
        )
    if background_stage != "preparation":
        assert admission[background_id] <= before
    assert not [
        item
        for item in observations
        if item["run_id"] == background_id
        and item["kind"]
        in {
            "model_admission_commit_confirmed",
            "shell_admission_commit_confirmed",
            "verification_admission_commit_confirmed",
        }
        and item["monotonic"] > before
    ]
    foreground_checks = [
        item
        for item in observations
        if item["run_id"] == foreground_id
        and item["kind"] == "verification_admission_commit_confirmed"
    ]
    assert len(foreground_checks) == (0 if foreground_kind == "generation" else 1)
    if foreground_checks:
        assert first_admission <= foreground_checks[0]["monotonic"]
    print(
        "worker_wait_matrix="
        + json.dumps(
            {
                "background_stage": background_stage,
                "foreground_kind": foreground_kind,
                "enqueue_commit_bracket_monotonic": [before, after],
                "first_model_admission_commit_confirmed_monotonic": first_admission,
                "submit_to_first_admission_seconds": {
                    "lower": max(0, first_admission - after),
                    "upper": first_admission - before,
                },
                "model_transport": "synthetic",
                "observations": observations,
            },
            sort_keys=True,
        )
    )
