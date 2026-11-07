"""Definition-checked scheduling migrations against disposable PostgreSQL."""

import psycopg
import pytest
from doonex import store as store_module
from doonex.store import SCHEDULING_INDEXES, termination_unknown
from psycopg import sql
from psycopg.types.json import Jsonb
from test_worker_queue_queries import insert_run

pytestmark = pytest.mark.postgres


def index_rows(connection):
    return connection.execute(
        "SELECT c.relname,i.indisvalid,i.indisready,i.indislive,"
        "pg_get_indexdef(c.oid) AS definition FROM pg_class c "
        "JOIN pg_index i ON i.indexrelid=c.oid "
        "WHERE c.relnamespace=current_schema()::regnamespace "
        "AND c.relname=ANY(%s) ORDER BY c.relname",
        (list(SCHEDULING_INDEXES),),
    ).fetchall()


def test_initialize_migrates_populated_history_and_is_idempotent(store):
    with store.connect() as c:
        for name in SCHEDULING_INDEXES:
            c.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(name)))
        run = insert_run(c, state="completed_verified")
        c.execute(
            "INSERT INTO actions VALUES(gen_random_uuid(),%s,'model','settled','{}')",
            (run["id"],),
        )
    store.initialize()
    with store.connect() as c:
        first = index_rows(c)
    assert len(first) == len(SCHEDULING_INDEXES)
    assert all(
        row[k] for row in first for k in ("indisvalid", "indisready", "indislive")
    )
    store.initialize()
    with store.connect() as c:
        assert index_rows(c) == first
        assert not c.execute(
            "SELECT 1 FROM pg_class WHERE relnamespace=pg_my_temp_schema() "
            "AND relname LIKE 'doonex_index_check_%'"
        ).fetchone()
    assert store.get(run["id"]) == run


@pytest.mark.parametrize(
    "replacement",
    [
        "CREATE INDEX runs_queued_order_v1 ON runs(id)",
        "CREATE INDEX runs_queued_order_v1 ON events(id)",
        "CREATE UNIQUE INDEX runs_queued_order_v1 ON runs(id)",
        "CREATE INDEX runs_queued_order_v1 ON runs USING hash(id)",
        "CREATE INDEX runs_queued_order_v1 ON runs(id DESC) WHERE state='queued'",
        "CREATE INDEX runs_queued_order_v1 ON runs(id) WHERE state='running'",
        "CREATE INDEX runs_queued_order_v1 ON runs(id) INCLUDE(state) WHERE state='queued'",
        "CREATE TABLE runs_queued_order_v1(id uuid)",
    ],
)
def test_same_name_wrong_index_or_relation_is_rejected_without_replacement(
    store, replacement
):
    with store.connect() as c:
        c.execute("DROP INDEX runs_queued_order_v1")
        # A later missing index must stay missing when this migration rolls back.
        c.execute("DROP INDEX allocations_day_v1")
        c.execute(replacement)
        original = c.execute(
            "SELECT oid,relkind FROM pg_class WHERE oid='runs_queued_order_v1'::regclass"
        ).fetchone()
    with pytest.raises(RuntimeError, match="runs_queued_order_v1.*显式迁移"):
        store.initialize()
    with store.connect() as c:
        assert (
            c.execute(
                "SELECT oid,relkind FROM pg_class WHERE oid='runs_queued_order_v1'::regclass"
            ).fetchone()
            == original
        )
        assert (
            c.execute("SELECT to_regclass('allocations_day_v1') AS value").fetchone()[
                "value"
            ]
            is None
        )


def test_canonical_definition_checks_include_predicate_and_order(store):
    name = "allocations_reserved_v1"
    with store.connect() as c:
        c.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(name)))
        c.execute(
            "CREATE INDEX allocations_reserved_v1 ON background_allocations(id) "
            "INCLUDE(authorization_id) WHERE state='reserved'"
        )
    with pytest.raises(RuntimeError, match=name):
        store.initialize()


