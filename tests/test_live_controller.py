"""CLI controller lifetime/authority and disabled legacy entry, never live APIs."""

import json
import os
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from doonex import controller_operations as co
from doonex.store import OwnershipLost
from doonex.worker import WORKER_LOCK

from scripts import live_arena, live_background


@pytest.fixture
def authority():
    state = SimpleNamespace(owned=True, failed=False, fence_calls=0, queries=[])

    class Connection:
        def execute(self, statement, parameters):
            if state.failed:
                raise OwnershipLost("synthetic lease lost")
            state.queries.append((statement, parameters))
            assert "FROM pg_locks" in statement
            assert "pid=pg_backend_pid()" in statement
            assert "mode='ExclusiveLock' AND granted" in statement
            assert parameters == (WORKER_LOCK >> 32, WORKER_LOCK & 0xFFFFFFFF)
            return SimpleNamespace(fetchone=lambda: {"owned": state.owned})

    connection = Connection()

    def fence():
        state.fence_calls += 1
        if state.failed:
            raise OwnershipLost("synthetic lease lost")

    worker = SimpleNamespace(store=object(), lock_connection=connection, fence=fence)
    state.worker, state.connection = worker, connection
    return state


def test_cli_actor_is_real_current_scope_and_expires_on_normal_exit(authority):
    state = authority
    with live_arena.cli_registry(state.worker, state.connection) as registry:
        assert type(registry) is co.ControllerRegistry
        assert co.current_registry() is registry
        assert registry.actor.kind == "cli" and registry.actor.pid == os.getpid()
        assert state.fence_calls >= 2 and len(state.queries) >= 2
        assert registry.check_other_fences is live_arena.external_execution_fenced
    assert registry._closed
    with pytest.raises(co.RegistryError, match="registry_missing"):
        co.current_registry()
    with pytest.raises(co.RegistryError, match="actor_closed"):
        registry._live()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_cli_actor_closes_and_restores_outer_scope_on_error(authority, failure):
    state = authority
    outer = co.ControllerRegistry(
        object(),
        actor=co.fresh_actor("api"),
        check_owner=state.worker.fence,
        check_other_fences=live_arena.external_execution_fenced,
    )
    with co.bind_registry(outer):
        with (
            pytest.raises(failure),
            live_arena.cli_registry(state.worker, state.connection) as registry,
        ):
            assert co.current_registry() is registry
            raise failure("synthetic failure")
        assert registry._closed and co.current_registry() is outer


@pytest.mark.parametrize("change", ["identity", "lock", "lease"])
def test_cli_authority_loss_cannot_keep_a_live_context(authority, change):
    state = authority
    with live_arena.cli_registry(state.worker, state.connection):
        if change == "identity":
            state.worker.lock_connection = object()
        elif change == "lock":
            state.owned = False
        else:
            state.failed = True
        with pytest.raises((co.RegistryError, OwnershipLost)):
            co.current_registry()


def test_cli_without_actual_lock_is_refused_before_context_yield(authority):
    state = authority
    state.owned = False
    with (
        pytest.raises(co.RegistryError, match="cli_authority_lost"),
        live_arena.cli_registry(state.worker, state.connection),
    ):
        pytest.fail("CLI authority without the owned lock was yielded")
    with pytest.raises(co.RegistryError, match="registry_missing"):
        co.current_registry()


def test_disabled_background_run_never_loads_configuration_or_performs_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled background CLI performed an external action")

    monkeypatch.setattr(live_background.Settings, "load", forbidden)
    monkeypatch.setattr(live_background, "Worker", forbidden)
    monkeypatch.setattr(live_background, "load_campaign", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for arguments in [None, SimpleNamespace(confirm_model_usage=True), object()]:
        with pytest.raises(
            live_background.Refused, match="legacy_background_harness_disabled"
        ):
            live_background.run(arguments)
    with pytest.raises(
        live_background.Refused, match="legacy_background_harness_disabled"
    ):
        live_background.no_containers()
    with pytest.raises(
        live_background.Refused, match="legacy_background_harness_disabled"
    ):
        live_background._legacy_fixture(SimpleNamespace(confirm_model_usage=True))


def test_disabled_background_main_reports_fixed_reason_and_preserves_ledger(
    tmp_path, monkeypatch, capsys
):
    import sys

    ledger = tmp_path / "live-acceptance.json"
    previous = {
        "reserved_calls": 23,
        "reserved_tokens": 12345,
        "runs": [{"status": "unknown", "unknown_calls": 1}],
    }
    ledger.write_text(json.dumps(previous))
    original = ledger.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled CLI loaded settings or changed its campaign")

    monkeypatch.setattr(live_background.Settings, "load", forbidden)
    monkeypatch.setattr(live_background, "save_campaign", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_background.py",
            "--confirm-model-usage",
            "--authorization-id",
            str(uuid4()),
            "--foreground-run-id",
            str(uuid4()),
            "--model",
            "synthetic-never-called",
        ],
    )
    with pytest.raises(SystemExit) as error:
        live_background.main()
    assert error.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "refused"
    assert summary["reason"] == "legacy_background_harness_disabled"
    assert ledger.read_bytes() == original


@pytest.mark.postgres
def test_cli_registry_checks_real_pg_advisory_lock_and_detects_unlock(
    settings, monkeypatch
):
    from doonex.worker import Worker

    worker = Worker(settings)
    worker.store.initialize()

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("Lost real CLI authority started a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden_spawn)
    with worker.store.connect() as connection:
        connection.autocommit = True
        assert connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
        ).fetchone()["acquired"]
        worker.lock_connection = connection
        with live_arena.cli_registry(worker, connection) as registry:
            assert co.current_registry() is registry
            assert connection.execute(
                "SELECT pg_advisory_unlock(%s) AS released", (WORKER_LOCK,)
            ).fetchone()["released"]
            # A stronger Worker.lease may reject first; either authority layer
            # must refuse before any subprocess or new context can be granted.
            with pytest.raises((co.RegistryError, OwnershipLost)) as error:
                co.current_registry()
            if isinstance(error.value, co.RegistryError):
                assert error.value.reason == "cli_authority_lost"
        assert registry._closed


@pytest.mark.postgres
def test_cli_registry_rejects_lock_held_by_another_real_session(settings, monkeypatch):
    from doonex.worker import Worker

    worker = Worker(settings)

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("A foreign lock session started a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden_spawn)
    with worker.store.connect() as owner, worker.store.connect() as other:
        owner.autocommit = other.autocommit = True
        assert owner.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
        ).fetchone()["acquired"]
        worker.lock_connection = other
        with (
            pytest.raises((co.RegistryError, OwnershipLost)) as error,
            live_arena.cli_registry(worker, other),
        ):
            pytest.fail("A foreign session's lock authorized the CLI")
        if isinstance(error.value, co.RegistryError):
            assert error.value.reason == "cli_authority_lost"
        with pytest.raises(co.RegistryError, match="registry_missing"):
            co.current_registry()
