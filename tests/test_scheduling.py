"""Real PostgreSQL, SDK and Docker; every provider response is synthetic."""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from doonex.api import create_app
from doonex.arena import Arena
from doonex.background import Background
from doonex.budget import Ledger
from doonex.config import Settings
from doonex.sandbox import Sandbox
from doonex.scheduling import Scheduler
from doonex.sdk import GuardedLLM, SchedulerYield
from doonex.store import dispatch_lock, now
from doonex.worker import Worker, run_directory
from fastapi.testclient import TestClient
from test_tasksets import taskset_source as taskset_fixture
from test_worker import response

taskset_source = taskset_fixture
pytestmark = pytest.mark.postgres


@pytest.fixture
def tuning(settings, store, taskset_source, tmp_path):
    settings = Settings(
        settings.home,
        settings.database_url,
        settings.token,
        settings.model.model_copy(update={"model_id": "gpt-4.1-mini"}),
    )
    background = Background(settings, store)
    source, spec, repo, checker, _ = taskset_source
    spec["split"] = "tuning"
    spec["tasks"][0]["prompt"] = "Set main.answer to 42."
    (checker / "verify.py").write_text(
        "import sys\nsys.path.insert(0, '/workspace')\nfrom main import answer\nassert answer == 42\n"
    )
    (source / "taskset.json").write_text(json.dumps(spec))
    taskset = background.library.import_taskset(source)
    candidates = []
    for i in range(2):
        candidate = tmp_path / f"skill-{i}"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text(
            f"Inspect and verify the authorized change. {i}"
        )
        skill = background.library.import_skill(candidate, {})
        background.library.review(skill["version_id"], "approved", "Synthetic test")
        candidates.append(skill["version_id"])
    clock = datetime.now(UTC)
    body = {
        "taskset_id": taskset["id"],
        "skill_version_ids": candidates,
        "model_profile_id": "legacy",
        "repeats": 1,
        "window_start": clock.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(),
        "window_end": clock.replace(
            hour=23, minute=59, second=59, microsecond=0
        ).isoformat(),
        "run_budget": {
            "max_calls": 5,
            "max_tokens": 200000,
            "max_cost_usd": 2,
            "max_seconds": 600,
        },
        "batch_budget": {
            "max_calls": 20,
            "max_tokens": 2000000,
            "max_cost_usd": 10,
            "max_seconds": 1800,
        },
        "daily_budget": {
            "max_calls": 40,
            "max_tokens": 4000000,
            "max_cost_usd": 20,
            "max_seconds": 3600,
        },
    }
    return settings, background, body, repo


def claimed(tuning, store):
    settings, background, body, _ = tuning
    draft = background.create(body)
    grant = background.enable(draft["id"], draft["revision"])
    batch = Scheduler(settings, store).claim_next()
    assert batch is not None
    grant = background.get(grant["id"])
    assert grant["evaluation_id"] == batch["id"] and grant["state"] == "consumed"
    return settings, background, grant, batch


def foreground(store, request_data):
    return store.create_run(request_data)


def finish_foreground(store, item):
    store.transition(item["id"], "cancelled", "Synthetic user cancels the queued task")


def action_rows(store, run_id):
    with store.connect() as c:
        return c.execute(
            "SELECT kind,status,data FROM actions WHERE run_id=%s ORDER BY data->>'started_at',id",
            (run_id,),
        ).fetchall()


def test_authorization_api_is_explicit_and_queue_does_not_consume_draft(
    tuning, store, request_data
):
    settings, background, body, _ = tuning
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/api/background/authorizations", json=body).status_code == 401
        )
        client.headers["Authorization"] = "Bearer " + settings.token
        result = client.post("/api/background/authorizations", json=body)
        assert result.status_code == 200, result.text
        draft = result.json()
        assert draft["state"] == "draft"
        assert Scheduler(settings, store).claim_next() is None
        assert not Arena(settings, store).list()
        enabled = client.post(
            f"/api/background/authorizations/{draft['id']}/enable",
            json={"expected_revision": draft["revision"]},
        )
        assert enabled.status_code == 200, enabled.text
        user = foreground(store, request_data)
        assert Scheduler(settings, store).claim_next() is None
        assert background.get(draft["id"])["state"] == "enabled"
        finish_foreground(store, user)
        batch = Scheduler(settings, store).claim_next()
        assert batch and len(batch["attempts"]) == 15
        assert Scheduler(settings, store).claim_next() is None
        listing = client.get("/api/background/authorizations").json()["authorizations"][
            0
        ]
        assert listing["evaluation"]["id"] == batch["id"]
        assert (
            client.get("/api/background/days/" + draft["date"]).json()["reserved"][
                "calls"
            ]
            == 20
        )
        assert (
            client.post(
                f"/api/background/authorizations/{draft['id']}/enable",
                json={"expected_revision": draft["revision"]},
            ).status_code
            == 400
        )
        assert Arena(settings, store).recommendations() == {
            "current": [],
            "history": [],
        }


