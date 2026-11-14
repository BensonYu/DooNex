"""Finite maintenance candidates preserve unknown reservations and ownership."""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from doonex.arena import Arena
from doonex.background import BackgroundConflict, BackgroundWaiting
from doonex.scheduling import Scheduler
from psycopg.types.json import Jsonb
from test_background import attached, enabled, finish
from test_background import background as background_fixture
from test_background import taskset_source as source_fixture

background = background_fixture
taskset_source = source_fixture
pytestmark = pytest.mark.postgres


def scheduler_for(background):
    scheduler = Scheduler(background.settings, background.store)
    scheduler.background = background
    return scheduler


def allocations(store):
    with store.connect() as c:
        return c.execute(
            "SELECT id,authorization_id,evaluation_id,day,state,data "
            "FROM background_allocations ORDER BY id"
        ).fetchall()


def test_one_terminal_allocation_per_call_and_settled_history_is_skipped(
    background, request_data
):
    background, body, clock = background
    pairs = [attached(background, body, request_data) for _ in range(2)]
    for _, batch in pairs:
        finish(background, batch, state="failed", calls=1, tokens=5)
    clock[0] += timedelta(seconds=20, microseconds=1)
    scheduler = scheduler_for(background)
    scheduler.maintain()
    rows = allocations(background.store)
    assert [row["state"] for row in rows] == ["settled", "reserved"]
    assert rows[0]["data"]["actual"]["seconds"] == 21
    assert rows[0]["data"]["actual"]["calls"] == 1
    first = deepcopy(rows[0])
    scheduler.maintain()
    assert all(row["state"] == "settled" for row in allocations(background.store))
    clock[0] += timedelta(hours=1)
    scheduler.maintain()
    assert allocations(background.store)[0] == first


def test_unknown_candidate_advances_and_wraps_without_freeing_reservation(
    background, request_data
):
    background, body, clock = background
    pairs = [attached(background, body, request_data) for _ in range(2)]
    by_id = {batch["background"]["reservation_id"]: batch for _, batch in pairs}
    identifiers = sorted(by_id)
    first = finish(background, by_id[identifiers[0]], unknown_calls=1)
    finish(background, by_id[identifiers[1]], calls=1)
    clock[0] += timedelta(seconds=2)
    scheduler = scheduler_for(background)
    scheduler.maintain()
    before = allocations(background.store)[0]
    assert before["state"] == "reserved"
    scheduler.maintain()
    assert [r["state"] for r in allocations(background.store)] == [
        "reserved",
        "settled",
    ]
    scheduler.maintain()
    assert allocations(background.store)[0] == before
    assert scheduler._maintenance_cursor == identifiers[0]
    assert background.day(first["background"]["date"])["reserved"]["calls"] > 0


@pytest.mark.parametrize(
    "location,key,value",
    [
        ("allocation", "id", None),
        ("allocation", "authorization_id", str(uuid4())),
        ("allocation", "evaluation_id", None),
        ("allocation", "date", "2031-04-06"),
        ("parent", "id", str(uuid4())),
        ("parent", "state", "running"),
        ("parent", "background", None),
        ("parent", "background", {}),
        ("grant", "id", str(uuid4())),
        ("grant", "state", "enabled"),
        ("grant", "evaluation_id", str(uuid4())),
        ("grant", "date", "2031-04-06"),
    ],
)
def test_payload_binding_corruption_fails_visibly_and_keeps_exact_ledger(
    background, request_data, location, key, value
):
    background, body, _ = background
    grant, batch = attached(background, body, request_data)
    finish(background, batch)
    table, identifier = {
        "allocation": ("background_allocations", batch["background"]["reservation_id"]),
        "parent": ("arena_evaluations", batch["id"]),
        "grant": ("background_authorizations", grant["id"]),
    }[location]
    with background.store.connect() as c:
        c.execute(
            f"UPDATE {table} SET data=jsonb_set(data,%s,%s) WHERE id=%s",
            ([key], Jsonb(value), identifier),
        )
    rows = allocations(background.store)
    day = background.day(grant["date"])
    with pytest.raises(BackgroundConflict, match="关联无效.*保留预留"):
        scheduler_for(background).maintain()
    assert allocations(background.store) == rows
    assert background.day(grant["date"]) == day


