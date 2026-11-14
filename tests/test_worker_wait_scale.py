"""Complete Worker.run paths with finite, explicitly synthetic index load.

Historical payloads are database load, not claims of completed product tasks.
The unknown/yielded Runs, enabled grants and delivered foreground are produced
through the real product entry points. Only the provider is synthetic.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import controller_test_support
import pytest
from doonex import sandbox as sandbox_module
from doonex import sdk
from doonex import workspace as workspace_module
from doonex.background import Background
from doonex.budget import Ledger
from doonex.sandbox import Sandbox
from doonex.scheduling import Scheduler
from doonex.store import Store
from doonex.worker import Worker, run_directory
from test_api import repository
from test_scheduling import action_rows, claimed
from test_scheduling import taskset_source as taskset_fixture
from test_scheduling import tuning as tuning_fixture
from test_worker import response
from test_worker_wait_faults import _failure_watchdog

controller_registry = controller_test_support.controller_registry
taskset_source = taskset_fixture
tuning = tuning_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]

HISTORY_RUNS = 100_000
HISTORY_PARENTS = 10_000
ENABLED_SAMPLES = 64  # A finite sample, not a product authorization cap.
LOAD_MARKER = "synthetic_index_load_not_completed_tasks_v1"
TABLES = {
    "runs": "id",
    "events": "id",
    "actions": "id",
    "controller_operations": "id",
    "arena_evaluations": "id",
    "background_authorizations": "id",
    "background_allocations": "id",
    "background_days": "day",
}
LOAD_COUNTS = {
    "runs": HISTORY_RUNS,
    "actions": HISTORY_RUNS,
    "arena_evaluations": HISTORY_PARENTS,
    "background_authorizations": HISTORY_PARENTS,
    "background_allocations": HISTORY_PARENTS,
}


def register_owned(run_ids):
    raw = os.environ.get("DOONEX_TEST_OWNERSHIP_FILE")
    if not raw:
        return
    path = Path(raw)
    value = json.loads(path.read_text())
    value["run_ids"] = sorted(set(value.get("run_ids", ())) | set(run_ids))
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def real_rows(store):
    """Preserve actual durable rows before the fixture drops its schema."""
    with store.connect() as connection:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} "
                + ("" if table == "events" else "WHERE NOT(data ? 'scale_index_load') ")
                + f"ORDER BY {order}"
            ).fetchall()
            for table, order in TABLES.items()
        }


def index_fingerprints(store):
    """Stream only digests to the receipt; never print 230,000 payload rows."""
    result = {}
    with store.connect() as connection:
        for table, expected in LOAD_COUNTS.items():
            digest, count = hashlib.sha256(), 0
            with connection.cursor(name="scale_fingerprint") as cursor:
                cursor.execute(
                    f"SELECT row_to_json(source)::text AS serialized FROM {table} "
                    "AS source WHERE data->>'scale_index_load'=%s ORDER BY id",
                    (LOAD_MARKER,),
                )
                while rows := cursor.fetchmany(512):
                    for row in rows:
                        digest.update(row["serialized"].encode())
                        digest.update(b"\n")
                        count += 1
            assert count == expected
            result[table] = {"count": count, "sha256": digest.hexdigest()}
    return result


def install_index_load(store, day, project_id):
    """Build foreign-key-bound terminal load with no claim of valid evidence.

    The minimal payloads intentionally have no executable sessions or delivery
    artifacts. Every one is marked synthetic and must remain excluded from
    active Worker/recovery queries. Completed is the actual Action status;
    there is no invented 'settled' Action status.
    """
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO arena_evaluations(id,state,data)
            SELECT md5('scale-arena-'||n)::uuid,'complete',jsonb_build_object(
              'id',md5('scale-arena-'||n)::uuid,'state','complete',
              'created_at','2026-01-01T00:00:00+00:00',
              'scale_index_load',%s,
              'background',jsonb_build_object(
                'authorization_id',md5('scale-grant-'||n)::uuid,
                'reservation_id',md5('scale-allocation-'||n)::uuid,'date',%s))
            FROM generate_series(1,%s) AS n""",
            (LOAD_MARKER, day, HISTORY_PARENTS),
        )
        connection.execute(
            """INSERT INTO background_authorizations(id,state,data)
            SELECT md5('scale-grant-'||n)::uuid,'consumed',jsonb_build_object(
              'id',md5('scale-grant-'||n)::uuid,'state','consumed',
              'created_at','2026-01-01T00:00:00+00:00','date',%s,
              'evaluation_id',md5('scale-arena-'||n)::uuid,'scale_index_load',%s)
            FROM generate_series(1,%s) AS n""",
            (day, LOAD_MARKER, HISTORY_PARENTS),
        )
        connection.execute(
            """INSERT INTO background_allocations
            (id,authorization_id,evaluation_id,day,state,data)
            SELECT md5('scale-allocation-'||n)::uuid,
              md5('scale-grant-'||n)::uuid,md5('scale-arena-'||n)::uuid,
              %s::date,'settled',jsonb_build_object(
                'id',md5('scale-allocation-'||n)::uuid,
                'authorization_id',md5('scale-grant-'||n)::uuid,
                'evaluation_id',md5('scale-arena-'||n)::uuid,
                'date',%s,'state','settled','scale_index_load',%s,
                'actual',jsonb_build_object('calls',0,'tokens',0,'seconds',0,
                  'cost_usd',NULL),
                'reserved',jsonb_build_object('calls',0,'tokens',0,'seconds',0,
                  'cost_usd',NULL))
            FROM generate_series(1,%s) AS n""",
            (day, day, LOAD_MARKER, HISTORY_PARENTS),
        )
        connection.execute(
            """INSERT INTO runs(id,state,data)
            SELECT md5('scale-run-'||n)::uuid,'completed_unverified',
              jsonb_build_object(
                'id',md5('scale-run-'||n)::uuid,'state','completed_unverified',
                'created_at','2026-01-01T00:00:00+00:00',
                'scale_index_load',%s,
                'request',jsonb_build_object('project_id',%s,
                  'arena',jsonb_build_object('evaluation_id',
                    md5('scale-arena-'||((n-1) %% %s+1))::uuid),
                  'background',jsonb_build_object('authorization_id',
                    md5('scale-grant-'||((n-1) %% %s+1))::uuid)))
            FROM generate_series(1,%s) AS n""",
            (LOAD_MARKER, project_id, HISTORY_PARENTS, HISTORY_PARENTS, HISTORY_RUNS),
        )
        connection.execute(
            """INSERT INTO actions(id,run_id,kind,status,data)
            SELECT md5('scale-action-'||n)::uuid,md5('scale-run-'||n)::uuid,
              'model','completed',jsonb_build_object(
                'id',md5('scale-action-'||n)::uuid,
                'run_id',md5('scale-run-'||n)::uuid,
                'kind','model','status','completed','scale_index_load',%s)
            FROM generate_series(1,%s) AS n""",
            (LOAD_MARKER, HISTORY_RUNS),
        )
        # Analyze the actual populated schema; never substitute EXPLAIN for the
        # subsequent complete Worker.run observation.
        for table in LOAD_COUNTS:
            connection.execute(f"ANALYZE {table}")