def test_late_definition_failure_rolls_back_earlier_created_indexes(store):
    with store.connect() as c:
        c.execute("DROP INDEX runs_queued_order_v1")
        c.execute("DROP INDEX authorizations_enabled_order_v1")
        c.execute(
            "CREATE INDEX authorizations_enabled_order_v1 "
            "ON background_authorizations(id) WHERE state='enabled'"
        )
    with pytest.raises(RuntimeError, match="authorizations_enabled_order_v1"):
        store.initialize()
    with store.connect() as c:
        assert (
            c.execute("SELECT to_regclass('runs_queued_order_v1') AS value").fetchone()[
                "value"
            ]
            is None
        )


def test_failed_concurrent_index_is_not_mistaken_for_valid_migration(store):
    with store.connect() as c:
        c.execute("DROP INDEX runs_termination_unknown_v1")
        run = insert_run(c)
        c.execute(
            "UPDATE runs SET data=jsonb_set(data,'{recovery}',%s) WHERE id=%s",
            (Jsonb({"remaining": {"invalid": "not an array"}}), run["id"]),
        )
    table, definition = SCHEDULING_INDEXES["runs_termination_unknown_v1"]
    with psycopg.connect(store.database_url, autocommit=True) as c:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            c.execute(
                sql.SQL("CREATE INDEX CONCURRENTLY {} ON {} " + definition).format(
                    sql.Identifier("runs_termination_unknown_v1"), sql.Identifier(table)
                )
            )
        c.execute("DELETE FROM runs WHERE id=%s", (run["id"],))
        row = c.execute(
            "SELECT indisvalid FROM pg_index "
            "WHERE indexrelid='runs_termination_unknown_v1'::regclass"
        ).fetchone()
        assert row == (False,)
    with pytest.raises(RuntimeError, match="runs_termination_unknown_v1"):
        store.initialize()


def test_index_build_lock_timeout_rolls_back_and_later_startup_can_migrate(
    store, monkeypatch
):
    monkeypatch.setattr(store_module, "LOCK_TIMEOUT_MS", 100)
    monkeypatch.setattr(store_module, "STATEMENT_TIMEOUT_MS", 1000)
    with store.connect() as c:
        run = insert_run(c)
        c.execute("DROP INDEX actions_unresolved_run_v1")
    with store.connect() as blocker:
        blocker.execute(
            "INSERT INTO actions VALUES(gen_random_uuid(),%s,'model','settled','{}')",
            (run["id"],),
        )
        with pytest.raises(psycopg.errors.LockNotAvailable):
            store.initialize()
        with store.connect() as c:
            assert (
                c.execute(
                    "SELECT to_regclass('actions_unresolved_run_v1') AS value"
                ).fetchone()["value"]
                is None
            )
            assert c.execute(
                "SELECT pg_try_advisory_xact_lock(4142061107) AS acquired"
            ).fetchone()["acquired"]
    store.initialize()
    with store.connect() as c:
        assert c.execute(
            "SELECT indisvalid FROM pg_index WHERE "
            "indexrelid='actions_unresolved_run_v1'::regclass"
        ).fetchone()["indisvalid"]


def test_unknown_predicate_keeps_acknowledged_recovery_and_rejects_malformed_data(
    store,
):
    with store.connect() as c:
        run = insert_run(c, state="cancelled")
        assert not termination_unknown(c)
        c.execute(
            "UPDATE runs SET data=jsonb_set(data,'{recovery}',%s) WHERE id=%s",
            (Jsonb({"remaining": [], "errors": ["unconfirmed container"]}), run["id"]),
        )
        assert termination_unknown(c)
    with pytest.raises(psycopg.errors.InvalidParameterValue), store.connect() as c:
        c.execute(
            "UPDATE runs SET data=jsonb_set(data,'{recovery}',%s) WHERE id=%s",
            (Jsonb({"remaining": None}), run["id"]),
        )