@pytest.mark.parametrize("enqueue_first", [True, False])
@pytest.mark.parametrize("kind", ["model", "tool"])
def test_foreground_enqueue_and_background_dispatch_share_transaction_order(
    tuning, store, request_data, enqueue_first, kind
):
    settings, _, _, batch = claimed(tuning, store)
    arena = Arena(settings, store)
    assert arena.start(batch["id"])
    run_id = batch["attempts"][0]["run_id"]
    with store.locked(run_id) as (_, run):
        run.update(state="running", first_started_at=now(), owner="test")
    ledger = Ledger(
        store, run_id, "test", dispatch_check=Scheduler(settings, store).preflight
    )
    entered = threading.Event()
    finished = threading.Event()
    results = []
    errors = []

    def competing():
        entered.set()
        try:
            if enqueue_first:
                results.append(
                    ledger.reserve(100, 100)
                    if kind == "model"
                    else ledger.tool_intent("true")
                )
            else:
                results.append(foreground(store, request_data))
        except (Exception, SchedulerYield) as error:  # noqa: BLE001 - collect competing thread failure for assertion
            errors.append(error)
        finally:
            finished.set()

    with store.connect() as c:
        dispatch_lock(c)
        if enqueue_first:
            store.create_run(request_data, connection=c)
        else:
            Scheduler(settings, store).preflight(c, store.get(run_id))
            store.action(
                run_id, kind, {"race_test": "intent committed before enqueue"}, c
            )
        thread = threading.Thread(target=competing)
        thread.start()
        assert entered.wait(3)
        assert not finished.wait(0.15), "Competing operation crossed the admission lock"
    thread.join(5)
    assert not thread.is_alive()
    if enqueue_first:
        assert len(errors) == 1 and isinstance(errors[0], SchedulerYield)
        assert not action_rows(store, run_id)
        assert store.get(run_id)["usage"]["calls"] == 0
    else:
        assert not errors and len(results) == 1
        assert len(action_rows(store, run_id)) == 1
        with pytest.raises(SchedulerYield):
            ledger.reserve(100, 100)