@pytest.mark.parametrize("change", ["null_parent", "foreign_parent", "foreign_grant"])
def test_column_binding_corruption_is_selected_and_rejected(
    background, request_data, change
):
    background, body, _ = background
    grant, batch = attached(background, body, request_data)
    finish(background, batch)
    identifier = batch["background"]["reservation_id"]
    with background.store.connect() as c:
        if change == "null_parent":
            c.execute(
                "UPDATE background_allocations SET evaluation_id=NULL,"
                "data=jsonb_set(data,'{evaluation_id}','null') WHERE id=%s",
                (identifier,),
            )
        elif change == "foreign_parent":
            other = str(uuid4())
            c.execute(
                "INSERT INTO arena_evaluations VALUES(%s,'complete','{}')", (other,)
            )
            c.execute(
                "UPDATE background_allocations SET evaluation_id=%s,"
                "data=jsonb_set(data,'{evaluation_id}',%s) WHERE id=%s",
                (other, Jsonb(other), identifier),
            )
        else:
            other = str(uuid4())
            c.execute(
                "INSERT INTO background_authorizations VALUES(%s,'consumed','{}')",
                (other,),
            )
            c.execute(
                "UPDATE background_allocations SET authorization_id=%s,"
                "data=jsonb_set(data,'{authorization_id}',%s) WHERE id=%s",
                (other, Jsonb(other), identifier),
            )
    before = allocations(background.store)
    assert background.next_reserved() == identifier
    with pytest.raises(BackgroundConflict, match="关联无效"):
        scheduler_for(background).maintain()
    assert allocations(background.store) == before
    assert background.day(grant["date"])["reserved"]["calls"] > 0


def test_missing_allocation_or_already_settled_is_not_replayed(
    background, request_data
):
    background, body, _ = background
    grant, batch = attached(background, body, request_data)
    batch = finish(background, batch)
    with background.store.connect() as c:
        with pytest.raises(BackgroundConflict):
            background.maintenance_batch(c, str(uuid4()))
        settled = background.settle(c, batch["id"], batch)
    with background.store.connect() as c:
        assert background.maintenance_batch(c, settled["id"], lock=True) is None
    assert background.next_reserved() is None
    assert background.get(grant["id"])["state"] == "consumed"


@pytest.mark.parametrize("missing", ["parent", "grant"])
def test_missing_bound_record_is_not_hidden_by_a_join(
    background, request_data, missing
):
    background, body, _ = background
    grant, batch = attached(background, body, request_data)
    table, identifier = {
        "parent": ("arena_evaluations", batch["id"]),
        "grant": ("background_authorizations", grant["id"]),
    }[missing]
    with background.store.connect() as c:
        # Simulate a legacy damaged database; ordinary writers cannot bypass FK.
        constraint = c.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid="
            "'background_allocations'::regclass AND confrelid=%s::regclass",
            (table,),
        ).fetchone()["conname"]
        c.execute(f'ALTER TABLE background_allocations DROP CONSTRAINT "{constraint}"')
        c.execute(f"DELETE FROM {table} WHERE id=%s", (identifier,))
    before = allocations(background.store)
    with pytest.raises(BackgroundConflict, match="关联无效"):
        scheduler_for(background).maintain()
    assert allocations(background.store) == before


@pytest.mark.parametrize("location", ["allocation", "parent", "grant"])
def test_missing_binding_key_keeps_reserved_allocation(
    background, request_data, location
):
    background, body, _ = background
    grant, batch = attached(background, body, request_data)
    table, identifier, key = {
        "allocation": (
            "background_allocations",
            batch["background"]["reservation_id"],
            "evaluation_id",
        ),
        "parent": ("arena_evaluations", batch["id"], "background"),
        "grant": ("background_authorizations", grant["id"], "evaluation_id"),
    }[location]
    with background.store.connect() as c:
        c.execute(f"UPDATE {table} SET data=data-%s WHERE id=%s", (key, identifier))
    before = allocations(background.store)
    with pytest.raises(BackgroundConflict, match="关联无效"):
        scheduler_for(background).maintain()
    assert allocations(background.store) == before


def test_enabled_keyset_ties_and_history_list_remain_complete(background):
    background, body, _ = background
    grants = [enabled(background, body) for _ in range(3)]
    ordered = sorted(grants, key=lambda item: (item["created_at"], item["id"]))
    cursor = None
    for grant in ordered:
        assert background.next_enabled(cursor) == grant
        cursor = (grant["created_at"], grant["id"])
    assert background.next_enabled(cursor) is None
    disabled = background.disable(ordered[1]["id"], ordered[1]["revision"])
    assert (
        background.next_enabled((ordered[0]["created_at"], ordered[0]["id"]))
        == ordered[2]
    )
    assert disabled in background.list() and len(background.list()) == 3


