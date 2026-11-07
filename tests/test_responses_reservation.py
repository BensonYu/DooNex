"""Actual PG reserve/crash recovery, fixed identities, no model traffic."""

import copy
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from doonex import budget
from doonex import responses_process as process
from doonex import responses_registry as registry
from doonex.api import create_app
from doonex.budget import Halt, Ledger
from doonex.responses_binding import ResponsesBinding
from doonex.store import now
from doonex.worker import Worker
from fastapi.testclient import TestClient
from test_responses_protocol import sample_arm
from test_responses_registry import AmbiguousCommit
from test_responses_worker import provider

pytestmark = pytest.mark.postgres


def running(settings, store, request_data, *, base_url="http://127.0.0.1:1/v1"):
    data = copy.deepcopy(request_data)
    data["model"].update(
        api_mode="responses",
        responses_transport=registry.MODE,
        base_url=base_url,
        allow_private_http=True,
    )
    run = store.create_run(data)
    owner = str(uuid4())
    with store.locked(run["id"]) as (_, current):
        current.update(state="running", owner=owner, first_started_at=now())
    binding = ResponsesBinding(
        settings,
        store,
        run_id=run["id"],
        owner=owner,
        check_owner=lambda: None,
        check_dispatch=lambda: None,
        model=data["model"],
        api_key="offline-reservation-key",
    )
    return run["id"], owner, binding, Ledger(store, run["id"], owner)


def action(store, run_id):
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT id,status,data FROM actions WHERE run_id=%s AND kind='model'",
            (run_id,),
        ).fetchall()
    assert len(rows) == 1
    return rows[0]


def fenced(store):
    with store.connect() as connection:
        return registry.helper_fenced(connection)


def test_initial_record_and_budget_commit_together_without_lock_io(
    settings, store, request_data, monkeypatch
):
    run_id, _, binding, ledger = running(settings, store, request_data)
    inside = False
    opened = []
    original_locked, original_open = store.locked, process._open_lease

    @contextmanager
    def locked(*args, **kwargs):
        nonlocal inside
        with original_locked(*args, **kwargs) as value:
            inside = True
            try:
                yield value
            finally:
                inside = False

    def opened_outside(*args):
        assert not inside, "Lease I/O inside the dispatch transaction"
        opened.append(1)
        return original_open(*args)

    monkeypatch.setattr(store, "locked", locked)
    monkeypatch.setattr(process, "_open_lease", opened_outside)
    client = str(uuid4())
    intent = binding.prepare_intent(client)
    reservation = ledger.reserve(100, 50, helper_intent=intent)
    assert opened == [1]
    row = action(store, run_id)
    initial = row["data"]["transport_helper"]
    assert initial == reservation["transport_helper"]
    assert initial["state"] == "reserved" and initial["registered"] is True
    assert initial["client_request_id"] == client
    assert initial["lease"] == intent.lease_identity.data()
    assert initial["spawn_attempted"] is False
    assert initial["child"] is initial["reap"] is initial["terminal_summary"] is None
    assert initial["deadline_monotonic_ns"] <= intent.deadline_monotonic_ns
    assert store.get(run_id)["usage"]["calls"] == 1
    assert store.get(run_id)["usage"]["reserved_tokens"] == 150
    with store.connect() as connection:
        registry._record(connection, initial, run_id=run_id, action_id=str(row["id"]))
    assert fenced(store)


@pytest.mark.parametrize("point", ["action", "record", "budget", "event"])
def test_reserve_failure_rolls_back_action_identity_and_all_usage(
    settings, store, request_data, monkeypatch, point
):
    run_id, _, binding, ledger = running(settings, store, request_data)
    intent = binding.prepare_intent(str(uuid4()))
    if point == "action":
        original = store.action

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic action boundary")

        monkeypatch.setattr(store, "action", fail)
    elif point == "record":
        original = registry.reserved_record

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic record boundary")

        monkeypatch.setattr(registry, "reserved_record", fail)
    elif point == "budget":

        def fail(*args):
            raise RuntimeError("synthetic budget boundary")

        monkeypatch.setattr(budget, "_save_batch", fail)
    else:
        original = store.event

        def fail(run, kind, body, connection=None):
            original(run, kind, body, connection)
            if kind == "model_reserved":
                raise RuntimeError("synthetic event boundary")

        monkeypatch.setattr(store, "event", fail)
    before = copy.deepcopy(store.get(run_id)["usage"])
    with pytest.raises(RuntimeError, match="synthetic"):
        ledger.reserve(100, 50, helper_intent=intent)
    assert store.get(run_id)["usage"] == before
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM actions WHERE run_id=%s", (run_id,)
            ).fetchone()["count"]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM events WHERE run_id=%s AND kind='model_reserved'",
                (run_id,),
            ).fetchone()["count"]
            == 0
        )
    assert not fenced(store)


