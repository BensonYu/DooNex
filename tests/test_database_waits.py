"""Real PostgreSQL wait limits, rollback, ownership and safe API failures."""

import configparser
import socket
import threading
import time

import doonex.store as store_module
import psycopg
import pytest
from doonex.api import create_app
from doonex.store import DISPATCH_LOCK, Store
from doonex.worker import WORKER_LOCK
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def short_waits(monkeypatch):
    monkeypatch.setattr(store_module, "STATEMENT_TIMEOUT_MS", 300)
    monkeypatch.setattr(store_module, "LOCK_TIMEOUT_MS", 100)


def test_store_preserves_schema_and_enforces_default_wait_limits(store):
    options = conninfo_to_dict(store.database_url)["options"]
    configured = Store(
        make_conninfo(
            store.database_url,
            options=options + " -c statement_timeout=0 -c lock_timeout=0",
            connect_timeout=0,
        )
    )
    with configured.connect() as c:
        assert c.info.get_parameters()["connect_timeout"] == "5"
        assert (
            c.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "15s"
        )
        assert c.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "5s"
        assert c.execute("SELECT count(*) n FROM runs").fetchone()["n"] == 0


def test_store_preserves_readonly_option(store):
    options = conninfo_to_dict(store.database_url)["options"]
    readonly = Store(
        make_conninfo(
            store.database_url,
            options=options + " -c default_transaction_read_only=on",
        )
    )
    with readonly.connect() as c:
        assert (
            c.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
            == "on"
        )
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("DELETE FROM runs")


def test_store_preserves_environment_options_when_dsn_has_none(store, monkeypatch):
    parameters = conninfo_to_dict(store.database_url)
    options = parameters.pop("options")
    monkeypatch.setenv("PGOPTIONS", options + " -c default_transaction_read_only=on")
    with Store(make_conninfo(**parameters)).connect() as c:
        assert (
            c.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
            == "on"
        )
        assert c.execute("SELECT count(*) n FROM runs").fetchone()["n"] == 0
        assert c.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "5s"


@pytest.mark.parametrize("explicit_service", [True, False])
def test_store_preserves_service_options_over_environment(
    store, monkeypatch, tmp_path, explicit_service
):
    parameters = conninfo_to_dict(store.database_url)
    parameters["options"] += " -c default_transaction_read_only=on"
    parser = configparser.ConfigParser(interpolation=None)
    parser["doonex_offline"] = parameters
    service_file = tmp_path / "pg_service.conf"
    with service_file.open("w") as output:
        parser.write(output, space_around_delimiters=False)
    service_file.chmod(0o600)
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv(
        "PGOPTIONS", "-c search_path=pg_catalog -c default_transaction_read_only=off"
    )
    monkeypatch.setenv("PGSERVICE", "doonex_offline")
    service = Store("service=doonex_offline" if explicit_service else "")
    with service.connect() as c:
        assert (
            c.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
            == "on"
        )
        assert c.execute("SELECT count(*) n FROM runs").fetchone()["n"] == 0
        assert (
            c.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "15s"
        )
        assert c.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "5s"


def test_failed_timeout_setup_closes_connection(store, monkeypatch):
    original_connect = psycopg.connect
    opened = []

    def capture(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(psycopg, "connect", capture)
    monkeypatch.setattr(store_module, "STATEMENT_TIMEOUT_MS", "invalid")
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        store.connect()
    assert len(opened) == 1 and opened[0].closed


def test_stalled_local_handshake_has_finite_connect_timeout():
    # Accept TCP but never speak PostgreSQL. No actual database or credential.
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(10)
        accepted = threading.Event()
        release = threading.Event()

        def stall():
            with server.accept()[0]:
                accepted.set()
                release.wait(10)

        thread = threading.Thread(target=stall, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with pytest.raises(psycopg.OperationalError, match="timeout"):
                Store(
                    make_conninfo(
                        host="127.0.0.1",
                        port=server.getsockname()[1],
                        dbname="offline",
                        user="offline",
                        sslmode="disable",
                        connect_timeout=0,
                    )
                ).connect()
            assert accepted.is_set()
            assert 3 <= time.monotonic() - started < 9
        finally:
            release.set()
            thread.join(2)
        assert not thread.is_alive()


def test_long_statement_is_cancelled_and_transaction_rolls_back(store, monkeypatch):
    short_waits(monkeypatch)
    with pytest.raises(psycopg.errors.QueryCanceled), store.connect() as c:
        c.execute("CREATE TABLE rollback_probe (value integer)")
        c.execute("SELECT pg_sleep(3)")
    with store.connect() as c:
        assert (
            c.execute("SELECT to_regclass('rollback_probe') name").fetchone()["name"]
            is None
        )


def test_row_lock_timeout_does_not_change_run_or_budget(
    store, request_data, monkeypatch
):
    short_waits(monkeypatch)
    run = store.create_run(request_data)
    with store.connect() as holder:
        holder.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (run["id"],))
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable), store.locked(run["id"]):
            pytest.fail("Contended row must never be acquired")
        assert time.monotonic() - started < 2
    assert store.get(run["id"]) == run


def test_dispatch_lock_timeout_cannot_create_partial_run(
    store, request_data, monkeypatch
):
    short_waits(monkeypatch)
    with store.connect() as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (DISPATCH_LOCK,))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            store.create_run(request_data)
    assert store.list_runs() == []
    with store.connect() as c:
        assert c.execute("SELECT count(*) n FROM events").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) n FROM actions").fetchone()["n"] == 0
    # A later explicit operation is permitted; the failed call was not replayed.
    assert store.create_run(request_data)["state"] == "queued"


def test_query_timeout_keeps_worker_session_lock(store, monkeypatch):
    short_waits(monkeypatch)
    with store.connect() as owner, store.connect() as contender:
        owner.autocommit = True
        assert owner.execute(
            "SELECT pg_try_advisory_lock(%s) acquired", (WORKER_LOCK,)
        ).fetchone()["acquired"]
        with pytest.raises(psycopg.errors.QueryCanceled):
            owner.execute("SELECT pg_sleep(3)")
        assert not contender.execute(
            "SELECT pg_try_advisory_lock(%s) acquired", (WORKER_LOCK,)
        ).fetchone()["acquired"]
    with store.connect() as next_owner:
        assert next_owner.execute(
            "SELECT pg_try_advisory_lock(%s) acquired", (WORKER_LOCK,)
        ).fetchone()["acquired"]


@pytest.mark.parametrize(
    "failure",
    [
        psycopg.errors.QueryCanceled,
        psycopg.errors.LockNotAvailable,
        psycopg.OperationalError,
    ],
)
def test_api_database_failure_is_safe_and_does_not_retry(
    settings, monkeypatch, failure
):
    calls = []
    with TestClient(create_app(settings)) as client:

        def fail(self):
            calls.append(1)
            raise failure("private SQL and " + settings.database_url)

        monkeypatch.setattr(Store, "list_projects", fail)
        response = client.get(
            "/api/projects", headers={"Authorization": "Bearer " + settings.token}
        )
        assert response.status_code == 503
        assert response.json() == {
            "detail": "数据库暂不可用或等待超时；请先刷新并核对操作结果"
        }
        assert response.headers["Cache-Control"] == "no-store"
        assert calls == [1]
        assert settings.database_url not in response.text