def protected_accounting(rows):
    """Status/owner changes during real startup are distinguished from money."""
    background_runs = [
        row["data"] for row in rows["runs"] if row["data"]["request"].get("background")
    ]
    run_ids = {run["id"] for run in background_runs}
    return {
        "run_frozen_clock_usage": [
            {
                key: deepcopy(run.get(key))
                for key in ("id", "request", "first_started_at", "usage", "scheduling")
            }
            for run in background_runs
        ],
        "background_actions": [
            row for row in rows["actions"] if str(row["run_id"]) in run_ids
        ],
        "parents": [
            {
                key: value
                for key, value in row["data"].items()
                if key not in {"state", "reason", "updated_at"}
            }
            for row in rows["arena_evaluations"]
        ],
        "grants": rows["background_authorizations"],
        "allocations": rows["background_allocations"],
        "days": rows["background_days"],
    }


def cleanup_actual_resources(settings, store, handles):
    """Delete only exact, inspected IDs with this fixture's durable binding."""
    before = real_rows(store)
    print(
        "worker_scale_actual_durable_rows="
        + json.dumps(before, default=str, sort_keys=True)
    )
    reaped = []
    for process in handles:
        # Only actual, live direct-child Popen handles may be signalled. Durable
        # numeric PIDs are evidence, never cleanup authority.
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            reaped.append(process.pid)
        if process.stdout is not None:
            process.stdout.close()
    removed = []
    for row in before["runs"]:
        run_id = str(row["id"])
        names = {
            operation["data"]["scope"]["container_name"]
            for operation in before["controller_operations"]
            if operation["data"]["run_id"] == run_id
            and operation["data"]["scope"]["container_name"] is not None
        }
        listed = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=doonex.run_id=" + run_id,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        for identifier in listed.stdout.splitlines():
            assert re.fullmatch(r"[0-9a-f]{64}", identifier)
            inspected = subprocess.run(
                ["docker", "inspect", identifier],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            values = json.loads(inspected.stdout)
            assert len(values) == 1
            value = values[0]
            assert value["Id"] == identifier
            assert value["Config"]["Labels"]["doonex.run_id"] == run_id
            assert value["Name"].removeprefix("/") in names
            mounts = [
                item for item in value["Mounts"] if item["Destination"] == "/workspace"
            ]
            assert len(mounts) == 1 and mounts[0]["Type"] == "bind"
            assert (
                Path(mounts[0]["Source"])
                .resolve()
                .is_relative_to(run_directory(settings, run_id).resolve())
            )
            subprocess.run(
                ["docker", "rm", "-f", identifier],
                capture_output=True,
                timeout=15,
                check=True,
            )
            removed.append({"run_id": run_id, "container_id": identifier})
        remaining = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=doonex.run_id=" + run_id,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert not remaining.stdout.strip() and not remaining.stderr.strip()
    assert real_rows(store) == before
    print(
        "worker_scale_resource_cleanup="
        + json.dumps(
            {
                "removed_exact_ids": removed,
                "reaped_actual_live_handles": reaped,
                "all_actual_handles_reaped": all(
                    process.poll() is not None for process in handles
                ),
                "durable_rows_unchanged": True,
                "all_actual_run_label_queries_succeeded_and_empty": True,
            },
            sort_keys=True,
        )
    )


@pytest.mark.parametrize("mode", ["cold_start", "warm_ready"])
def test_actual_worker_scale_preserves_accounting_to_foreground_admission(
    tuning, store, request_data, tmp_path, monkeypatch, mode
):
    settings, background, body, _ = tuning
    source = tmp_path / "scale-foreground-source"
    source.mkdir()
    repo, git = repository(source)
    foreground_request = deepcopy(request_data)
    foreground_request.update(
        repo_path=str(repo),
        base_commit=git("rev-parse", "HEAD"),
        checks=["python -c 'assert True'"],
    )
    foreground_request["model"].update(model_id="gpt-4.1-mini")
    events, executions, calls, admissions = [], [], [], []
    handles = []

    def popen(*args, **kwargs):
        assert kwargs.get("start_new_session") is True
        process = subprocess.Popen(*args, **kwargs)
        handles.append(process)
        return process

    supervised = SimpleNamespace(**{**vars(subprocess), "Popen": popen})
    monkeypatch.setattr(sandbox_module, "subprocess", supervised)
    monkeypatch.setattr(workspace_module, "subprocess", supervised)
    state = {
        "phase": "setup",
        "active": None,
        "target": None,
        "enqueue": None,
        "warmup": None,
        "warmup_returned": None,
        "measured_worker": None,
    }
    original_execute, original_initialize = Worker.execute, Store.initialize
    original_reconcile, original_maintain = Worker.reconcile, Scheduler.maintain
    original_reserve, original_settle = Ledger.reserve, Ledger.settle
    original_intent, original_containers = Ledger.tool_intent, Sandbox.containers
    original_prepare, original_enabled = Background.prepare, Background.next_enabled

    def observe(kind, **details):
        events.append({"kind": kind, "monotonic": time.monotonic(), **details})

    def enqueue_target():
        assert state["target"] is None
        before = time.monotonic()
        target = store.create_run(foreground_request)
        after = time.monotonic()
        register_owned([target["id"]])
        state.update(target=target["id"], enqueue=[before, after])
        observe(
            "foreground_enqueue_commit_confirmed",
            run_id=target["id"],
            bracket=[before, after],
        )
        return target

    def execute(worker, run_id):
        assert state["active"] is None
        state["active"] = run_id
        executions.append(run_id)
        observe("actual_execute_enter", run_id=run_id, phase=state["phase"])
        try:
            return original_execute(worker, run_id)
        finally:
            observe("actual_execute_returned", run_id=run_id, phase=state["phase"])
            state["active"] = None
            if run_id == state["warmup"]:
                assert store.get(run_id)["state"] == "completed_verified"
                state["warmup_returned"] = time.monotonic()
            if run_id == state["target"] and state["phase"] == "measured":
                worker.stopping = True

    def initialize(current_store):
        observe("actual_initialize_enter", phase=state["phase"])
        value = original_initialize(current_store)
        observe("actual_initialize_returned", phase=state["phase"])
        return value

    def reconcile(worker, **kwargs):
        observe("actual_startup_reconcile_enter", phase=state["phase"])
        value = original_reconcile(worker, **kwargs)
        observe("actual_startup_reconcile_returned", phase=state["phase"])
        return value

    def maintain(scheduler, **kwargs):
        observe("actual_maintenance_enter", phase=state["phase"])
        value = original_maintain(scheduler, **kwargs)
        observe("actual_maintenance_returned", phase=state["phase"])
        return value

    def reserve(ledger, *args, **kwargs):
        value = original_reserve(ledger, *args, **kwargs)
        admission = {
            "run_id": ledger.run_id,
            "action_id": value["id"],
            "monotonic": time.monotonic(),
            "phase": state["phase"],
        }
        admissions.append(admission)
        observe("model_admission_commit_confirmed", **admission)
        return value

    def settle(ledger, reservation, actual):
        value = original_settle(ledger, reservation, actual)
        observe(
            "model_settlement_commit_confirmed",
            run_id=ledger.run_id,
            action_id=reservation["id"],
            known_usage=actual is not None,
        )
        return value

    def intent(ledger, *args, **kwargs):
        value = original_intent(ledger, *args, **kwargs)
        observe(
            "tool_admission_commit_confirmed", run_id=ledger.run_id, action_id=value
        )
        return value

    def containers(sandbox, run_id, **kwargs):
        value = original_containers(sandbox, run_id, **kwargs)
        observe("actual_container_probe_returned", run_id=run_id, containers=value)
        return value

    def next_enabled(current_background, *args, **kwargs):
        value = original_enabled(current_background, *args, **kwargs)
        observe(
            "actual_next_enabled_returned",
            phase=state["phase"],
            authorization_id=value["id"] if value else None,
        )
        return value

    def prepare(current_background, identifier, **kwargs):
        observe(
            "actual_background_prepare_enter",
            authorization_id=identifier,
            phase=state["phase"],
        )
        value = original_prepare(current_background, identifier, **kwargs)
        observe(
            "actual_background_prepare_returned",
            authorization_id=identifier,
            phase=state["phase"],
        )
        if mode == "warm_ready" and state["phase"] == "measured":
            assert state["warmup_returned"] is not None
            enqueue_target()
        return value

    def transport(*args, **kwargs):
        run_id = state["active"]
        assert run_id is not None
        calls.append(run_id)
        observe("synthetic_provider_enter", run_id=run_id, phase=state["phase"])
        if state["phase"] == "unknown":
            raise KeyboardInterrupt(
                "Synthetic model interruption for scale preservation"
            )
        if state["phase"] == "yield":
            enqueue_target()
            answer = response(
                "doonex_shell", {"command": "touch scale-must-not-replay"}, 0
            )
            answer.choices[0].message.tool_calls.extend(
                response("finish", {"message": "Synthetic yielded scale fixture"}, 1)
                .choices[0]
                .message.tool_calls
            )
            return answer
        assert run_id in {state["target"], state["warmup"]}
        assert not store.get(run_id)["request"].get("background")
        return response("finish", {"message": "Synthetic scale foreground"}, 0)

    monkeypatch.setattr(Worker, "execute", execute)
    monkeypatch.setattr(Store, "initialize", initialize)
    monkeypatch.setattr(Worker, "reconcile", reconcile)
    monkeypatch.setattr(Scheduler, "maintain", maintain)
    monkeypatch.setattr(Ledger, "reserve", reserve)
    monkeypatch.setattr(Ledger, "settle", settle)
    monkeypatch.setattr(Ledger, "tool_intent", intent)
    monkeypatch.setattr(Sandbox, "containers", containers)
    monkeypatch.setattr(Background, "prepare", prepare)
    monkeypatch.setattr(Background, "next_enabled", next_enabled)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    fingerprints_before = None
    try:
        settings, _, _, unknown_batch = claimed(tuning, store)
        unknown_id = unknown_batch["attempts"][0]["run_id"]
        register_owned([item["run_id"] for item in unknown_batch["attempts"]])
        state["phase"] = "unknown"
        with _failure_watchdog(events, seconds=90):
            Worker(settings).run(once=True)
        unknown = store.get(unknown_id)
        assert unknown["state"] == "interrupted"
        assert unknown["usage"]["calls"] == unknown["usage"]["unknown_calls"] == 1
        assert unknown["usage"]["reserved_tokens"] > 0
        assert [row["status"] for row in action_rows(store, unknown_id)] == ["unknown"]
        assert unknown["first_started_at"] is not None
        yielded_id = None
        if mode == "cold_start":
            state["phase"] = "setup"
            settings, _, _, yielded_batch = claimed(tuning, store)
            yielded_id = yielded_batch["attempts"][0]["run_id"]
            register_owned([item["run_id"] for item in yielded_batch["attempts"]])
            state["phase"] = "yield"
            with _failure_watchdog(events, seconds=90):
                Worker(settings).run(once=True)
            yielded = store.get(yielded_id)
            assert yielded["state"] == "yielded", yielded["reason"]
            assert yielded["usage"]["calls"] == 1
            assert (
                yielded["usage"]["unknown_calls"]
                == yielded["usage"]["reserved_tokens"]
                == 0
            )
            assert len(yielded["scheduling"]["receipt"]["pending_actions"]) == 2
            assert not (
                run_directory(settings, yielded_id) / "workspace/scale-must-not-replay"
            ).exists()
            setup_target = state["target"]
            store.transition(
                setup_target, "cancelled", "Synthetic user cancels setup foreground"
            )
            state.update(target=None, enqueue=None)
        state["phase"] = "setup"
        enabled = []
        for _ in range(ENABLED_SAMPLES):
            draft = background.create(body)
            enabled.append(background.enable(draft["id"], draft["revision"]))
        assert len({item["id"] for item in enabled}) == ENABLED_SAMPLES
        day = enabled[0]["date"]
        install_index_load(store, day, foreground_request["project_id"])
        fingerprints_before = index_fingerprints(store)
        rows_before = real_rows(store)
        accounting_before = protected_accounting(rows_before)
        measured_offset, execute_offset, call_offset = (
            len(events),
            len(executions),
            len(calls),
        )
        state["phase"] = "measured"
        if mode == "cold_start":
            enqueue_target()
        else:
            warmup = store.create_run(foreground_request)
            state["warmup"] = warmup["id"]
            register_owned([warmup["id"]])
        worker = Worker(settings)
        state["measured_worker"] = worker
        worker_started = time.monotonic()
        with _failure_watchdog(events, seconds=120):
            worker.run(once=mode == "cold_start")
        worker_returned = time.monotonic()
        assert state["target"] is not None and state["enqueue"] is not None
        target_id = state["target"]
        target = store.get(target_id)
        assert target["state"] == "completed_verified", target["reason"]
        assert target["usage"]["calls"] == 1
        assert (
            target["usage"]["unknown_calls"] == target["usage"]["reserved_tokens"] == 0
        )
        assert target["request"]["budget"] == foreground_request["budget"]
        assert not store.pending(target_id)
        expected_selected = (
            [target_id] if mode == "cold_start" else [state["warmup"], target_id]
        )
        assert executions[execute_offset:] == calls[call_offset:] == expected_selected
        selected_admissions = [
            item for item in admissions if item["run_id"] == target_id
        ]
        assert len(selected_admissions) == 1
        first_admission = selected_admissions[0]["monotonic"]
        enqueue_before, enqueue_after = state["enqueue"]
        assert enqueue_before <= enqueue_after <= first_admission <= worker_returned
        measured = events[measured_offset:]
        initialize_events = [
            item for item in measured if item["kind"] == "actual_initialize_returned"
        ]
        reconcile_events = [
            item
            for item in measured
            if item["kind"] == "actual_startup_reconcile_returned"
        ]
        assert len(initialize_events) == len(reconcile_events) == 1
        assert (
            worker_started
            <= initialize_events[0]["monotonic"]
            <= reconcile_events[0]["monotonic"]
        )
        assert reconcile_events[0]["monotonic"] <= first_admission
        if mode == "cold_start":
            assert enqueue_after <= worker_started
            assert store.get(yielded_id)["state"] == "yielded"
            assert any(
                item["kind"] == "actual_container_probe_returned"
                and item["run_id"] == yielded_id
                for item in measured
            )
        else:
            assert state["warmup_returned"] <= enqueue_before
            prepares = [
                item
                for item in measured
                if item["kind"] == "actual_background_prepare_returned"
            ]
            assert len(prepares) == 1
            assert prepares[0]["authorization_id"] in {item["id"] for item in enabled}
            assert (
                initialize_events[0]["monotonic"]
                <= state["warmup_returned"]
                <= prepares[0]["monotonic"]
                <= enqueue_before
            )
            enabled_queries = [
                item
                for item in measured
                if item["kind"] == "actual_next_enabled_returned"
            ]
            assert len(enabled_queries) == 1
            assert (
                enabled_queries[0]["authorization_id"]
                == prepares[0]["authorization_id"]
            )
        after_rows = real_rows(store)
        assert protected_accounting(after_rows) == accounting_before
        assert index_fingerprints(store) == fingerprints_before
        current_unknown = store.get(unknown_id)
        assert current_unknown["first_started_at"] == unknown["first_started_at"]
        assert current_unknown["usage"] == unknown["usage"]
        assert current_unknown["owner"] == worker.owner
        assert not [
            item
            for item in measured
            if item["kind"]
            in {"model_admission_commit_confirmed", "tool_admission_commit_confirmed"}
            and item["monotonic"] > enqueue_before
            and item["run_id"] not in {target_id, state["warmup"]}
        ]
        for run_id in expected_selected:
            assert run_id not in sdk._CALLBACKS
            assert run_id not in sdk._SCHEDULING
            assert run_id not in sdk._SESSION_STORES
        print(
            "worker_scale_wait="
            + json.dumps(
                {
                    "mode": mode,
                    "load_description": LOAD_MARKER,
                    "history_is_synthetic_index_load_not_completed_tasks": True,
                    "index_fingerprints_before_and_after": fingerprints_before,
                    "actual_enabled_authorizations": ENABLED_SAMPLES,
                    "enabled_count_is_a_sample_not_product_cap": True,
                    "actual_unknown_run_id": unknown_id,
                    "actual_yielded_run_id": yielded_id,
                    "target_run_id": target_id,
                    "warmup_run_id": state["warmup"],
                    "target_has_preceding_queued_foreground": False,
                    "worker_start_bracket_monotonic": [worker_started, worker_returned],
                    "enqueue_commit_bracket_monotonic": state["enqueue"],
                    "first_model_admission_commit_confirmed_monotonic": first_admission,
                    "submit_to_first_admission_seconds": {
                        "lower": first_admission - enqueue_after,
                        "upper": first_admission - enqueue_before,
                    },
                    "cold_worker_start_to_first_admission_seconds": first_admission
                    - worker_started
                    if mode == "cold_start"
                    else None,
                    "protected_accounting_before_and_after": accounting_before,
                    "events": measured,
                    "real_model_calls": 0,
                    "normal_wait_sla": False,
                },
                default=str,
                sort_keys=True,
            )
        )
    finally:
        print(
            "worker_scale_partial="
            + json.dumps(
                {
                    "mode": mode,
                    "phase": state["phase"],
                    "events": events,
                    "index_fingerprints_before": fingerprints_before,
                },
                default=str,
                sort_keys=True,
            )
        )
        cleanup_actual_resources(settings, store, handles)
