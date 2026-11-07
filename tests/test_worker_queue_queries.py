"""Read-only worker candidates and state filters use real PostgreSQL."""

import json
from uuid import UUID, uuid4

import pytest
from doonex.store import Store
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb, set_json_loads

pytestmark = pytest.mark.postgres


def insert_run(
    connection,
    *,
    state="queued",
    created_at="2026-11-06T00:00:00Z",
    request=None,
    identifier=None,
    marker="candidate",
):
    identifier = str(identifier or uuid4())
    value = {
        "id": identifier,
        "state": state,
        "created_at": created_at,
        "request": request or {"project_id": "project-a"},
        "marker": marker,
    }
    connection.execute(
        "INSERT INTO runs(id,state,data) VALUES(%s,%s,%s)",
        (identifier, state, Jsonb(value)),
    )
    return value


def insert_arena(connection, state="running"):
    identifier = str(uuid4())
    connection.execute(
        "INSERT INTO arena_evaluations(id,state,data) VALUES(%s,%s,%s)",
        (identifier, state, Jsonb({"id": identifier, "state": state})),
    )
    return {"evaluation_id": identifier}


def remove_candidate(store, run):
    with store.connect() as c:
        c.execute(
            "UPDATE runs SET state='cancelled', "
            "data=jsonb_set(data,'{state}','\"cancelled\"') WHERE id=%s",
            (run["id"],),
        )


def test_empty_queue_and_state_filter(store):
    assert store.next_queued_run() is None
    assert store.list_runs(states={"running", "interrupted"}, limit=None) == []


def test_queue_priority_then_fifo_is_independent_of_insert_order(store):
    with store.connect() as c:
        arena = insert_arena(c)
        background_request = {"arena": arena, "background": {"authorization_id": "x"}}
        manual_request = {"arena": arena}
        background_old = insert_run(c, request=background_request)
        manual_old = insert_run(c, request=manual_request)
        normal_new = insert_run(c, created_at="2026-11-06T03:00:00Z")
        manual_new = insert_run(
            c, request=manual_request, created_at="2026-11-06T02:00:00Z"
        )
        background_new = insert_run(
            c, request=background_request, created_at="2026-11-06T01:00:00Z"
        )
        normal_old = insert_run(c, created_at="2026-11-06T02:00:00Z")
    for expected in (
        normal_old,
        normal_new,
        manual_old,
        manual_new,
        background_old,
        background_new,
    ):
        assert store.next_queued_run() == expected
        # Reading a candidate neither claims nor removes it.
        assert store.next_queued_run() == expected
        assert store.get(expected["id"]) == expected
        remove_candidate(store, expected)
    assert store.next_queued_run() is None


@pytest.mark.parametrize("parent_state", ["queued", "running"])
def test_queue_accepts_only_active_parent_arena_states(store, parent_state):
    with store.connect() as c:
        run = insert_run(c, request={"arena": insert_arena(c, parent_state)})
    assert store.next_queued_run() == run


@pytest.mark.parametrize(
    "parent_state",
    ["complete", "cancelled", "budget_exceeded", "interrupted", "cancelling", "bogus"],
)
def test_queue_excludes_non_runnable_parent_arena(store, parent_state):
    with store.connect() as c:
        insert_run(c, request={"arena": insert_arena(c, parent_state)})
    assert store.next_queued_run() is None


@pytest.mark.parametrize(
    "arena",
    [
        {"evaluation_id": str(uuid4())},
        {"evaluation_id": "not-a-uuid"},
        {"evaluation_id": "' OR true --"},
        {"evaluation_id": None},
        {},
        None,
        [],
    ],
)
def test_queue_excludes_missing_or_malformed_parent_without_uuid_cast(store, arena):
    with store.connect() as c:
        insert_run(c, request={"arena": arena})
    assert store.next_queued_run() is None


def test_queue_uses_stable_id_order_for_equal_created_at(store):
    with store.connect() as c:
        records = {
            number: insert_run(c, identifier=UUID(int=number)) for number in (3, 1, 2)
        }
    assert store.next_queued_run() == records[1]
    assert store.list_runs(states={"queued"}) == [records[3], records[2], records[1]]
    remove_candidate(store, records[1])
    assert store.next_queued_run() == records[2]


