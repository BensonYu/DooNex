"""Short worker transactions with real PostgreSQL and bounded Arena reads."""

from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import psycopg
import pytest
from doonex.arena import Arena
from doonex.budget import Ledger
from doonex.sandbox import Sandbox
from doonex.sdk import GuardedLLM
from doonex.store import OwnershipLost, Store
from doonex.worker import Worker, run_directory
from doonex.workspace import Workspace, WorkspaceError
from psycopg.types.json import Jsonb
from test_arena_recovery import evaluation as evaluation_fixture
from test_worker import response, scripted_model

evaluation = evaluation_fixture
pytestmark = pytest.mark.postgres


def shell_actions(store, run_id):
    with store.connect() as c:
        return c.execute(
            "SELECT status,data FROM actions WHERE run_id=%s AND kind='shell'",
            (run_id,),
        ).fetchall()


@pytest.mark.docker
@pytest.mark.parametrize("stop", ["pause", "cancel"])
def test_stop_commits_while_shell_checkpoint_is_computed(
    evaluation, store, monkeypatch, stop
):
    settings, _, _, run_id = evaluation
    path = run_directory(settings, run_id) / "workspace"
    original_digest = Workspace.execution_digest
    changed = []

    def digest(workspace):
        if (workspace / "digest-ready").exists() and not changed:
            # A second connection must acquire the Run while the scan is active.
            # The old lock-held scan times out here instead of accepting stop.
            with store.connect() as c:
                c.execute("SET LOCAL lock_timeout='200ms'")
                current = c.execute(
                    "SELECT data FROM runs WHERE id=%s FOR UPDATE", (run_id,)
                ).fetchone()["data"]
                assert shell_actions(store, run_id)[0]["status"] == "pending"
                current.update(state="pausing", requested_action=stop)
                c.execute(
                    "UPDATE runs SET state=%s,data=%s WHERE id=%s",
                    (current["state"], Jsonb(current), run_id),
                )
            changed.append(True)
        return original_digest(workspace)

    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(digest))
    calls = scripted_model(
        monkeypatch, ["printf 'answer = 42\\n' > hello.py; touch digest-ready"]
    )
    Worker(settings).run(once=True)
    final = store.get(run_id)
    assert changed and final["state"] == ("paused" if stop == "pause" else "cancelled")
    assert final["requested_action"] == stop
    assert final["workspace_checkpoint_digest"] == original_digest(path)
    assert len(calls) == final["usage"]["calls"] == 1
    assert final["usage"]["unknown_calls"] == final["usage"]["reserved_tokens"] == 0
    assert shell_actions(store, run_id)[0]["status"] == "completed"


@pytest.mark.docker
def test_shell_checkpoint_failure_keeps_action_unknown(evaluation, store, monkeypatch):
    settings, arena, batch, run_id = evaluation
    original_digest = Workspace.execution_digest
    prior = []

    def digest(workspace):
        if (workspace / "digest-ready").exists():
            prior.append(store.get(run_id)["workspace_checkpoint_digest"])
            assert shell_actions(store, run_id)[0]["status"] == "pending"
            raise WorkspaceError("Synthetic checkpoint scan failure")
        return original_digest(workspace)

    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(digest))
    calls = scripted_model(monkeypatch, ["touch digest-ready"])
    Worker(settings).run(once=True)
    final = store.get(run_id)
    assert final["state"] == "interrupted" and prior
    assert final["workspace_checkpoint_digest"] == prior[0]
    assert shell_actions(store, run_id)[0]["status"] == "unknown"
    assert not any(e["kind"] == "tool_finished" for e in store.events(run_id)["events"])
    assert len(calls) == 1
    with pytest.raises(ValueError, match="明确确认恢复"):
        arena.resume(batch["id"], False)
    assert store.get(run_id)["state"] == "interrupted" and len(calls) == 1
    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(original_digest))
    arena.resume(batch["id"], True)
    resumed = store.get(run_id)
    assert resumed["state"] == "queued" and resumed["arena_assisted"] is True
    assert resumed["usage"] == final["usage"]
    assert resumed["first_started_at"] == final["first_started_at"]
    assert not store.pending(run_id)


