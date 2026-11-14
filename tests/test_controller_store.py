"""Store admission and controller Run binding against disposable PostgreSQL."""

import subprocess
import sys
import time
from uuid import uuid4

import pytest
from doonex import controller_operations as co
from doonex.responses_registry import MODE
from doonex.store import execution_fenced, external_execution_fenced
from psycopg.types.json import Jsonb


def registry(store):
    return co.ControllerRegistry(
        store,
        actor=co.fresh_actor("api"),
        check_owner=lambda: None,
        check_other_fences=external_execution_fenced,
    )


def reserve(store, **context):
    return registry(store).reserve(
        "git_validate",
        scope=co.scope(intent_digest="a" * 64, repository_digest="b" * 64),
        deadline_monotonic_ns=time.monotonic_ns() + 60_000_000_000,
        **context,
    )


def fences(store):
    with store.connect() as connection:
        return (
            external_execution_fenced(connection),
            co.fenced(connection),
            execution_fenced(connection),
        )


def evidence():
    return {
        "outcome": "success",
        "reason": "success",
        "local_process_unknown": False,
        "daemon_request_unknown": False,
        "docker": None,
    }


def actual_receipt(capability):
    permit = capability.before_process("git")
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "print('receipt')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output, error = process.communicate(timeout=5)
    capability.after_process(
        permit,
        pid=process.pid,
        exit_code=process.returncode,
        reaped=process.returncode is not None,
        pipe_eof=output == b"receipt\n" and error == b"",
    )


def add_external_fence(store, request_data, source):
    run = store.create_run(request_data)
    if source == "termination":
        with store.locked(run["id"]) as (_, current):
            current["recovery"] = {"remaining": ["controlled-unknown"], "errors": []}
    elif source == "helper":
        store.action(run["id"], "model", {"transport_mode": MODE})
    else:
        raise AssertionError("unknown controlled fence source")
    return run


def test_pg_store_initialize_creates_controller_schema_and_preserves_records(store):
    # The shared Store fixture only calls Store.initialize, not co.initialize.
    with store.connect() as connection:
        assert connection.execute(
            "SELECT to_regclass('controller_operations') AS table_name, "
            "to_regclass('controller_operations_fenced_v1') AS index_name"
        ).fetchone() == {
            "table_name": "controller_operations",
            "index_name": "controller_operations_fenced_v1",
        }
    operation = reserve(store)
    operation.launch()
    operation.mark_unknown()
    before = operation.record
    store.initialize()
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT data FROM controller_operations WHERE id=%s",
                (operation.operation_id,),
            ).fetchone()["data"]
            == before
        )
    assert fences(store) == (False, True, True)


def test_pg_store_initialize_does_not_migrate_historical_actions(store, request_data):
    run = store.create_run(request_data)
    action = store.action(run["id"], "shell", {"command": "controlled-old-intent"})
    with store.connect() as connection:
        connection.execute("UPDATE actions SET status='unknown' WHERE id=%s", (action,))
        before = connection.execute(
            "SELECT status,data FROM actions WHERE id=%s", (action,)
        ).fetchone()
    store.initialize()
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT status,data FROM actions WHERE id=%s", (action,)
            ).fetchone()
            == before
        )
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM controller_operations"
            ).fetchone()["count"]
            == 0
        )


def test_pg_store_initialize_rejects_controller_schema_drift(store):
    with store.connect() as connection:
        connection.execute(
            "ALTER TABLE controller_operations ALTER COLUMN data DROP NOT NULL"
        )
    with pytest.raises(co.RegistryError, match="schema_drift"):
        store.initialize()


@pytest.mark.parametrize("source", ["termination", "helper", "controller"])
def test_pg_public_fence_includes_each_independent_registry(
    store, request_data, source
):
    assert fences(store) == (False, False, False)
    if source == "controller":
        operation = reserve(store)
        operation.launch()
        operation.mark_unknown()
    else:
        add_external_fence(store, request_data, source)
    assert fences(store) == (source != "controller", source == "controller", True)
    with pytest.raises(co.RegistryError, match="fenced"):
        reserve(store)