def test_state_and_project_filter_are_combined_before_order_and_limit(store):
    with store.connect() as c:
        old = insert_run(c, state="running")
        new = insert_run(c, state="yielded", created_at="2026-11-06T01:00:00Z")
        insert_run(c, state="completed_verified", created_at="2026-11-06T02:00:00Z")
        insert_run(
            c,
            state="running",
            request={"project_id": "project-b"},
            created_at="2026-11-06T03:00:00Z",
        )
    arguments = {"project_id": "project-a", "states": {"running", "yielded"}}
    assert store.list_runs(limit=None, **arguments) == [new, old]
    assert store.list_runs(limit=1, **arguments) == [new]
    assert store.list_runs(limit=0, **arguments) == []
    assert store.list_runs(project_id="' OR true --", states={"running"}) == []
    assert store.list_runs(states=["yielded", "yielded"]) == [new]


def test_history_is_filtered_in_postgres_and_only_selected_data_is_decoded(
    store, monkeypatch
):
    with store.connect() as c:
        c.execute(
            """INSERT INTO runs(id,state,data)
            SELECT md5(n::text)::uuid,'completed_verified',jsonb_build_object(
                'id',md5(n::text),'state','completed_verified',
                'created_at','2026-11-06T23:00:00Z',
                'request',jsonb_build_object('project_id','project-a'),
                'marker','historical','payload',repeat('x',4096))
            FROM generate_series(1,3000) AS n"""
        )
        candidate = insert_run(c)
        insert_run(c, created_at="2026-11-06T01:00:00Z")
        yielded = insert_run(c, state="yielded")
        interrupted = insert_run(c, state="interrupted")
    original_connect = store.connect
    decoded = []

    def loads(value):
        record = json.loads(value)
        assert record.get("marker") != "historical", "Terminal history was decoded"
        decoded.append(record["id"])
        return record

    def tracked_connect():
        connection = original_connect()
        set_json_loads(loads, context=connection)
        return connection

    monkeypatch.setattr(store, "connect", tracked_connect)
    assert store.next_queued_run() == candidate
    assert decoded == [candidate["id"]]
    decoded.clear()
    assert store.list_runs(states={"yielded", "interrupted"}, limit=None) == sorted(
        [yielded, interrupted], key=lambda run: run["id"], reverse=True
    )
    assert set(decoded) == {yielded["id"], interrupted["id"]}
    assert len(decoded) == 2


def test_queries_work_in_read_only_transactions_without_claim_or_dispatch(store):
    with store.connect() as c:
        run = insert_run(c)
    options = conninfo_to_dict(store.database_url)["options"]
    readonly = Store(
        make_conninfo(
            store.database_url,
            options=options + " -c default_transaction_read_only=on",
        )
    )
    assert readonly.next_queued_run() == run
    assert readonly.list_runs(states={"queued"}) == [run]
    assert store.get(run["id"]) == run
    assert store.events(run["id"])["events"] == []
    assert store.pending(run["id"]) == []


@pytest.mark.parametrize("states", [[], (), set(), frozenset()])
def test_empty_state_filter_never_falls_back_to_all_runs(store, states):
    with store.connect() as c:
        insert_run(c)
    assert store.list_runs(states=states) == []


@pytest.mark.parametrize(
    "states",
    [
        "queued",
        "",
        True,
        1,
        {"queued": True},
        ["bogus"],
        [None],
        [False],
        [[]],
        ["queued", "' OR true --"],
    ],
)
def test_invalid_state_filters_are_rejected(store, states):
    with pytest.raises(ValueError, match="状态"):
        store.list_runs(states=states)


@pytest.mark.parametrize("limit", [-1, True, False, 1.5, "1", []])
def test_invalid_limits_are_rejected(store, limit):
    with pytest.raises(ValueError, match="上限"):
        store.list_runs(limit=limit, states={"queued"})