@pytest.mark.docker
def test_unknown_shell_stops_remaining_tools_and_finish_in_same_response(
    evaluation, store, monkeypatch
):
    settings, _, _, run_id = evaluation
    original_digest = Workspace.execution_digest

    def digest(workspace):
        if (workspace / "digest-ready").exists():
            raise WorkspaceError("Synthetic checkpoint scan failure")
        return original_digest(workspace)

    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(digest))
    answer = response("doonex_shell", {"command": "touch digest-ready"}, 0)
    for index, tool, arguments in (
        (1, "doonex_shell", {"command": "touch must-not-run"}),
        (2, "finish", {"message": "Must not finish after unknown shell"}),
    ):
        answer.choices[0].message.tool_calls.extend(
            response(tool, arguments, index).choices[0].message.tool_calls
        )
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        assert len(calls) == 1, "No model dispatch after the unknown shell"
        return answer

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    Worker(settings).run(once=True)
    final = store.get(run_id)
    assert final["state"] == final["assignments"]["executor"]["state"] == "interrupted"
    assert not (run_directory(settings, run_id) / "workspace" / "must-not-run").exists()
    actions = shell_actions(store, run_id)
    assert len(actions) == 1 and actions[0]["status"] == "unknown"
    assert len(calls) == final["usage"]["calls"] == 1
    assert not final.get("evidence")
    assert not any(
        e["kind"] == "sdk"
        and e["payload"].get("kind") == "ObservationEvent"
        and e["payload"].get("tool_name") == "finish"
        for e in store.events(run_id)["events"]
    )


@pytest.mark.docker
@pytest.mark.parametrize("boundary", ["before_intent", "intent_commit", "remaining"])
def test_unconfirmed_shell_admission_never_dispatches_tool_or_next_model(
    evaluation, store, monkeypatch, boundary
):
    settings, _, _, run_id = evaluation
    original_intent, original_get = Ledger.tool_intent, Store.get
    fail_remaining = []

    def intent(ledger, *args, **kwargs):
        if boundary == "before_intent":
            raise psycopg.OperationalError("Synthetic admission connection failure")
        action_id = original_intent(ledger, *args, **kwargs)
        if boundary == "intent_commit":
            # The durable transaction committed, but the caller never gets its ID.
            raise psycopg.OperationalError(
                "Synthetic lost intent commit acknowledgement"
            )
        fail_remaining.append(True)
        return action_id

    def get(current_store, identifier):
        if identifier == run_id and fail_remaining:
            fail_remaining.clear()
            raise psycopg.OperationalError("Synthetic post-intent budget read failure")
        return original_get(current_store, identifier)

    monkeypatch.setattr(Ledger, "tool_intent", intent)
    monkeypatch.setattr(Store, "get", get)
    monkeypatch.setattr(
        Sandbox, "run", lambda *_a, **_k: pytest.fail("Tool dispatched")
    )
    calls = scripted_model(monkeypatch, ["touch must-not-run"])
    Worker(settings).run(once=True)
    final = store.get(run_id)
    assert final["state"] == "interrupted"
    assert len(calls) == final["usage"]["calls"] == 1
    actions = shell_actions(store, run_id)
    if boundary == "before_intent":
        assert actions == [] and store.pending(run_id) == []
    else:
        assert len(actions) == 1 and actions[0]["status"] == "unknown"
    assert not any(e["kind"] == "tool_finished" for e in store.events(run_id)["events"])


@pytest.mark.docker
@pytest.mark.parametrize("failure", ["unknown_record", "lease"])
def test_shell_recovery_record_failure_cannot_become_sdk_tool_error(
    evaluation, store, monkeypatch, failure
):
    settings, _, _, run_id = evaluation
    original_digest = Workspace.execution_digest
    original_finish, original_lease = Store.finish_action, Worker.lease
    failed_scan, injected = [], []

    def digest(workspace):
        if (workspace / "digest-ready").exists():
            failed_scan.append(True)
            raise WorkspaceError("Synthetic checkpoint scan failure")
        return original_digest(workspace)

    def finish(current_store, action_id, status, *args, **kwargs):
        if failure == "unknown_record" and status == "unknown" and not injected:
            injected.append(True)
            raise psycopg.OperationalError("Synthetic unknown result record failure")
        return original_finish(current_store, action_id, status, *args, **kwargs)

    def lease(worker):
        if failure == "lease" and failed_scan and not injected:
            injected.append(True)
            raise OwnershipLost("Synthetic lease failure while recording unknown")
        return original_lease(worker)

    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(digest))
    monkeypatch.setattr(Store, "finish_action", finish)
    monkeypatch.setattr(Worker, "lease", lease)
    calls = scripted_model(monkeypatch, ["touch digest-ready"])
    Worker(settings).run(once=True)
    assert injected and store.get(run_id)["state"] == "interrupted"
    assert len(calls) == 1
    assert shell_actions(store, run_id)[0]["status"] == "unknown"
    assert not any(e["kind"] == "tool_finished" for e in store.events(run_id)["events"])