@pytest.mark.parametrize(
    "stamp", [None, "missing", "not-a-time", "2031-04-05T10:00:00"]
)
def test_bad_enabled_timestamp_is_visible_even_after_a_valid_cursor(background, stamp):
    background, body, _ = background
    grant = enabled(background, body)
    with background.store.connect() as c:
        if stamp == "missing":
            c.execute(
                "UPDATE background_authorizations SET data=data-'created_at' WHERE id=%s",
                (grant["id"],),
            )
        else:
            c.execute(
                "UPDATE background_authorizations SET data=jsonb_set(data,'{created_at}',%s) "
                "WHERE id=%s",
                (Jsonb(stamp), grant["id"]),
            )
    scheduler = scheduler_for(background)
    scheduler._authorization_cursor = ("9999-01-01T00:00:00Z", str(UUID(int=1)))
    with pytest.raises(BackgroundConflict, match="候选授权"):
        scheduler.claim_next()


def test_waiting_candidates_advance_one_per_call_then_wrap(background, monkeypatch):
    background, body, _ = background
    grants = sorted(
        [enabled(background, body) for _ in range(3)],
        key=lambda item: (item["created_at"], item["id"]),
    )
    seen = []

    def waiting(identifier, **kwargs):
        seen.append(identifier)
        raise BackgroundWaiting("尚未开始")

    monkeypatch.setattr(background, "prepare", waiting)
    scheduler = scheduler_for(background)
    for expected in grants + grants[:1]:
        assert scheduler.claim_next() is None
        assert seen[-1] == expected["id"]
    assert len(seen) == 4
    assert allocations(background.store) == []


@pytest.mark.parametrize("first_status", ["future", "blocked"])
def test_first_refused_candidate_does_not_starve_later_ready_grant(
    background, monkeypatch, first_status
):
    background, body, clock = background
    first_body = deepcopy(body)
    if first_status == "future":
        first_body["window_start"] = "2031-04-05T10:30:00Z"
    first = enabled(background, first_body)
    clock[0] += timedelta(microseconds=1)
    second = enabled(background, body)
    prepare = background.prepare

    def checked(identifier, **kwargs):
        if first_status == "blocked" and identifier == first["id"]:
            raise OSError("PRIVATE_DETAIL")
        return prepare(identifier, **kwargs)

    monkeypatch.setattr(background, "prepare", checked)
    scheduler = scheduler_for(background)
    assert scheduler.claim_next() is None
    batch = scheduler.claim_next()
    assert batch["background"]["authorization_id"] == second["id"]
    assert background.get(first["id"])["state"] == "enabled"
    assert background.get(first["id"])["dispatch_status"]["state"] == (
        "waiting" if first_status == "future" else "blocked"
    )


def test_foreground_arrival_during_prepare_blocks_final_admission(
    background, request_data, monkeypatch
):
    background, body, _ = background
    grant = enabled(background, body)
    prepare = background.prepare

    def enqueue_during_inspection(identifier, **kwargs):
        receipt = prepare(identifier, **kwargs)
        background.store.create_run(request_data)
        return receipt

    monkeypatch.setattr(background, "prepare", enqueue_during_inspection)
    assert scheduler_for(background).claim_next() is None
    assert allocations(background.store) == []
    assert background.get(grant["id"])["state"] == "enabled"


def test_arena_internal_states_filter_does_not_shorten_public_history(background):
    background, _, _ = background
    arena = Arena(background.settings, background.store)
    rows = []
    with background.store.connect() as c:
        for state in ("running", "complete", "cancelling", "interrupted"):
            row = {
                "id": str(uuid4()),
                "state": state,
                "created_at": "2031-01-01T00:00:00Z",
            }
            c.execute(
                "INSERT INTO arena_evaluations VALUES(%s,%s,%s)",
                (row["id"], state, Jsonb(row)),
            )
            rows.append(row)
    assert len(arena.list()) == 4
    assert {r["state"] for r in arena.list(states={"running", "cancelling"})} == {
        "running",
        "cancelling",
    }
    assert arena.list(states=[]) == []
    for invalid in (
        "running",
        ["completed_verified"],
        [None],
        [[]],
        {"unknown"},
        ["running') OR true --"],
    ):
        with pytest.raises(ValueError, match="状态"):
            arena.list(states=invalid)