def test_reserve_commit_ack_loss_keeps_complete_initial_identity(
    settings, store, request_data, monkeypatch
):
    run_id, _, binding, ledger = running(settings, store, request_data)
    intent = binding.prepare_intent(str(uuid4()))
    connect = store.connect
    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", lambda: AmbiguousCommit(connect()))
        with pytest.raises(RuntimeError, match="lost commit acknowledgment"):
            ledger.reserve(100, 50, helper_intent=intent)
    row = action(store, run_id)
    current = row["data"]["transport_helper"]
    assert current["state"] == "reserved"
    assert current["attempt_id"] == intent.attempt_id
    assert current["nonce"] == intent.nonce
    assert current["lease"] == intent.lease_identity.data()
    assert store.get(run_id)["usage"]["reserved_tokens"] == 150
    assert registry.inspect_helper(settings, store, str(row["id"]))["valid"]
    assert fenced(store)


@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_actual_parent_sigkill_at_reserve_boundary_and_strict_human_recovery(
    settings, store, request_data, tmp_path, monkeypatch, phase
):
    with provider() as (url, requests):
        run_id, owner, _, _ = running(settings, store, request_data, base_url=url)
        model = store.get(run_id)["request"]["model"]
        payload = tmp_path / "owner-input.json"
        payload.write_text(
            json.dumps(
                {
                    "database_url": store.database_url,
                    "home": str(settings.home),
                    "model": model,
                    "run_id": run_id,
                    "owner": owner,
                    "request_id": str(uuid4()),
                    "phase": phase,
                }
            )
        )
        payload.chmod(0o600)
        receipt, log = tmp_path / "owner-receipt.json", tmp_path / "owner.log"
        with log.open("wb") as output:
            log.chmod(0o600)
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(
                        Path(__file__).parent
                        / "fixtures/responses_helper/reservation_owner.py"
                    ),
                    str(Path(process.__file__).parents[1]),
                    str(payload),
                    str(receipt),
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                until = time.monotonic() + 10
                while time.monotonic() < until and not receipt.exists():
                    assert parent.poll() is None, (
                        "Controlled owner exited before boundary"
                    )
                    time.sleep(0.01)
                assert receipt.exists(), "Controlled owner did not reach boundary"
                parent.kill()
                assert parent.wait(timeout=5) == -signal.SIGKILL
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
        assert requests == []
    if phase == "before_commit":
        with store.connect() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM actions WHERE run_id=%s", (run_id,)
                ).fetchone()["count"]
                == 0
            )
        assert store.get(run_id)["usage"]["calls"] == 0
        assert not fenced(store)
        return
    row = action(store, run_id)
    initial = row["data"]["transport_helper"]
    assert initial["state"] == "reserved" and initial["reap"] is None
    # No task container was created by this pre-register fixture. Exercise the
    # real model-action reconcile while the irrelevant container probe is empty.
    monkeypatch.setattr(
        "doonex.worker.stop_containers", lambda *args: {"remaining": [], "errors": []}
    )
    with pytest.raises(RuntimeError, match="模型传输进程"):
        Worker(settings).run(once=True)
    assert action(store, run_id)["data"]["transport_helper"] == initial
    before = copy.deepcopy(store.get(run_id))
    assert before["usage"]["unknown_calls"] == 1
    assert before["usage"]["reserved_tokens"] == 150
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        detail = client.get(f"/api/transport-helpers/{row['id']}").json()
        assert (
            detail["valid"] and detail["unresolved"] and not detail["lease"]["locked"]
        )
        body = {
            "expected_record_sha256": initial["record_sha256"],
            "attempt_id": initial["attempt_id"],
            "nonce": initial["nonce"],
            "lease_identity": initial["lease"],
            "reason": "operator_confirmed_local_lease",
        }
        path = f"/api/transport-helpers/{row['id']}/resolve"
        assert client.post(path, json={**body, "nonce": "0" * 64}).status_code == 400
        result = client.post(path, json=body)
        assert result.status_code == 200, result.text
        final = result.json()["record"]
        assert final["state"] == "operator_resolved"
        assert final["resolution"]["original_state"] == "reserved"
        assert final["spawn_attempted"] is False and final["reap"] is None
    assert store.get(run_id) == before
    assert not fenced(store)