def test_pg_run_state_and_action_ack_cannot_exclude_controller_fence(
    store, request_data
):
    run = store.create_run(request_data)
    action = store.action(run["id"], "shell", {"command": "controlled-intent"})
    operation = reserve(
        store,
        run_id=run["id"],
        project_id=request_data["project_id"],
        parent_action_id=action,
    )
    operation.launch()
    operation.mark_unknown()
    with store.locked(run["id"]) as (connection, current):
        current.update(state="cancelled", recovery={"remaining": [], "errors": []})
        connection.execute(
            "UPDATE actions SET status='acknowledged' WHERE id=%s", (action,)
        )
    store.initialize()
    assert fences(store) == (False, True, True)


def test_pg_external_callback_allows_exact_live_group_to_continue(store):
    operation = reserve(store)
    capability = operation.launch()
    assert fences(store) == (False, True, True)
    actual_receipt(capability)
    operation.finish(evidence())
    assert fences(store) == (False, False, False)


@pytest.mark.parametrize("source", ["termination", "helper", "controller"])
def test_pg_other_fences_still_block_a_live_group(store, request_data, source):
    operation = reserve(store)
    capability = operation.launch()
    if source == "controller":
        # Corrupt/unknown records fence independently; no second admission is
        # used to smuggle a new business group through the first live group.
        with store.connect() as connection:
            connection.execute(
                "INSERT INTO controller_operations(id,data) VALUES(%s,%s)",
                (str(uuid4()), Jsonb({"state": "unknown"})),
            )
    else:
        add_external_fence(store, request_data, source)
    with pytest.raises(co.RegistryError, match="fenced"):
        capability.before_process("git")
    assert capability._processes == []
    assert fences(store)[2] is True


@pytest.mark.parametrize(
    "actual,bound",
    [("project", None), ("project", "different"), (None, "different")],
)
def test_pg_wrong_project_binding_is_rejected_before_reserve(
    store, request_data, actual, bound
):
    actual_id = request_data["project_id"] if actual else None
    request = {**request_data, "project_id": actual_id}
    run = store.create_run(request)
    bound_id = str(uuid4()) if bound else None
    with pytest.raises(co.RegistryError, match="project_binding"):
        reserve(store, run_id=run["id"], project_id=bound_id)
    assert fences(store) == (False, False, False)
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM controller_operations"
            ).fetchone()["count"]
            == 0
        )


@pytest.mark.parametrize("project_present", [True, False])
def test_pg_matching_project_including_none_is_persisted(
    store, request_data, project_present
):
    request = {
        **request_data,
        "project_id": request_data["project_id"] if project_present else None,
    }
    run = store.create_run(request)
    operation = reserve(store, run_id=run["id"], project_id=request["project_id"])
    operation.not_spawned()
    assert operation.record["run_id"] == run["id"]
    assert operation.record["project_id"] == request["project_id"]
    assert fences(store) == (False, False, False)


@pytest.mark.parametrize("stage", ["launch", "before_process", "finish"])
def test_pg_project_change_rejects_live_transition(store, request_data, stage):
    run = store.create_run(request_data)
    operation = reserve(store, run_id=run["id"], project_id=request_data["project_id"])
    capability = None if stage == "launch" else operation.launch()
    if stage == "finish":
        actual_receipt(capability)
    with store.locked(run["id"]) as (_, current):
        current["request"]["project_id"] = str(uuid4())
    with pytest.raises(co.RegistryError, match="project_binding"):
        if stage == "launch":
            operation.launch()
        elif stage == "before_process":
            capability.before_process("git")
        else:
            operation.finish(evidence())
    assert fences(store) == (False, True, True)


@pytest.mark.parametrize("project_present", [True, False])
def test_pg_source_only_operations_do_not_fabricate_run_binding(store, project_present):
    project_id = str(uuid4()) if project_present else None
    operation = reserve(store, project_id=project_id)
    operation.not_spawned()
    record = operation.record
    assert record["run_id"] is None and record["parent_action_id"] is None
    assert record["expected_run_owner"] is None and record["project_id"] == project_id
    assert fences(store) == (False, False, False)


@pytest.mark.parametrize("field", ["expected_run_owner", "parent_action_id"])
def test_pg_source_only_operations_reject_forged_run_fields(store, field):
    value = "owner-without-run" if field == "expected_run_owner" else str(uuid4())
    with pytest.raises(co.RegistryError, match="run_binding|action_binding"):
        reserve(store, **{field: value})
    assert fences(store) == (False, False, False)