@pytest.mark.docker
def test_lost_completion_acknowledgement_preserves_durable_completed_action(
    evaluation, store, monkeypatch
):
    settings, _, _, run_id = evaluation
    original_locked = Store.locked
    injected = []

    @contextmanager
    def locked(current_store, identifier, *args, **kwargs):
        with original_locked(current_store, identifier, *args, **kwargs) as result:
            yield result
        if identifier == run_id and not injected:
            actions = shell_actions(store, run_id)
            if actions and actions[0]["status"] == "completed":
                injected.append(True)
                raise psycopg.OperationalError(
                    "Synthetic lost completion acknowledgement"
                )

    monkeypatch.setattr(Store, "locked", locked)
    calls = scripted_model(monkeypatch, ["touch completion-durable"])
    Worker(settings).run(once=True)
    final = store.get(run_id)
    actions = shell_actions(store, run_id)
    assert injected and final["state"] == "interrupted"
    assert len(calls) == final["usage"]["calls"] == 1
    assert len(actions) == 1 and actions[0]["status"] == "completed"
    assert actions[0]["data"]["result"]["exit_code"] == 0
    assert not store.pending(run_id)
    assert final["workspace_checkpoint_digest"] == Workspace.execution_digest(
        run_directory(settings, run_id) / "workspace"
    )
    assert (
        sum(e["kind"] == "tool_finished" for e in store.events(run_id)["events"]) == 1
    )


@pytest.mark.docker
def test_owner_change_during_checkpoint_prevents_old_worker_completion(
    evaluation, store, monkeypatch
):
    settings, _, _, run_id = evaluation
    original_digest = Workspace.execution_digest
    replacement = str(uuid4())
    prior = []

    def digest(workspace):
        if (workspace / "digest-ready").exists() and not prior:
            with store.connect() as c:
                c.execute("SET LOCAL lock_timeout='200ms'")
                current = c.execute(
                    "SELECT data FROM runs WHERE id=%s FOR UPDATE", (run_id,)
                ).fetchone()["data"]
                prior.append(current["workspace_checkpoint_digest"])
                current["owner"] = replacement
                c.execute(
                    "UPDATE runs SET data=%s WHERE id=%s", (Jsonb(current), run_id)
                )
        return original_digest(workspace)

    monkeypatch.setattr(Workspace, "execution_digest", staticmethod(digest))
    calls = scripted_model(monkeypatch, ["touch digest-ready"])
    with pytest.raises(OwnershipLost):
        Worker(settings).run(once=True)
    final = store.get(run_id)
    assert prior and final["owner"] == replacement
    assert final["state"] == "running"
    assert final["workspace_checkpoint_digest"] == prior[0]
    assert shell_actions(store, run_id)[0]["status"] == "pending"
    assert not any(e["kind"] == "tool_finished" for e in store.events(run_id)["events"])
    assert len(calls) == 1


def batch_rows(settings, store, states, *, state="running"):
    identifier = str(uuid4())
    runs = [
        {
            "id": str(uuid4()),
            "state": child_state,
            "request": {"arena": {"evaluation_id": identifier}},
        }
        for child_state in states
    ]
    parent = {
        "id": identifier,
        "state": state,
        "attempts": [{"run_id": run["id"]} for run in runs],
    }
    with store.connect() as c:
        c.execute(
            "INSERT INTO arena_evaluations VALUES(%s,%s,%s)",
            (identifier, state, Jsonb(parent)),
        )
        with c.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO runs VALUES(%s,%s,%s)",
                [(run["id"], run["state"], Jsonb(run)) for run in runs],
            )
    return Arena(settings, store), parent, runs