def test_replaced_inode_after_reserve_never_dispatches_or_accepts_new_identity(
    settings, store, request_data, monkeypatch
):
    run_id, _, binding, ledger = running(settings, store, request_data)
    intent = binding.prepare_intent(str(uuid4()))
    reservation = ledger.reserve(100, 50, helper_intent=intent)
    path = settings.home / "responses-helper" / "lifetime.lease"
    path.rename(path.with_name("original-lease"))
    path.touch(mode=0o600)
    calls = []
    monkeypatch.setattr(os, "posix_spawn", lambda *args, **kwargs: calls.append(1))
    call = sample_arm()["call"]
    call.update(
        model=binding.model["model_id"].removeprefix("openai/"),
        api_base=binding.model["base_url"],
        max_output_tokens=binding.model["max_output_tokens"],
        api_key="offline-reservation-key",
        extra_headers={"X-Client-Request-Id": intent.client_request_id},
    )
    with pytest.raises(process.HelperProcessError, match="lease_changed"):
        binding(reservation, call, intent.client_request_id)
    assert not calls
    ledger.settle(reservation, None)
    current = action(store, run_id)["data"]["transport_helper"]
    assert current["lease"] == intent.lease_identity.data()
    assert current["state"] == "not_spawned" and current["reap"] is None
    assert store.get(run_id)["usage"]["reserved_tokens"] == 150
    with pytest.raises(process.HelperProcessError, match="lease_changed"):
        binding.prepare_intent(str(uuid4()))


def test_isolated_requires_intent_and_direct_refuses_one(settings, store, request_data):
    run_id, _, binding, ledger = running(settings, store, request_data)
    with pytest.raises(ValueError, match="helper intent"):
        ledger.reserve(100, 50)
    assert store.get(run_id)["usage"]["calls"] == 0
    intent = binding.prepare_intent(str(uuid4()))
    other = store.create_run(request_data)
    with store.locked(other["id"]) as (_, current):
        current.update(state="running", owner="direct-owner")
    with pytest.raises(ValueError, match="cannot bind"):
        Ledger(store, other["id"], "direct-owner").reserve(
            100, 50, helper_intent=intent
        )
    assert not fenced(store)


@pytest.mark.parametrize(
    "change",
    [
        {"attempt_id": "bad"},
        {"client_request_id": "bad"},
        {"nonce": "bad"},
        {"parent_pid": 0},
        {"parent_boot_id": ""},
        {"observed_at_utc": "bad"},
        {"deadline_monotonic_ns": 0},
        {"deadline_monotonic_ns": True},
        {"deadline_monotonic_ns": float("inf")},
        {"deadline_monotonic_ns": None},
        {"record_sha256": "0" * 64},
    ],
)
def test_malformed_initial_intent_rolls_back_everything(
    settings, store, request_data, change
):
    run_id, _, binding, ledger = running(settings, store, request_data)
    intent = replace(binding.prepare_intent(str(uuid4())), **change)
    before = copy.deepcopy(store.get(run_id)["usage"])
    with pytest.raises((ValueError, registry.RegistryError)):
        ledger.reserve(100, 50, helper_intent=intent)
    assert store.get(run_id)["usage"] == before
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM actions WHERE run_id=%s", (run_id,)
            ).fetchone()["count"]
            == 0
        )
    assert not fenced(store)


def test_legacy_missing_record_still_blocks_and_cannot_be_registered(
    settings, store, request_data
):
    run_id, owner, binding, ledger = running(settings, store, request_data)
    intent = binding.prepare_intent(str(uuid4()))
    with store.connect() as connection:
        action_id = store.action(
            run_id, "model", {"transport_mode": registry.MODE}, connection
        )
    bound = registry.ResponsesRegistry(
        settings,
        store,
        run_id=run_id,
        owner=owner,
        action_id=action_id,
        check_owner=lambda: None,
    )
    with pytest.raises(registry.RegistryError):
        bound.register(
            {
                **intent.base(),
                "parent": intent.parent(),
                "lease": intent.lease_identity.data(),
                "deadline_monotonic_ns": intent.deadline_monotonic_ns,
                "observed_at_utc": intent.observed_at_utc,
                "previous_record_sha256": "0" * 64,
            }
        )
    with pytest.raises(Halt, match="传输进程"):
        ledger.reserve(100, 50, helper_intent=intent)
    detail = registry.inspect_helper(settings, store, action_id)
    assert not detail["valid"] and detail["problems"] == ["record_missing"]
    assert store.get(run_id)["usage"]["calls"] == 0
