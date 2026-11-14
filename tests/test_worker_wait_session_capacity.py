"""Real Worker.run session-capacity scenarios, with synthetic model transport.

The 64 MiB storage contract does not increase the 16 MiB source-shape or
200,000-token configured input ceilings. Full active-history receipts and
known pending-tool restoration are tested separately from model admission.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import controller_test_support
import pytest
from doonex import sandbox as sandbox_module
from doonex import sdk
from doonex import workspace as workspace_module
from doonex.budget import Ledger
from doonex.session_store import LIMITS, MIB, SessionFileStore, canonical_sha256
from doonex.store import Store
from doonex.worker import Worker, run_directory
from openhands.sdk.conversation.event_store import EventLog
from openhands.sdk.conversation.persistence_const import EVENT_NAME_RE
from openhands.sdk.event import MessageEvent
from openhands.sdk.io import LocalFileStore
from openhands.sdk.llm import Message, TextContent
from test_api import repository
from test_scheduling import action_rows, claimed
from test_worker import response
from test_worker_wait_matrix import (
    taskset_source as taskset_fixture,
)
from test_worker_wait_matrix import (
    test_actual_worker_wait_timeline as chat_timeline_fixture,
)
from test_worker_wait_matrix import tuning as tuning_fixture

controller_registry = controller_test_support.controller_registry
taskset_source = taskset_fixture
tuning = tuning_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]


class _FixtureWatchdogExpired(BaseException):
    """A failed test guard, never a product deadline or latency success."""


@contextmanager
def _watchdog(seconds=90):
    assert threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGALRM)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    fired = []

    def expired(signum, frame):
        fired.append(time.monotonic())
        raise _FixtureWatchdogExpired("Capacity fixture failure watchdog fired")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        assert not fired, "The watchdog fired; this is a failed capacity sample"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _is_event(path):
    return path.startswith("events/") and bool(EVENT_NAME_RE.fullmatch(path[7:]))


def _files(root):
    return {
        str(path.relative_to(root)): _sha(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _snapshot(store):
    tables = {
        "runs": "id",
        "events": "id",
        "actions": "id",
        "controller_operations": "id",
        "arena_evaluations": "id",
        "background_authorizations": "id",
        "background_allocations": "id",
        "background_days": "day",
    }
    with store.connect() as connection:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY {order}"
            ).fetchall()
            for table, order in tables.items()
        }


def _accounting(store, evaluation_id):
    with store.connect() as connection:
        parent = connection.execute(
            "SELECT data FROM arena_evaluations WHERE id=%s", (evaluation_id,)
        ).fetchone()["data"]
        allocations = connection.execute(
            "SELECT * FROM background_allocations WHERE evaluation_id=%s ORDER BY id",
            (evaluation_id,),
        ).fetchall()
        days = connection.execute(
            "SELECT * FROM background_days WHERE day IN "
            "(SELECT day FROM background_allocations WHERE evaluation_id=%s) ORDER BY day",
            (evaluation_id,),
        ).fetchall()
    assert allocations and days
    return {
        "parent": {
            key: value
            for key, value in parent.items()
            if key not in {"state", "reason", "updated_at"}
        },
        "allocations": allocations,
        "days": days,
    }


@pytest.fixture
def capacity_resources(tuning, store, monkeypatch):
    """Own live Popen handles and exact registered fixture containers only."""
    settings = tuning[0]
    handles = []

    def popen(*args, **kwargs):
        assert kwargs.get("start_new_session") is True
        process = subprocess.Popen(*args, **kwargs)
        handles.append(process)
        return process

    supervised = SimpleNamespace(**{**vars(subprocess), "Popen": popen})
    monkeypatch.setattr(sandbox_module, "subprocess", supervised)
    monkeypatch.setattr(workspace_module, "subprocess", supervised)
    yield
    before = _snapshot(store)
    print("capacity_rows_before_cleanup=" + json.dumps(before, default=str))
    for process in handles:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
    docker = shutil.which("docker")
    assert docker is not None
    removed = []
    for row in before["runs"]:
        run_id = row["data"]["id"]
        names = {
            operation["data"]["scope"]["container_name"]
            for operation in before["controller_operations"]
            if operation["data"]["run_id"] == run_id
        }
        arguments = [
            docker,
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=doonex.run_id=" + run_id,
        ]
        listed = subprocess.run(
            arguments, check=True, capture_output=True, text=True, timeout=10
        )
        for identifier in listed.stdout.splitlines():
            assert re.fullmatch(r"[0-9a-f]{64}", identifier)
            inspected = subprocess.run(
                [docker, "inspect", identifier],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            values = json.loads(inspected.stdout)
            assert len(values) == 1
            value = values[0]
            assert value["Id"] == identifier
            assert value["Name"].removeprefix("/") in names
            assert value["Config"]["Labels"]["doonex.run_id"] == run_id
            mounts = [m for m in value["Mounts"] if m["Destination"] == "/workspace"]
            assert len(mounts) == 1 and mounts[0]["Type"] == "bind"
            assert (
                Path(mounts[0]["Source"])
                .resolve()
                .is_relative_to(run_directory(settings, run_id).resolve())
            )
            subprocess.run(
                [docker, "rm", "-f", identifier],
                check=True,
                capture_output=True,
                timeout=15,
            )
            removed.append(identifier)
        assert (
            subprocess.run(
                arguments, check=True, capture_output=True, text=True, timeout=10
            ).stdout
            == ""
        )
    assert _snapshot(store) == before
    print(
        "capacity_resource_cleanup="
        + json.dumps(
            {
                "removed_exact_ids": removed,
                "live_handles_reaped": all(p.poll() is not None for p in handles),
                "durable_rows_unchanged": True,
            }
        )
    )


def _wide_tuning(tuning):
    settings, background, body, repo = tuning
    settings = replace(
        settings, model=settings.model.model_copy(update={"max_input_tokens": 200000})
    )
    # A finite, explicitly synthetic fixture allowance; production limits and
    # each Run's original clock/reservations remain in force.
    body = deepcopy(body)
    body["run_budget"]["max_tokens"] = 1_000_000
    return settings, type(background)(settings, background.store), body, repo


def _message(text, *, parent_id=None):
    # IDs and timestamps always come from the pinned SDK's default factories.
    return MessageEvent(
        source="environment",
        parent_id=parent_id,
        llm_message=Message(role="user", content=[TextContent(text=text)]),
    )


def _census(conversation):
    state, store = conversation.state, conversation.state._fs
    branch = state.active_branch()
    return {
        "root": store.root,
        "events": len(state.events),
        "active_branch_events": len(branch),
        "entries": len(store._files) + len(store._dirs),
        "logical_bytes": store._total,
        "leaf_event_id": state.leaf_event_id,
        "branch_sha256": canonical_sha256(
            [event.model_dump(mode="json") for event in branch], check=store.check_ok
        ),
    }


def _seed_active(conversation, shape, *, target=1000, entry_headroom=None):
    state, store = conversation.state, conversation.state._fs
    count = target - len(state.events)
    assert count > 900
    text_bytes = {
        "small": 16,
        "input": 512,
        "events": 16,
        "entries": 16,
        "bytes": (LIMITS.total_bytes - 512 * 1024 - store._total) // count - 1024,
    }[shape]
    ids = []
    # This is the SDK's own transactional public state context and storage
    # chokepoint; no raw event IDs, shortened history, cache or replaced methods.
    with state:
        for index in range(count):
            event = _message(f"{index:04d}:" + "x" * (text_bytes - 5))
            state.append_event(event)
            ids.append(event.id)
    state.rebuild_view()
    if shape == "entries" or entry_headroom is not None:
        headroom = 8 if entry_headroom is None else entry_headroom
        while len(store._files) + len(store._dirs) < LIMITS.entries - headroom:
            store.write(f"capacity-sidecar-{len(store._files):05d}", "x")
    store.verify_inventory()
    branch = state.active_branch()
    assert set(ids).issubset({event.id for event in branch})
    assert state.leaf_event_id == ids[-1]
    known = set()
    for event in branch:
        assert event.parent_id is None or event.parent_id in known
        known.add(event.id)
    value = _census(conversation)
    assert value["events"] == target
    assert value["active_branch_events"] >= count
    if shape == "bytes":
        assert 62 * MIB <= value["logical_bytes"] < LIMITS.total_bytes
    if shape == "entries" or entry_headroom is not None:
        assert value["entries"] == LIMITS.entries - headroom
    value["sdk_generated_seed_ids_sha256"] = canonical_sha256(ids)
    return value


def _seed_hook(monkeypatch, store, shape, records, *, phase, entry_headroom=None):
    if phase == "receipt":
        original = sdk._SchedulingRuntime.receipt

        def receipt(runtime, conversation):
            if not records:
                record = _seed_active(
                    conversation,
                    shape,
                    target=1020 if shape == "events" else 1000,
                    entry_headroom=entry_headroom,
                )
                records.append(record)
            result = original(runtime, conversation)
            if len(records) == 1:
                assert result["branch_sha256"] == records[0]["branch_sha256"]
            return result

        monkeypatch.setattr(sdk._SchedulingRuntime, "receipt", receipt)
        return
    original = sdk._M1Conversation.__init__
    original_close = sdk._M1Conversation.close

    def initialize(conversation, *args, **kwargs):
        original(conversation, *args, **kwargs)
        identifier = conversation.state.agent.doonex_run_id
        if not records and store.get(identifier)["request"].get("background"):
            try:
                records.append(
                    _seed_active(conversation, shape, entry_headroom=entry_headroom)
                )
            except BaseException:
                # __init__ has returned from the real SDK, but the outer local
                # variable is not assigned yet. Close that actual SDK instance.
                original_close(conversation)
                raise

    monkeypatch.setattr(sdk._M1Conversation, "__init__", initialize)


def _replace_pending_command(monkeypatch):
    import test_worker_wait_matrix as matrix

    original = matrix.response

    def controlled_response(tool, arguments, sequence):
        if tool == "doonex_shell" and arguments == {"command": "touch must-not-run"}:
            arguments = {"command": "printf 'answer = 42\\n' > main.py"}
        return original(tool, arguments, sequence)

    monkeypatch.setattr(matrix, "response", controlled_response)


def _yielded(store):
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT data FROM runs WHERE state='yielded'"
        ).fetchall()
    assert len(rows) == 1
    value = rows[0]["data"]
    assert value["request"].get("background")
    return value


def _foreground_body(request_data, tmp_path, name):
    source = tmp_path / name
    source.mkdir()
    repo, git = repository(source)
    body = deepcopy(request_data)
    body.update(
        repo_path=str(repo),
        base_commit=git("rev-parse", "HEAD"),
        checks=["python -c 'assert True'"],
    )
    body["model"]["model_id"] = "gpt-4.1-mini"
    return body


def _cold_startup_timeline(settings, store, paused, root, body, monkeypatch):
    """Include construction/initialize/reconcile/census after enqueue commits."""
    observations, admission = [], []
    original_initialize = Store.initialize
    original_reconcile = Worker.reconcile
    original_census = Store.scheduling_runs
    original_reserve = Ledger.reserve

    def observe(kind, **details):
        observations.append({"kind": kind, "monotonic": time.monotonic(), **details})

    def initialize(current, *args, **kwargs):
        observe("startup_store_initialize_enter")
        value = original_initialize(current, *args, **kwargs)
        observe("startup_store_initialize_returned")
        return value

    def reconcile(current, *args, **kwargs):
        observe("startup_reconcile_enter")
        value = original_reconcile(current, *args, **kwargs)
        observe("startup_reconcile_returned")
        return value

    def census(current, *args, **kwargs):
        value = original_census(current, *args, **kwargs)
        observe(
            "startup_census_returned",
            states=sorted(kwargs["states"]),
            run_ids=[item["id"] for item in value],
        )
        return value

    def reserve(ledger, *args, **kwargs):
        assert ledger.run_id == foreground["id"]
        value = original_reserve(ledger, *args, **kwargs)
        admission.append(time.monotonic())
        observe("startup_foreground_model_admission_commit_confirmed")
        return value

    def transport(llm, *args, **kwargs):
        assert Path(llm._session_store.root) != root
        return response("finish", {"message": "Synthetic startup foreground"}, 61)

    with monkeypatch.context() as startup:
        startup.setattr(Store, "initialize", initialize)
        startup.setattr(Worker, "reconcile", reconcile)
        startup.setattr(Store, "scheduling_runs", census)
        startup.setattr(Ledger, "reserve", reserve)
        startup.setattr(sdk.GuardedLLM, "_transport_call", transport)
        before = time.monotonic()
        foreground = store.create_run(body)
        after = time.monotonic()
        observe("startup_foreground_enqueue_commit_confirmed")
        Worker(settings).run(once=True)
    assert len(admission) == 1
    assert store.get(foreground["id"])["state"] == "completed_verified"
    current = store.get(paused["id"])
    assert current["state"] == "yielded"
    assert current["usage"] == paused["usage"]
    assert current["scheduling"] == paused["scheduling"]
    assert current["first_started_at"] == paused["first_started_at"]
    moments = {item["kind"]: item["monotonic"] for item in observations}
    assert (
        before
        <= after
        <= moments["startup_store_initialize_enter"]
        <= moments["startup_store_initialize_returned"]
        <= moments["startup_reconcile_enter"]
        <= moments["startup_reconcile_returned"]
        <= admission[0]
    )
    yielded = [
        item
        for item in observations
        if item["kind"] == "startup_census_returned" and item["states"] == ["yielded"]
    ]
    assert yielded and paused["id"] in yielded[0]["run_ids"]
    assert after <= yielded[0]["monotonic"] <= admission[0]
    return {
        "foreground_id": foreground["id"],
        "enqueue_commit_bracket_monotonic": [before, after],
        "first_model_admission_commit_confirmed_monotonic": admission[0],
        "submit_to_first_admission_seconds": {
            "lower": admission[0] - after,
            "upper": admission[0] - before,
        },
        "observations": observations,
        "sdk_restore_deferred_by_actual_foreground_priority": True,
    }


def _cold_foreground_timeline(settings, store, paused, root, body, monkeypatch):
    """Queue during actual cold restoration, then run the real dispatcher."""
    entered, release, finished = (threading.Event() for _ in range(3))
    errors, order, observations, foreground, admission = ([] for _ in range(5))
    worker = Worker(settings)
    original_init = SessionFileStore.__init__
    original_execute = worker.execute
    original_reserve = Ledger.reserve

    def observe(kind, **details):
        observations.append({"kind": kind, "monotonic": time.monotonic(), **details})

    def initialize(current, current_root, *args, **kwargs):
        is_background = Path(current_root) == root
        if is_background:
            assert not entered.is_set()
            observe("cold_inventory_enter")
            entered.set()
            assert release.wait(20), "Foreground did not release cold restore gate"
        value = original_init(current, current_root, *args, **kwargs)
        if is_background:
            observe("cold_full_inventory_returned")
        return value

    def reserve(ledger, *args, **kwargs):
        assert foreground and ledger.run_id == foreground[0]["id"]
        value = original_reserve(ledger, *args, **kwargs)
        admission.append(time.monotonic())
        observe("cold_foreground_model_admission_commit_confirmed")
        return value

    def transport(llm, *args, **kwargs):
        assert Path(llm._session_store.root) != root, (
            "Cold background dispatched a model after foreground enqueue"
        )
        return response("finish", {"message": "Synthetic cold foreground finish"}, 60)

    def execute(identifier):
        order.append(identifier)
        try:
            original_execute(identifier)
            if identifier == paused["id"]:
                current = store.get(identifier)
                assert current["state"] == "yielded", current["reason"]
                assert current["usage"] == paused["usage"]
                assert current["first_started_at"] == paused["first_started_at"]
                observe("cold_background_yield_commit_confirmed")
            else:
                assert foreground and identifier == foreground[0]["id"]
                worker.stopping = True
        except BaseException:
            worker.stopping = True
            raise

    def run():
        try:
            worker.run()
        except BaseException as error:  # noqa: BLE001 - keep actual thread failures
            errors.append(error)
        finally:
            finished.set()

    with monkeypatch.context() as cold:
        cold.setattr(SessionFileStore, "__init__", initialize)
        cold.setattr(Ledger, "reserve", reserve)
        cold.setattr(sdk.GuardedLLM, "_transport_call", transport)
        cold.setattr(worker, "execute", execute)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert entered.wait(20), "Fresh Worker did not enter cold SessionFileStore"
            before = time.monotonic()
            foreground.append(store.create_run(body))
            after = time.monotonic()
            observe("cold_foreground_enqueue_commit_confirmed")
            release.set()
            assert finished.wait(60), "Cold capacity fixture did not finish foreground"
        finally:
            release.set()
            worker.stopping = True
            thread.join(timeout=20)
            print(
                "worker_capacity_cold_partial="
                + json.dumps(
                    {
                        "thread_alive": thread.is_alive(),
                        "error_types": [type(error).__name__ for error in errors],
                        "observations": observations,
                    }
                )
            )
        assert not thread.is_alive() and not errors, errors
    assert order == [paused["id"], foreground[0]["id"]]
    assert store.get(foreground[0]["id"])["state"] == "completed_verified"
    assert len(admission) == 1 and before <= after <= admission[0]
    moments = {item["kind"]: item["monotonic"] for item in observations}
    assert (
        moments["cold_inventory_enter"]
        <= before
        <= after
        <= moments["cold_full_inventory_returned"]
        <= moments["cold_background_yield_commit_confirmed"]
        <= admission[0]
    )
    return {
        "foreground_id": foreground[0]["id"],
        "enqueue_commit_bracket_monotonic": [before, after],
        "first_model_admission_commit_confirmed_monotonic": admission[0],
        "submit_to_first_admission_seconds": {
            "lower": admission[0] - after,
            "upper": admission[0] - before,
        },
        "observations": observations,
    }


@pytest.mark.parametrize("shape", ["small", "bytes"])
def test_near_capacity_active_branch_receipt_and_actual_cold_worker_restore(
    tuning,
    taskset_source,
    store,
    request_data,
    tmp_path,
    monkeypatch,
    capacity_resources,
    shape,
):
    wide = _wide_tuning(tuning)
    seeded = []
    with monkeypatch.context() as warm:
        _replace_pending_command(warm)
        _seed_hook(
            warm,
            store,
            shape,
            seeded,
            phase="init" if shape == "small" else "receipt",
            # Native sidecars count toward the unchanged directory contract.
            # Leave 32 entries for real model/status and restored observations;
            # retain every sidecar through the complete cold inventory.
            entry_headroom=32 if shape == "small" else None,
        )
        with _watchdog():
            chat_timeline_fixture(
                wide,
                taskset_source,
                store,
                request_data,
                tmp_path,
                warm,
                "model",
                "ordinary",
            )
    paused = _yielded(store)
    root = Path(seeded[0]["root"])
    original_files = _files(root)
    original_events = {
        name: digest for name, digest in original_files.items() if _is_event(name)
    }
    assert len(original_events) >= 1000
    assert paused["usage"]["calls"] == 1
    assert paused["usage"]["unknown_calls"] == paused["usage"]["reserved_tokens"] == 0
    assert len(paused["scheduling"]["receipt"]["pending_actions"]) == 2
    evaluation_id = paused["request"]["arena"]["evaluation_id"]
    parent_before = _accounting(store, evaluation_id)
    reads, receipts, closes, close_calls, sessions = [], [], [], [], []
    original_read = SessionFileStore._read_fd
    original_receipt = sdk._SchedulingRuntime.receipt
    original_close = sdk._M1Conversation.close
    original_session = sdk.run_session

    def read(current, fd, path, info):
        value = original_read(current, fd, path, info)
        if Path(current.root) == root:
            reads.append((path, _sha(value.encode("utf-8"))))
        return value

    def receipt(runtime, conversation):
        value = original_receipt(runtime, conversation)
        receipts.append(value)
        return value

    def close(conversation):
        already_complete = conversation._cleanup_complete
        value = original_close(conversation)
        identifier = conversation.state.agent.doonex_run_id
        assert conversation._cleanup_complete
        close_calls.append({"run_id": identifier, "already_complete": already_complete})
        # The SDK finish path closes natively; run_session's finally closes
        # again idempotently. Count real first cleanup while recording both.
        if not already_complete:
            closes.append(identifier)
        return value

    def session(**kwargs):
        sessions.append((kwargs["run_id"], kwargs["resume"]))
        return original_session(**kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "Known pending shell+finish must restore without another model call"
        )

    monkeypatch.setattr(SessionFileStore, "_read_fd", read)
    monkeypatch.setattr(sdk._SchedulingRuntime, "receipt", receipt)
    monkeypatch.setattr(sdk._M1Conversation, "close", close)
    monkeypatch.setattr(sdk, "run_session", session)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)
    with _watchdog():
        cold_startup = _cold_startup_timeline(
            wide[0],
            store,
            paused,
            root,
            _foreground_body(request_data, tmp_path, "startup-foreground"),
            monkeypatch,
        )
    assert not reads, "Foreground priority should defer background SDK restoration"
    assert _files(root) == original_files
    assert _accounting(store, evaluation_id) == parent_before
    with _watchdog():
        cold_wait = _cold_foreground_timeline(
            wide[0],
            store,
            paused,
            root,
            _foreground_body(request_data, tmp_path, "cold-foreground"),
            monkeypatch,
        )
    assert _accounting(store, evaluation_id) == parent_before
    with _watchdog():
        Worker(wide[0]).run(once=True)
    final = store.get(paused["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert final["first_started_at"] == paused["first_started_at"]
    assert final["request"] == paused["request"]
    assert final["usage"] == paused["usage"]
    assert _accounting(store, evaluation_id) == parent_before
    assert sessions == [
        (cold_startup["foreground_id"], False),
        (paused["id"], True),
        (cold_wait["foreground_id"], False),
        (paused["id"], True),
    ]
    assert closes == [
        cold_startup["foreground_id"],
        paused["id"],
        cold_wait["foreground_id"],
        paused["id"],
    ]
    assert receipts == [paused["scheduling"]["receipt"]] * 3
    assert set(original_files.items()).issubset(set(reads))
    final_files = _files(root)
    assert all(final_files[name] == digest for name, digest in original_events.items())
    final_entries = sum(1 for _ in root.rglob("*"))
    if shape == "small":
        assert seeded[0]["entries"] == LIMITS.entries - 32
        assert seeded[0]["entries"] <= final_entries <= LIMITS.entries
    assert [r["kind"] for r in action_rows(store, paused["id"])] == [
        "model",
        "shell",
        "verification",
    ]
    assert all(r["status"] == "completed" for r in action_rows(store, paused["id"]))
    assert not store.pending(paused["id"])
    for registry in (sdk._CALLBACKS, sdk._SCHEDULING, sdk._SESSION_STORES):
        assert paused["id"] not in registry
    print(
        "worker_session_capacity="
        + json.dumps(
            {
                "shape": shape,
                "warm_seed": seeded[0],
                "cold_all_original_files_read_and_hashed": True,
                "cold_original_event_count": len(original_events),
                "cold_final_directory_entries": final_entries,
                "cold_startup_foreground_wait": cold_startup,
                "cold_foreground_wait": cold_wait,
                "native_close_calls": close_calls,
                "original_usage_and_parent_ledgers_preserved": True,
                "additional_model_calls": 0,
                "transport": "synthetic",
                "normal_latency_pass_claim": False,
            },
            sort_keys=True,
        )
    )


@pytest.mark.parametrize(
    "shape,reason", [("input", "input token bound"), ("bytes", "source capacity")]
)
def test_full_active_prompt_respects_original_input_contract_before_reserving(
    tuning, store, monkeypatch, capacity_resources, shape, reason
):
    wide = _wide_tuning(tuning)
    _, _, _, batch = claimed(wide, store)
    identifier = batch["attempts"][0]["run_id"]
    records, calls, reservations = [], [], []
    _seed_hook(monkeypatch, store, shape, records, phase="init")
    original = Ledger.reserve

    def reserve(ledger, *args, **kwargs):
        reservations.append(ledger.run_id)
        return original(ledger, *args, **kwargs)

    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("The original input guards must refuse before model transport")

    monkeypatch.setattr(Ledger, "reserve", reserve)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)
    with _watchdog():
        Worker(wide[0]).run(once=True)
    final = store.get(identifier)
    assert final["state"] == "failed", final["reason"]
    assert reason in final["reason"].lower()
    assert not calls and not reservations and not action_rows(store, identifier)
    assert final["usage"]["calls"] == final["usage"]["unknown_calls"] == 0
    assert final["usage"]["reserved_tokens"] == 0
    assert final["first_started_at"] is not None
    assert records[0]["events"] == 1000
    print(
        "worker_session_input_refusal="
        + json.dumps({"shape": shape, "seed": records[0], "reason": final["reason"]})
    )


def _external_sdk_over_capacity(root, kind):
    """Model an external native SDK writer; production guards are untouched."""
    native = LocalFileStore(str(root))
    log = EventLog(native)
    prior = _files(root)
    parent = log[-1].id
    if kind == "events":
        while len(log) <= LIMITS.events:
            event = _message("External SDK capacity fixture", parent_id=parent)
            log.append(event)
            parent = event.id
    elif kind == "bytes":
        total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        wanted = LIMITS.total_bytes + 1 - total
        event = _message("", parent_id=parent)
        overhead = len(event.model_dump_json(exclude_none=True).encode("utf-8"))
        assert 0 < wanted - overhead < LIMITS.event_bytes - overhead
        event.llm_message.content[0].text = "x" * (wanted - overhead)
        assert len(event.model_dump_json(exclude_none=True).encode("utf-8")) == wanted
        log.append(event)
    else:
        while sum(1 for _ in root.rglob("*")) <= LIMITS.entries:
            native.write(f"external-capacity-sidecar-{len(_files(root)):05d}", "x")
    after = _files(root)
    assert all(
        after[name] == digest for name, digest in prior.items() if _is_event(name)
    )
    census = {
        "events": len(log),
        "entries": sum(1 for _ in root.rglob("*")),
        "logical_bytes": sum(
            path.stat().st_size for path in root.rglob("*") if path.is_file()
        ),
    }
    assert (
        census[kind if kind != "bytes" else "logical_bytes"]
        == {
            "events": LIMITS.events + 1,
            "entries": LIMITS.entries + 1,
            "bytes": LIMITS.total_bytes + 1,
        }[kind]
    )
    return after, census


@pytest.mark.parametrize("kind", ["events", "bytes", "entries"])
def test_actual_cold_worker_refuses_external_sdk_cap_plus_one_without_new_admission(
    tuning,
    taskset_source,
    store,
    request_data,
    tmp_path,
    monkeypatch,
    capacity_resources,
    kind,
):
    wide = _wide_tuning(tuning)
    seeded = []
    with monkeypatch.context() as warm:
        _replace_pending_command(warm)
        _seed_hook(warm, store, kind, seeded, phase="receipt")
        with _watchdog():
            chat_timeline_fixture(
                wide,
                taskset_source,
                store,
                request_data,
                tmp_path,
                warm,
                "model",
                "ordinary",
            )
    paused = _yielded(store)
    root = Path(seeded[0]["root"])
    frozen, census = _external_sdk_over_capacity(root, kind)
    before_accounting = _accounting(store, paused["request"]["arena"]["evaluation_id"])
    original_fail = SessionFileStore._fail
    observed = []

    def fail(current, reason, **kwargs):
        if Path(current.root) == root:
            observed.append(reason)
        return original_fail(current, reason, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Cold cap+1 must refuse before a new model or pending tool")

    monkeypatch.setattr(SessionFileStore, "_fail", fail)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)
    monkeypatch.setattr(Ledger, "reserve", forbidden)
    monkeypatch.setattr(Ledger, "tool_intent", forbidden)
    with _watchdog():
        Worker(wide[0]).run(once=True)
    current = store.get(paused["id"])
    assert current["state"] == "failed", current["reason"]
    assert observed and all("capacity exceeded" in item.lower() for item in observed)
    assert current["usage"] == paused["usage"]
    assert current["first_started_at"] == paused["first_started_at"]
    assert current["request"] == paused["request"]
    assert (
        _accounting(store, paused["request"]["arena"]["evaluation_id"])
        == before_accounting
    )
    assert [row["kind"] for row in action_rows(store, paused["id"])] == ["model"]
    assert _files(root) == frozen
    print(
        "worker_session_cap_plus_one="
        + json.dumps(
            {
                "kind": kind,
                "warm_seed": seeded[0],
                "cold_census": census,
                "cold_refusal": observed,
                "files_and_original_ledgers_preserved": True,
            }
        )
    )


def test_near_capacity_unknown_model_reservation_survives_worker_restart(
    tuning, store, request_data, tmp_path, monkeypatch, capacity_resources
):
    wide = _wide_tuning(tuning)
    _, _, _, batch = claimed(wide, store)
    identifier = batch["attempts"][0]["run_id"]
    seeded, calls = [], []
    _seed_hook(monkeypatch, store, "small", seeded, phase="init")

    def unknown(*args, **kwargs):
        calls.append(identifier)
        raise TimeoutError("Synthetic unknown provider outcome")

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", unknown)
    with _watchdog():
        Worker(wide[0]).run(once=True)
    original = store.get(identifier)
    assert original["state"] == "interrupted", original["reason"]
    assert original["usage"]["calls"] == original["usage"]["unknown_calls"] == 1
    assert original["usage"]["reserved_tokens"] > 0
    pending = store.pending(identifier)
    assert len(pending) == 1 and pending[0]["status"] == "unknown"
    original_files = _files(Path(seeded[0]["root"]))
    parent_before = _accounting(store, batch["id"])
    repo_root = tmp_path / "unknown-foreground"
    repo_root.mkdir()
    repo, git = repository(repo_root)
    body = deepcopy(request_data)
    body.update(
        repo_path=str(repo),
        base_commit=git("rev-parse", "HEAD"),
        checks=["python -c 'assert True'"],
    )
    body["model"]["model_id"] = "gpt-4.1-mini"
    before = time.monotonic()
    foreground = store.create_run(body)
    after = time.monotonic()
    admission = []
    original_reserve = Ledger.reserve

    def reserve(ledger, *args, **kwargs):
        assert ledger.run_id == foreground["id"]
        value = original_reserve(ledger, *args, **kwargs)
        admission.append(time.monotonic())
        return value

    def finish(*args, **kwargs):
        calls.append(foreground["id"])
        return response("finish", {"message": "Synthetic foreground complete"}, 50)

    monkeypatch.setattr(Ledger, "reserve", reserve)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", finish)
    with _watchdog():
        Worker(wide[0]).run(once=True)
    current = store.get(identifier)
    assert current["state"] == "interrupted"
    assert current["usage"] == original["usage"]
    assert current["request"] == original["request"]
    assert current["first_started_at"] == original["first_started_at"]
    assert store.pending(identifier) == pending
    assert _accounting(store, batch["id"]) == parent_before
    assert _files(Path(seeded[0]["root"])) == original_files
    assert calls == [identifier, foreground["id"]]
    assert len(admission) == 1 and before <= after <= admission[0]
    assert store.get(foreground["id"])["state"] == "completed_verified"
    print(
        "worker_capacity_unknown_restart="
        + json.dumps(
            {
                "seed": seeded[0],
                "enqueue_commit_bracket_monotonic": [before, after],
                "first_admission_commit_confirmed_monotonic": admission[0],
                "submit_to_first_admission_seconds": {
                    "lower": admission[0] - after,
                    "upper": admission[0] - before,
                },
                "unknown_reservation_and_original_parent_ledgers_preserved": True,
                "normal_latency_pass_claim": False,
            }
        )
    )