def replace_parent(store, parent):
    with store.connect() as c:
        c.execute(
            "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
            (parent["state"], Jsonb(parent), parent["id"]),
        )


@pytest.mark.parametrize("count", [1, 400])
def test_sync_reads_only_frozen_child_states_once(settings, store, monkeypatch, count):
    arena, parent, _ = batch_rows(settings, store, ["completed_verified"] * count)
    child_reads = []
    original_execute = psycopg.Connection.execute

    def execute(connection, query, params=None, **kwargs):
        if isinstance(query, str) and "FROM runs WHERE id=ANY" in query:
            child_reads.append(tuple(params[0]))
        return original_execute(connection, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", execute)
    monkeypatch.setattr(store, "get", lambda *_: pytest.fail("Per-child Store.get"))
    arena.sync(parent["id"])
    assert arena.get(parent["id"])["state"] == "complete"
    assert child_reads == [tuple(item["run_id"] for item in parent["attempts"])]


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "empty",
        "invalid_id",
        "wrong_batch",
        "wrong_id",
        "wrong_state",
    ],
)
def test_sync_rejects_invalid_child_plan_or_binding(settings, store, change):
    arena, parent, runs = batch_rows(settings, store, ["completed_verified"])
    child = runs[0]
    if change == "duplicate":
        parent["attempts"] *= 2
    elif change == "empty":
        parent["attempts"] = []
    elif change == "invalid_id":
        parent["attempts"][0]["run_id"] = "not-a-uuid"
    else:
        with store.connect() as c:
            if change == "missing":
                c.execute("DELETE FROM runs WHERE id=%s", (child["id"],))
            else:
                value = deepcopy(child)
                if change == "wrong_batch":
                    value["request"]["arena"]["evaluation_id"] = str(uuid4())
                elif change == "wrong_id":
                    value["id"] = str(uuid4())
                elif change == "wrong_state":
                    value["state"] = "queued"
                c.execute(
                    "UPDATE runs SET data=%s WHERE id=%s", (Jsonb(value), child["id"])
                )
    replace_parent(store, parent)
    with pytest.raises(ValueError, match="评测子运行"):
        arena.sync(parent["id"])
    assert arena.get(parent["id"])["state"] == "running"


def test_sync_rejects_plan_over_supported_capacity(settings, store):
    arena, parent, _ = batch_rows(settings, store, ["completed_verified"] * 401)
    with pytest.raises(ValueError, match="数量无效"):
        arena.sync(parent["id"])
    assert arena.get(parent["id"])["state"] == "running"


@pytest.mark.parametrize(
    ("state", "children", "expected"),
    [
        ("running", ["completed_verified", "failed"], "complete"),
        ("queued", ["completed_verified", "paused"], "interrupted"),
        ("running", ["completed_verified", "interrupted"], "interrupted"),
        ("running", ["completed_verified", "queued"], "running"),
        ("cancelling", ["completed_verified", "paused"], "cancelled"),
        ("cancelling", ["completed_verified", "running"], "cancelling"),
    ],
)
def test_sync_preserves_parent_state_rules(settings, store, state, children, expected):
    arena, parent, _ = batch_rows(settings, store, children, state=state)
    arena.sync(parent["id"])
    assert arena.get(parent["id"])["state"] == expected


@pytest.mark.parametrize("change", ["cancel", "plan"])
def test_sync_rechecks_parent_after_initial_read(settings, store, monkeypatch, change):
    arena, parent, _ = batch_rows(
        settings, store, ["completed_verified", "completed_verified"]
    )
    original_get = arena.get
    changed = []

    def get(identifier, connection=None):
        value = original_get(identifier, connection)
        if connection is None and not changed:
            latest = deepcopy(parent)
            if change == "cancel":
                latest["state"] = "cancelling"
            else:
                latest["attempts"].reverse()
            replace_parent(store, latest)
            changed.append(True)
        return value

    monkeypatch.setattr(arena, "get", get)
    if change == "plan":
        with pytest.raises(ValueError, match="计划已变化"):
            arena.sync(parent["id"])
        assert original_get(parent["id"])["state"] == "running"
    else:
        arena.sync(parent["id"])
        assert original_get(parent["id"])["state"] == "cancelled"
