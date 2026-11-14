"""Finite Arena maintenance on actual PostgreSQL, without model dispatch."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import pytest
from doonex.arena import ACTIVE_BATCHES, MAX_PLANNED_ATTEMPTS, Arena
from doonex.budget import BatchHalt, Ledger
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.store import now
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


def clock_at(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: clock[0])
    return clock


def rows(store):
    with store.connect() as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("arena_evaluations", "runs", "actions", "events")
        }


def insert_evaluation(store, *, state="running", identifier=None, **changes):
    value = {
        "id": identifier or str(uuid4()),
        "state": state,
        "reason": "Controlled maintenance fixture",
        "created_at": now(),
        "updated_at": now(),
        "first_started_at": now(),
        "attempts": [],
        **changes,
    }
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO arena_evaluations(id,state,data) VALUES(%s,%s,%s)",
            (value["id"], state, Jsonb(value)),
        )
    return value


@pytest.fixture
def evaluation(settings, store, request_data):
    arena = Arena(settings, store)
    sample = store.create_run(deepcopy(request_data))
    usage = deepcopy(sample["usage"])
    value = insert_evaluation(
        store, usage=usage, batch_budget=deepcopy(request_data["budget"])
    )
    children = []
    for index, state in enumerate(("queued", "yielded", "running")):
        request = deepcopy(request_data)
        request["arena"] = {"evaluation_id": value["id"]}
        run = sample if index == 0 else store.create_run(request)
        with store.locked(run["id"]) as (_, current):
            current["request"]["arena"] = {"evaluation_id": value["id"]}
            current.update(state=state, first_started_at=now(), requested_action=None)
        children.append(run["id"])
    with arena.locked(value["id"]) as (_, current):
        current["attempts"] = [{"run_id": identifier} for identifier in children]
    # Keep a genuine unknown reservation throughout every expiry and refusal.
    ledger = Ledger(store, children[-1])
    ticket = ledger.reserve(2, 3)
    ledger.settle(ticket, None)
    assert store.get(children[-1])["usage"]["unknown_calls"] == 1
    return arena, value["id"], children


def observe_connections(
    monkeypatch, store, *, after_execute=None, after_read=None, after_commit=None
):
    """Observe actual psycopg work; never replace rows or transaction behavior."""
    connect = store.connect
    statements = []

    class Cursor:
        def __init__(self, cursor, query):
            self.cursor, self.query = cursor, query

        def fetchone(self):
            result = self.cursor.fetchone()
            if after_read:
                after_read(self.query, "one", result)
            return result

        def fetchall(self):
            result = self.cursor.fetchall()
            if after_read:
                after_read(self.query, "all", result)
            return result

        def __iter__(self):
            for result in self.cursor:
                if after_read:
                    after_read(self.query, "item", result)
                yield result
            if after_read:
                after_read(self.query, "end", None)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, connection, local):
            self.connection, self.local = connection, local

        def execute(self, query, parameters=None, **kwargs):
            result = self.connection.execute(query, parameters, **kwargs)
            self.local.append(query)
            statements.append((query, parameters))
            if after_execute:
                after_execute(query)
            return Cursor(result, query)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def observed():
        local = []
        with connect() as connection:
            yield Connection(connection, local)
        if after_commit:
            after_commit(local)

    monkeypatch.setattr(store, "connect", observed)
    return statements


@pytest.mark.parametrize("capacity", [True, False, 0, -1, 1.5, "64"])
def test_invalid_census_capacity_rejects_before_database(
    settings, store, monkeypatch, capacity
):
    arena = Arena(settings, store)
    monkeypatch.setattr(
        store, "connect", lambda: pytest.fail("Invalid capacity reached PostgreSQL")
    )
    with pytest.raises(ValueError, match="容量"):
        arena.list(capacity=capacity)


def test_cap_plus_one_census_never_silently_hides_active_batches(
    settings, store, monkeypatch
):
    arena = Arena(settings, store)
    active = [insert_evaluation(store) for _ in range(64)]
    history = [insert_evaluation(store, state="complete") for _ in range(3)]
    clock_at(monkeypatch)
    statements = observe_connections(monkeypatch, store)
    result = arena.list(states=ACTIVE_BATCHES, capacity=64, deadline=101.0)
    assert {value["id"] for value in result} == {value["id"] for value in active}
    assert statements[-1][1] == (65,)
    assert statements[-1][0].endswith(" LIMIT %s")
    assert len(arena.list()) == len(active) + len(history)
    insert_evaluation(store)
    before = rows(store)
    with pytest.raises(ValueError, match="超过维护容量"):
        arena.list(states=ACTIVE_BATCHES, capacity=64, deadline=101.0)
    assert rows(store) == before


def test_capped_census_uses_existing_index_without_historical_sort(
    settings, store, monkeypatch
):
    arena = Arena(settings, store)
    created = now()
    records = []
    for index in range(10_065):
        identifier = str(uuid4())
        state = "complete" if index < 10_000 else sorted(ACTIVE_BATCHES)[index % 3]
        records.append(
            (
                identifier,
                state,
                Jsonb({"id": identifier, "state": state, "created_at": created}),
            )
        )
    with store.connect() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO arena_evaluations(id,state,data) VALUES(%s,%s,%s)",
                records,
            )
        connection.execute("ANALYZE arena_evaluations")
        index = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
            "AND indexname='arena_state_order_v1'"
        ).fetchone()["indexdef"]
    clock_at(monkeypatch)
    statements = observe_connections(monkeypatch, store)
    with pytest.raises(ValueError, match="超过维护容量"):
        arena.list(states=ACTIVE_BATCHES, capacity=64, deadline=101.0)
    query, parameters = statements[-1]
    assert "ORDER BY state,data->>'created_at' DESC,id LIMIT %s" in query
    assert parameters == (65,)
    plans = {}
    with store.connect() as connection:
        for mode in ("natural", "seqscan_off_index_applicability_only"):
            if mode != "natural":
                connection.execute("SET LOCAL enable_seqscan=off")
            plans[mode] = connection.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, parameters
            ).fetchone()["QUERY PLAN"][0]

    def nodes(value):
        yield value
        for child in value.get("Plans", []):
            yield from nodes(child)

    for plan in plans.values():
        actual = list(nodes(plan["Plan"]))
        assert not {"Sort", "Incremental Sort", "Seq Scan"} & {
            node["Node Type"] for node in actual
        }
        assert any(node.get("Index Name") == "arena_state_order_v1" for node in actual)
        assert plan["Plan"]["Actual Rows"] == 65
    # The natural plan is the measured 10k-history sample. Disabling seqscan
    # separately demonstrates index applicability, never a general planner or
    # end-to-end wait guarantee. Preserve both actual plans in the test receipt.
    print(json.dumps({"index_definition": index, "plans": plans}, indent=2))


@pytest.mark.parametrize("binding", ["id", "state", "unknown_state"])
def test_census_rejects_malformed_column_json_binding(
    settings, store, monkeypatch, binding
):
    value = insert_evaluation(store)
    with store.connect() as connection:
        if binding == "unknown_state":
            value["state"] = "unexpected"
            connection.execute(
                "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
                (value["state"], Jsonb(value), value["id"]),
            )
        else:
            changed = deepcopy(value)
            changed[binding] = str(uuid4()) if binding == "id" else "complete"
            connection.execute(
                "UPDATE arena_evaluations SET data=%s WHERE id=%s",
                (Jsonb(changed), value["id"]),
            )
    arena = Arena(settings, store)
    before = rows(store)
    clock_at(monkeypatch)
    with pytest.raises(ValueError, match="状态绑定"):
        arena.list(capacity=64, deadline=101.0)
    assert rows(store) == before


@pytest.mark.parametrize("stage", ["entry", "query", "iteration", "iteration_end"])
def test_census_rechecks_same_deadline_after_real_query_and_iteration(
    settings, store, monkeypatch, stage
):
    arena = Arena(settings, store)
    insert_evaluation(store)
    before = rows(store)
    clock = clock_at(monkeypatch)

    def execute(query):
        if stage == "query" and "FROM arena_evaluations" in query:
            clock[0] = 102.0

    def read(query, kind, result):
        if "FROM arena_evaluations" in query and (
            (stage == "iteration" and kind == "item")
            or (stage == "iteration_end" and kind == "end")
        ):
            clock[0] = 102.0

    statements = observe_connections(
        monkeypatch, store, after_execute=execute, after_read=read
    )
    if stage == "entry":
        clock[0] = 102.0
    with pytest.raises(ExecutionDeadlineExceeded):
        arena.list(states=ACTIVE_BATCHES, capacity=64, deadline=101.0)
    if stage == "entry":
        assert statements == []
    assert rows(store) == before


@pytest.mark.parametrize("stage", ["get", "parent_lock", "children", "write"])
def test_sync_expiry_rolls_back_whole_current_transaction(
    evaluation, store, monkeypatch, stage
):
    arena, identifier, children = evaluation
    for run_id in children:
        store.transition(run_id, "cancelled", "Controlled terminal child")
    before = rows(store)
    clock = clock_at(monkeypatch)

    def execute(query):
        if stage == "write" and query.startswith("UPDATE arena_evaluations"):
            clock[0] = 102.0

    def read(query, kind, result):
        if (
            (stage == "get" and query.startswith("SELECT data FROM arena_evaluations"))
            or (stage == "parent_lock" and "FOR UPDATE" in query)
            or (stage == "children" and "FROM runs WHERE id=ANY" in query)
        ):
            clock[0] = 102.0

    observe_connections(monkeypatch, store, after_execute=execute, after_read=read)
    with pytest.raises(ExecutionDeadlineExceeded):
        arena.sync(identifier, deadline=101.0)
    assert rows(store) == before


def test_successful_sync_preserves_clocks_actions_and_unknown_reservations(
    evaluation, store, monkeypatch
):
    arena, identifier, children = evaluation
    for run_id in children:
        store.transition(run_id, "cancelled", "Controlled terminal child")
    before = rows(store)
    original = arena.get(identifier)
    clock_at(monkeypatch)
    arena.sync(identifier, deadline=101.0)
    current = arena.get(identifier)
    assert current["state"] == "complete"
    assert current["attempts"] == original["attempts"]
    assert current["usage"] == original["usage"]
    assert current["first_started_at"] == original["first_started_at"]
    after = rows(store)
    assert {
        key: value for key, value in after.items() if key != "arena_evaluations"
    } == {key: value for key, value in before.items() if key != "arena_evaluations"}


@pytest.mark.parametrize(
    "change", ["empty", "duplicate", "missing", "parent", "child_id", "child_state"]
)
def test_sync_retains_complete_frozen_plan_and_child_binding_checks(
    evaluation, store, monkeypatch, change
):
    arena, identifier, children = evaluation
    if change in {"empty", "duplicate", "missing"}:
        with arena.locked(identifier) as (_, value):
            if change == "empty":
                value["attempts"] = []
            elif change == "duplicate":
                value["attempts"][1] = deepcopy(value["attempts"][0])
            else:
                value["attempts"][0]["run_id"] = str(uuid4())
    else:
        with store.connect() as connection:
            run = store.get(children[0])
            if change == "parent":
                run["request"]["arena"]["evaluation_id"] = str(uuid4())
            elif change == "child_id":
                run["id"] = str(uuid4())
            else:
                run["state"] = "cancelled"
            connection.execute(
                "UPDATE runs SET data=%s WHERE id=%s", (Jsonb(run), children[0])
            )
    before = rows(store)
    clock_at(monkeypatch)
    with pytest.raises(ValueError, match="评测子运行"):
        arena.sync(identifier, deadline=101.0)
    assert rows(store) == before


def test_sync_accepts_complete_maximum_plan_and_rejects_next_attempt(
    settings, store, request_data, monkeypatch
):
    arena = Arena(settings, store)
    value = insert_evaluation(store)
    attempts = []
    with store.connect() as connection:
        for _ in range(MAX_PLANNED_ATTEMPTS):
            request = {
                **deepcopy(request_data),
                "arena": {"evaluation_id": value["id"]},
            }
            child = store.create_run(request, connection)
            attempts.append({"run_id": child["id"]})
    with arena.locked(value["id"]) as (_, current):
        current["attempts"] = attempts
    clock_at(monkeypatch)
    arena.sync(value["id"], deadline=101.0)
    assert arena.get(value["id"])["attempts"] == attempts
    with arena.locked(value["id"]) as (_, current):
        current["attempts"].append({"run_id": str(uuid4())})
    before = rows(store)
    with pytest.raises(ValueError, match="计划数量"):
        arena.sync(value["id"], deadline=101.0)
    assert rows(store) == before


@pytest.mark.parametrize("operation", ["sync", "stop"])
@pytest.mark.parametrize("binding", ["id", "state"])
def test_maintenance_rejects_parent_binding_before_any_child_write(
    evaluation, store, monkeypatch, operation, binding
):
    arena, identifier, _ = evaluation
    with store.connect() as connection:
        changed = arena.get(identifier)
        changed[binding] = str(uuid4()) if binding == "id" else "cancelled"
        connection.execute(
            "UPDATE arena_evaluations SET data=%s WHERE id=%s",
            (Jsonb(changed), identifier),
        )
    before = rows(store)
    clock_at(monkeypatch)
    with pytest.raises(ValueError, match="评测标识或状态绑定"):
        if operation == "sync":
            arena.sync(identifier, deadline=101.0)
        else:
            arena.stop(identifier, "cancelling", "Must reject", deadline=101.0)
    assert rows(store) == before


def test_stop_rejects_foreign_child_after_preserving_parent_stop(
    evaluation, store, monkeypatch
):
    arena, identifier, children = evaluation
    with store.locked(children[0]) as (_, run):
        run["request"]["arena"]["evaluation_id"] = str(uuid4())
    before = rows(store)
    clock_at(monkeypatch)
    with pytest.raises(ValueError, match="子运行归属或状态绑定"):
        arena.stop(identifier, "cancelling", "Controlled stop", deadline=101.0)
    after = rows(store)
    assert arena.get(identifier)["state"] == "cancelling"
    assert after["runs"] == before["runs"]
    assert after["actions"] == before["actions"]
    assert after["events"] == before["events"]


def test_stop_preserves_committed_parent_and_child_on_later_expiry(
    evaluation, store, monkeypatch
):
    arena, identifier, children = evaluation
    before = rows(store)
    original = arena.get(identifier)
    clock = clock_at(monkeypatch)

    def committed(statements):
        if any(query.startswith("UPDATE runs") for query in statements):
            clock[0] = 102.0

    observe_connections(monkeypatch, store, after_commit=committed)
    with pytest.raises(ExecutionDeadlineExceeded):
        arena.stop(identifier, "cancelling", "Controlled partial stop", deadline=101.0)
    assert arena.get(identifier)["state"] == "cancelling"
    assert store.get(children[0])["state"] == "cancelled"
    assert [store.get(run_id)["state"] for run_id in children[1:]] == [
        "yielded",
        "running",
    ]
    after = rows(store)
    assert after["actions"] == before["actions"]
    for row in after["runs"]:
        prior = next(item["data"] for item in before["runs"] if item["id"] == row["id"])
        assert row["data"]["usage"] == prior["usage"]
        assert row["data"]["first_started_at"] == prior["first_started_at"]
    assert arena.get(identifier)["usage"] == original["usage"]
    assert arena.get(identifier)["first_started_at"] == original["first_started_at"]
    for admission in (
        lambda: Ledger(store, children[-1]).reserve(2, 3),
        lambda: Ledger(store, children[-1]).tool_intent("never dispatch"),
    ):
        with pytest.raises(BatchHalt):
            admission()
    assert rows(store) == after


@pytest.mark.parametrize("stage", ["parent_write", "child_write"])
def test_stop_late_write_expiry_rolls_back_only_current_transaction(
    evaluation, store, monkeypatch, stage
):
    arena, identifier, _ = evaluation
    before = rows(store)
    clock = clock_at(monkeypatch)

    def execute(query):
        prefix = (
            "UPDATE arena_evaluations" if stage == "parent_write" else "UPDATE runs"
        )
        if query.startswith(prefix):
            clock[0] = 102.0

    observe_connections(monkeypatch, store, after_execute=execute)
    with pytest.raises(ExecutionDeadlineExceeded):
        arena.stop(identifier, "cancelling", "Controlled expiry", deadline=101.0)
    after = rows(store)
    if stage == "parent_write":
        assert after == before
    else:
        assert arena.get(identifier)["state"] == "cancelling"
        assert after["runs"] == before["runs"]
        assert after["actions"] == before["actions"]
        assert after["events"] == before["events"]


@pytest.mark.parametrize("operation", ["sync", "stop"])
def test_actual_parent_lock_wait_rechecks_original_deadline(
    evaluation, store, monkeypatch, operation
):
    arena, identifier, _ = evaluation
    before = rows(store)
    real_monotonic, real_sleep = time.monotonic, time.sleep
    clock = clock_at(monkeypatch)
    ready = threading.Event()
    pids = []
    original_connect = store.connect

    @contextmanager
    def tracked_connect():
        with original_connect() as connection:
            if threading.current_thread() is not threading.main_thread():
                pids.append(connection.info.backend_pid)
                ready.set()
            yield connection

    def attempt():
        if operation == "sync":
            arena.sync(identifier, deadline=101.0)
        else:
            arena.stop(identifier, "cancelling", "Controlled wait", deadline=101.0)

    with original_connect() as blocker, original_connect() as observer:
        observer.autocommit = True
        blocker.execute(
            "SELECT id FROM arena_evaluations WHERE id=%s FOR UPDATE", (identifier,)
        )
        monkeypatch.setattr(store, "connect", tracked_connect)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(attempt)
            try:
                assert ready.wait(timeout=2)
                end = real_monotonic() + 2
                while real_monotonic() < end:
                    # sync first reads without a lock, then opens the locking
                    # transaction. Inspect all real connections observed so far.
                    waiting = observer.execute(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE pid=ANY(%s) AND wait_event_type='Lock'",
                        (pids,),
                    ).fetchall()
                    if waiting:
                        break
                    real_sleep(0.01)
                else:
                    pytest.fail("Actual Arena call did not wait on the parent row")
                clock[0] = 102.0
            finally:
                blocker.commit()
            with pytest.raises(ExecutionDeadlineExceeded):
                future.result(timeout=3)
    assert rows(store) == before


def test_stop_commits_and_releases_parent_before_waiting_for_child(
    evaluation, store, monkeypatch
):
    arena, identifier, children = evaluation
    before = rows(store)
    real_monotonic, real_sleep = time.monotonic, time.sleep
    clock = clock_at(monkeypatch)
    with store.connect() as blocker, store.connect() as observer:
        observer.autocommit = True
        blocker.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (children[0],))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                arena.stop,
                identifier,
                "cancelling",
                "Controlled child wait",
                deadline=101.0,
            )
            try:
                end = real_monotonic() + 2
                while real_monotonic() < end:
                    parent = observer.execute(
                        "SELECT state FROM arena_evaluations WHERE id=%s", (identifier,)
                    ).fetchone()
                    if parent["state"] == "cancelling":
                        break
                    real_sleep(0.01)
                else:
                    pytest.fail("Parent stop did not commit before child lock")
                with store.connect() as unlocked:
                    assert (
                        unlocked.execute(
                            "SELECT state FROM arena_evaluations "
                            "WHERE id=%s FOR UPDATE NOWAIT",
                            (identifier,),
                        ).fetchone()["state"]
                        == "cancelling"
                    )
                assert not future.done()
                clock[0] = 102.0
            finally:
                blocker.commit()
            with pytest.raises(ExecutionDeadlineExceeded):
                future.result(timeout=3)
    after = rows(store)
    assert after["runs"] == before["runs"]
    assert after["actions"] == before["actions"]
    assert after["events"] == before["events"]
    assert arena.get(identifier)["state"] == "cancelling"