@pytest.mark.docker
@pytest.mark.parametrize("arrival", ["model", "tool", "verification"])
def test_sdk_and_verifier_yield_resume_without_replaying_completed_actions(
    tuning, store, request_data, monkeypatch, arrival
):
    settings, background, grant, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]
    user = []
    calls = []
    commands = []
    actual_sandbox = Sandbox.run
    from doonex.evidence import Evidence

    actual_capture = Evidence.capture

    def enqueue():
        if not user:
            user.append(foreground(store, request_data))

    def transport(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            if arrival == "model":
                enqueue()
            return response(
                "doonex_shell",
                {
                    "command": "printf 'answer = 42\\n' > main.py; printf x >> executed.txt"
                },
                0,
            )
        return response("finish", {"message": "Done"}, 1)

    def shell(self, path, command, *args, **kwargs):
        result = actual_sandbox(self, path, command, *args, **kwargs)
        commands.append(command)
        if arrival == "tool" and "executed.txt" in command:
            enqueue()
        return result

    def capture(*args, **kwargs):
        if arrival == "verification":
            enqueue()
        return actual_capture(*args, **kwargs)

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    monkeypatch.setattr(Sandbox, "run", shell)
    monkeypatch.setattr(Evidence, "capture", capture)
    Worker(settings).run(once=True)
    paused = store.get(run_id)
    assert paused["state"] == "yielded", paused["reason"]
    assert paused["usage"]["unknown_calls"] == paused["usage"]["reserved_tokens"] == 0
    assert not store.pending(run_id)
    assert background.day(grant["date"])["reserved"]["calls"] == 20
    if arrival == "model":
        assert not commands
        assert len(paused["scheduling"]["receipt"]["pending_actions"]) == 1
    elif arrival == "tool":
        assert len(commands) == 1 and len(calls) == 1
    else:
        assert len(commands) == 1 and paused["phase"] == "verification"
    finish_foreground(store, user[0])
    Worker(settings).run(once=True)
    final = store.get(run_id)
    assert final["state"] == "completed_verified", final["reason"]
    assert final["first_started_at"] == paused["first_started_at"]
    assert len(calls) == final["usage"]["calls"] == 2
    assert len(commands) == 2
    assert (
        run_directory(settings, run_id) / "workspace" / "executed.txt"
    ).read_text() == "x"
    shell_actions = [r for r in action_rows(store, run_id) if r["kind"] == "shell"]
    assert (
        len(shell_actions) == 1 and shell_actions[0]["data"]["sdk_action"]["action_id"]
    )
    assert all(r["status"] == "completed" for r in action_rows(store, run_id))
    assert Arena(settings, store).recommendations() == {"current": [], "history": []}
    Arena(settings, store).stop(
        batch["id"], "cancelling", "Test stops remaining planned attempts"
    )
    Scheduler(settings, store).maintain()
    day = background.day(grant["date"])
    assert day["reserved"]["calls"] == 0
    assert day["used"]["calls"] == final["usage"]["calls"]
    assert day["used"]["tokens"] == final["usage"]["tokens"]


@pytest.mark.docker
@pytest.mark.parametrize(
    "tamper",
    [
        "workspace",
        "receipt",
        "unknown",
        "clock",
        "usage",
        "role_usage",
        "generation",
        "assignment",
    ],
)
def test_no_automatic_resume_for_drift_missing_receipt_unknown_or_expired_clock(
    tuning, store, request_data, monkeypatch, tamper
):
    settings, background, grant, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]
    user = []
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        user.append(foreground(store, request_data))
        return response(
            "doonex_shell", {"command": "printf dangerous >> executed.txt"}, 0
        )

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    Worker(settings).run(once=True)
    before = store.get(run_id)
    assert before["state"] == "yielded", before["reason"]
    finish_foreground(store, user[0])
    directory = run_directory(settings, run_id)
    if tamper == "workspace":
        (directory / "workspace" / "changed.txt").write_text("external edit")
    else:
        with store.locked(run_id) as (c, current):
            if tamper == "receipt":
                current["scheduling"]["receipt"] = None
            elif tamper == "clock":
                current["first_started_at"] = (
                    datetime.now(UTC) - timedelta(hours=1)
                ).isoformat()
            elif tamper == "usage":
                current["usage"]["calls"] -= 1
            elif tamper == "role_usage":
                current["assignments"]["executor"]["usage"]["tokens"] -= 1
            elif tamper == "generation":
                current["assignments"]["executor"]["session_generation"] += 1
            elif tamper == "assignment":
                current["assignments"]["executor"]["id"] = "changed-assignment"
            else:
                store.action(run_id, "shell", {"command": "unknown effect"}, c)
    Worker(settings).run(once=True)
    after = store.get(run_id)
    assert after["state"] in {"interrupted", "cancelled", "budget_exceeded"}, after[
        "reason"
    ]
    assert len(calls) == 1
    assert not (directory / "workspace" / "executed.txt").exists()
    if tamper != "clock":
        assert background.day(grant["date"])["reserved"]["calls"] == 20


def test_residual_container_blocks_foreground_even_when_background_is_yielded(
    tuning, store, request_data, monkeypatch
):
    settings, _, _, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]
    with store.locked(run_id) as (_, current):
        current.update(state="yielded", first_started_at=now())
    user = foreground(store, request_data)
    monkeypatch.setattr(Sandbox, "containers", lambda *_: ["unknown-container"])
    with pytest.raises(RuntimeError, match="残留或未知容器"):
        Worker(settings).run(once=True)
    assert store.get(user["id"])["state"] == "queued"
    assert store.get(run_id)["state"] == "interrupted"


@pytest.mark.docker
def test_cleanup_error_never_becomes_safe_yield(
    tuning, store, request_data, monkeypatch
):
    settings, _, _, batch = claimed(tuning, store)
    run_id = batch["attempts"][0]["run_id"]
    user = []

    def transport(*args, **kwargs):
        return response("doonex_shell", {"command": "true"}, 0)

    def shell(*args, **kwargs):
        user.append(foreground(store, request_data))
        return {
            "exit_code": 0,
            "output": "",
            "cleanup_error": "synthetic cleanup failure",
        }

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    monkeypatch.setattr(Sandbox, "run", shell)
    monkeypatch.setattr(
        Sandbox,
        "stop_run",
        lambda *_: {"remaining": ["unknown"], "errors": ["synthetic"], "stopped": []},
    )
    Worker(settings).run(once=True)
    result = store.get(run_id)
    assert result["state"] == "interrupted"
    assert not result.get("scheduling")
    assert any(
        row["kind"] == "shell" and row["status"] == "unknown"
        for row in action_rows(store, run_id)
    )
    with pytest.raises(RuntimeError, match="终止状态不明"):
        Worker(settings).run(once=True)
    assert store.get(user[0]["id"])["state"] == "queued"
