"""Real PostgreSQL contracts; helper network and model calls are not used."""

import copy
import fcntl
import json
import os
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from doonex.budget import Ledger
from doonex.responses_process import HelperAttempt, LeaseIdentity, LeaseInspection
from doonex.responses_registry import (
    HELPER_FENCE_PREDICATE,
    HELPER_REGISTRY_SCHEMA,
    MODE,
    WORKER_LOCK,
    RegistryError,
    ResponsesRegistry,
    _digest,
    helper_fenced,
    historical_lease_identity,
    inspect_helper,
    lease_spec,
    list_unresolved,
    reserved_record,
    resolve_helper,
)
from doonex.store import OwnershipLost
from psycopg.types.json import Jsonb


@pytest.fixture(autouse=True)
def registry_schema(store):
    with store.connect() as connection:
        connection.execute(HELPER_REGISTRY_SCHEMA)


@pytest.fixture
def factory(settings, store, request_data):
    def create(*, marker=True, initial=True, lease=None):
        data = copy.deepcopy(request_data)
        if marker and initial:
            data["model"].update(api_mode="responses", responses_transport=MODE)
        run = store.create_run(data)
        owner = str(uuid4())
        with store.locked(run["id"]) as (_, current):
            current.update(state="running", owner=owner)
        ledger = Ledger(store, run["id"], owner)
        observed = time.monotonic_ns()
        intent = HelperAttempt(
            str(uuid4()),
            str(uuid4()),
            uuid4().hex + uuid4().hex,
            os.getpid(),
            observed,
            "offline-contract-fixture",
            datetime.now(UTC).isoformat(),
            lease or LeaseIdentity(1, 2),
            observed + 60_000_000_000,
        )
        reservation = ledger.reserve(
            100, 50, **({"helper_intent": intent} if marker and initial else {})
        )
        action_id = reservation["id"]
        if marker and not initial:
            # Deliberate historical missing-record fixture, not a new writer.
            with store.connect() as connection:
                connection.execute(
                    "UPDATE actions SET data=data || %s WHERE id=%s",
                    (Jsonb({"transport_mode": MODE}), action_id),
                )
        registry = ResponsesRegistry(
            settings,
            store,
            run_id=run["id"],
            owner=owner,
            action_id=action_id,
            check_owner=lambda: None,
        )
        base = intent.base()
        current = reservation.get("transport_helper")
        reserved = ResponsesRegistry._ack(current) if current else None
        registration = {
            **base,
            "previous_record_sha256": current["record_sha256"] if current else "0" * 64,
            "parent": intent.parent(),
            "deadline_monotonic_ns": current["deadline_monotonic_ns"]
            if current
            else intent.deadline_monotonic_ns,
            "observed_at_utc": intent.observed_at_utc,
            "lease": intent.lease_identity.data(),
        }
        return {
            "registry": registry,
            "run_id": run["id"],
            "owner": owner,
            "action_id": action_id,
            "base": base,
            "registration": registration,
            "ledger": ledger,
            "reservation": reservation,
            "intent": intent,
            "reserved": reserved,
        }

    return create


def record(store, item):
    with store.connect() as connection:
        return connection.execute(
            "SELECT data->'transport_helper' AS record FROM actions WHERE id=%s",
            (item["action_id"],),
        ).fetchone()["record"]


def fenced(store):
    with store.connect() as connection:
        return helper_fenced(connection)


def actual_reap():
    pid = os.posix_spawn(
        sys.executable,
        [sys.executable, "-I", "-S", "-c", "pass"],
        {"PATH": "/usr/bin:/bin"},
    )
    actual_pid, status = os.waitpid(pid, 0)
    assert actual_pid == pid
    return {
        "pid": pid,
        "wait_status": status,
        "exit_code": os.waitstatus_to_exitcode(status),
        "observed_monotonic_ns": time.monotonic_ns(),
    }


def arm(item, prepared, reap):
    return item["registry"].arm(
        {
            **item["base"],
            "previous_record_sha256": prepared["record_sha256"],
            "child": {"pid": reap["pid"], "pgid": reap["pid"]},
            "lease": item["registration"]["lease"],
            "deadline_monotonic_ns": item["registration"]["deadline_monotonic_ns"],
        }
    )


def completion(item, acknowledgment, reap):
    return {
        **item["base"],
        "previous_record_sha256": acknowledgment["record_sha256"],
        "reap": reap,
        "terminal_summary": {
            "kind": "result",
            "outcome": "success",
            "reason": "success",
        },
    }


def blocked(item, acknowledgment=None, *, spawned=True, reaped=False):
    return {
        **item["base"],
        "previous_record_sha256": (acknowledgment or item.get("reserved") or {}).get(
            "record_sha256"
        ),
        "spawn_attempted": spawned,
        "child_pid": 123 if spawned else None,
        "reaped": reaped,
        "errors": [],
        "reason": "internal",
    }


def free_lease(spec):
    return LeaseInspection(spec.expected_identity or LeaseIdentity(1, 2), False)


def resolve(settings, store, item, current, **kwargs):
    return resolve_helper(
        settings,
        store,
        item["action_id"],
        expected_record_sha256=current["record_sha256"],
        attempt_id=item["base"]["attempt_id"],
        nonce=item["base"]["nonce"],
        lease_identity=item["registration"]["lease"],
        reason="Confirmed local helper exit from bounded inspection",
        lease_inspector=kwargs.pop("lease_inspector", free_lease),
        **kwargs,
    )


class AmbiguousCommit:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.__enter__()
        return self.connection

    def __exit__(self, kind, value, traceback):
        result = self.connection.__exit__(kind, value, traceback)
        if kind is None:
            raise RuntimeError("Synthetic lost commit acknowledgment")
        return result


def lose_commit_ack(store, monkeypatch, operation):
    original = store.connect
    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", lambda: AmbiguousCommit(original()))
        with pytest.raises(RuntimeError, match="lost commit acknowledgment"):
            operation()


def test_register_arm_reap_commit_and_no_budget_mutation(factory, store):
    item = factory()
    before = store.get(item["run_id"])["usage"]
    assert fenced(store)
    prepared = item["registry"].register(item["registration"])
    assert prepared["state"] == "prepared" and fenced(store)
    assert item["registry"].register(item["registration"]) == prepared
    reap = actual_reap()
    armed = arm(item, prepared, reap)
    assert armed["state"] == "armed" and fenced(store)
    done = item["registry"].complete(completion(item, armed, reap))
    assert done["state"] == "reaped" and not fenced(store)
    assert item["registry"].complete(completion(item, armed, reap)) == done
    assert item["registry"].block(blocked(item, armed, reaped=True)) == done
    assert store.get(item["run_id"])["usage"] == before
    with store.connect() as connection:
        assert historical_lease_identity(connection) == LeaseIdentity(1, 2)


def test_prepared_receipt_lost_can_record_known_not_spawned(
    factory, store, monkeypatch
):
    item = factory()
    lose_commit_ack(
        store, monkeypatch, lambda: item["registry"].register(item["registration"])
    )
    assert record(store, item)["state"] == "prepared" and fenced(store)
    payload = blocked(item, spawned=False)
    done = item["registry"].block(payload)
    assert done["state"] == "not_spawned" and not fenced(store)
    # After cleanup, that initial hash is two transitions old. It must not
    # become a general ancestor-CAS exception for a terminal record.
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].block(payload)
    assert (
        item["registry"].block(
            {**payload, "previous_record_sha256": done["record_sha256"]}
        )
        == done
    )


def test_register_only_shrinks_deadline_and_keeps_final_deadline_immutable(
    factory, store
):
    item = factory()
    initial = record(store, item)
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].register(
            {
                **item["registration"],
                "deadline_monotonic_ns": initial["deadline_monotonic_ns"] + 1,
            }
        )
    registration = {
        **item["registration"],
        "deadline_monotonic_ns": initial["deadline_monotonic_ns"] - 1_000_000_000,
    }
    prepared = item["registry"].register(registration)
    current = record(store, item)
    assert current["deadline_monotonic_ns"] == registration["deadline_monotonic_ns"]
    assert current["transition_from_sha256"] == initial["record_sha256"]
    assert item["registry"].register(registration) == prepared
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].register(item["registration"])
    reap = actual_reap()
    payload = {
        **item["base"],
        "previous_record_sha256": prepared["record_sha256"],
        "child": {"pid": reap["pid"], "pgid": reap["pid"]},
        "lease": registration["lease"],
        "deadline_monotonic_ns": initial["deadline_monotonic_ns"],
    }
    with pytest.raises(RegistryError, match="arm_binding"):
        item["registry"].arm(payload)
    item["registry"].arm(
        {**payload, "deadline_monotonic_ns": registration["deadline_monotonic_ns"]}
    )
    assert (
        record(store, item)["deadline_monotonic_ns"]
        == registration["deadline_monotonic_ns"]
    )


def test_reserved_cannot_claim_spawn_or_reap_and_old_ancestor_cannot_skip_arm(
    factory, store
):
    item = factory()
    initial = copy.deepcopy(record(store, item))
    reap = actual_reap()
    with pytest.raises(RegistryError, match="cas"):
        arm(item, item["reserved"], reap)
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].complete(completion(item, item["reserved"], reap))
    with pytest.raises(RegistryError, match="not_spawned_binding"):
        item["registry"].block(blocked(item))
    assert record(store, item) == initial
    prepared = item["registry"].register(item["registration"])
    arm(item, prepared, reap)
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].block(blocked(item, item["reserved"], spawned=False))
    assert record(store, item)["state"] == "armed" and fenced(store)


def test_arm_commit_unknown_cannot_use_old_hash_to_claim_reap(
    factory, store, monkeypatch
):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    reap = actual_reap()
    lose_commit_ack(store, monkeypatch, lambda: arm(item, prepared, reap))
    assert record(store, item)["state"] == "armed"
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].complete(completion(item, prepared, reap))
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].block(blocked(item, prepared, reaped=True))
    assert fenced(store)


def test_complete_commit_unknown_is_idempotent_and_never_downgraded(
    factory, store, monkeypatch
):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    reap = actual_reap()
    armed = arm(item, prepared, reap)
    payload = completion(item, armed, reap)
    lose_commit_ack(store, monkeypatch, lambda: item["registry"].complete(payload))
    current = record(store, item)
    assert current["state"] == "reaped" and not fenced(store)
    done = item["registry"].complete(payload)
    assert done["record_sha256"] == current["record_sha256"]
    assert item["registry"].block(blocked(item, armed, reaped=True)) == done


def legacy_no_spawn(store, item):
    """Retain an explicit old valid row; no production API backfills it."""
    with store.connect() as connection:
        value = reserved_record(
            connection,
            run_id=item["run_id"],
            action_id=item["action_id"],
            owner=item["owner"],
            attempt=item["intent"],
        )
        failure = {
            key: blocked(item, spawned=False)[key]
            for key in ("reason", "spawn_attempted", "child_pid", "reaped", "errors")
        }
        value.update(
            state="not_spawned",
            registered=False,
            parent=None,
            lease=None,
            deadline_monotonic_ns=None,
            failure=failure,
        )
        value["record_sha256"] = _digest(connection, value)
        connection.execute(
            "UPDATE actions SET data=jsonb_set(data,'{transport_helper}',%s) WHERE id=%s",
            (Jsonb(value), item["action_id"]),
        )
    return value


def test_unregistered_known_not_spawned_has_no_fabricated_identity(factory, store):
    item = factory(initial=False)
    current = legacy_no_spawn(store, item)
    assert not fenced(store)
    assert not current["registered"]
    assert (
        current["lease"]
        is current["parent"]
        is current["deadline_monotonic_ns"]
        is None
    )
    assert (
        item["registry"].block(blocked(item, spawned=False))["state"] == "not_spawned"
    )
    with store.connect() as connection:
        assert historical_lease_identity(connection) is None


def test_safe_no_spawn_history_does_not_prevent_first_lease(factory, store, settings):
    item = factory(initial=False)
    legacy_no_spawn(store, item)
    spec = lease_spec(settings, store, check_owner=lambda: None)
    assert spec.expected_identity.inode == spec.path.stat().st_ino
    assert not spec.allow_create


@pytest.mark.parametrize(
    "change",
    [
        {"spawn_attempted": True},
        {"child_pid": 123},
        {"reaped": True},
        {"previous_record_sha256": "0" * 64},
    ],
)
def test_missing_record_never_implies_not_spawned(factory, store, change):
    item = factory(initial=False)
    with pytest.raises(RegistryError):
        item["registry"].block({**blocked(item, spawned=False), **change})
    assert record(store, item) is None and fenced(store)


def test_unmarked_action_cannot_become_unregistered_receipt(factory, store):
    item = factory(marker=False)
    with pytest.raises(RegistryError, match="record_missing"):
        item["registry"].block(blocked(item, spawned=False))


@pytest.mark.parametrize("hook", ["register", "arm", "complete", "block"])
def test_owner_transfer_rejects_old_bound_registry(factory, store, hook):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    reap = actual_reap()
    with store.locked(item["run_id"]) as (_, current):
        current["owner"] = str(uuid4())
    before = record(store, item)
    payloads = {
        "register": item["registration"],
        "arm": {
            **item["base"],
            "previous_record_sha256": prepared["record_sha256"],
            "child": {"pid": reap["pid"], "pgid": reap["pid"]},
            "lease": item["registration"]["lease"],
            "deadline_monotonic_ns": item["registration"]["deadline_monotonic_ns"],
        },
        "complete": completion(item, prepared, reap),
        "block": blocked(item, prepared),
    }
    with pytest.raises(OwnershipLost):
        getattr(item["registry"], hook)(payloads[hook])
    assert record(store, item) == before and fenced(store)


def test_stop_prevents_arm_but_allows_cleanup_completion(factory, store):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    reap = actual_reap()
    with store.locked(item["run_id"]) as (_, current):
        current.update(state="pausing", requested_action="pause")
    with pytest.raises(RegistryError, match="run_stopped"):
        arm(item, prepared, reap)
    assert (
        item["registry"].complete(completion(item, prepared, reap))["state"] == "reaped"
    )


@pytest.mark.parametrize("bad", [None, [], "broken", {}, {"state": "reaped"}])
def test_malformed_record_fences_without_json_operation_error(
    factory, store, settings, bad
):
    item = factory()
    with store.connect() as connection:
        connection.execute(
            "UPDATE actions SET data=data || %s WHERE id=%s",
            (Jsonb({"transport_helper": bad}), item["action_id"]),
        )
    assert fenced(store)
    inspection = inspect_helper(
        settings, store, item["action_id"], lease_inspector=free_lease
    )
    assert not inspection["valid"] and inspection["unresolved"]


@pytest.mark.parametrize(
    "change",
    [
        {"record_sha256": "0" * 64},
        {"unexpected": "field"},
        {"state": "future_terminal"},
        {"schema": 2},
    ],
)
def test_bad_hash_unknown_fields_and_states_in_terminal_fence(factory, store, change):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    reap = actual_reap()
    item["registry"].complete(completion(item, prepared, reap))
    current = {**record(store, item), **change}
    with store.connect() as connection:
        if "record_sha256" not in change:
            current["record_sha256"] = _digest(connection, current)
        connection.execute(
            "UPDATE actions SET data=jsonb_set(data,'{transport_helper}',%s) WHERE id=%s",
            (Jsonb(current), item["action_id"]),
        )
    assert fenced(store)


def test_unknown_ack_and_terminal_run_keep_helper_fence_and_list(factory, store):
    item = factory()
    item["registry"].register(item["registration"])
    item["ledger"].settle(item["reservation"], None)
    with store.connect() as connection:
        connection.execute(
            "UPDATE actions SET status='acknowledged' WHERE id=%s", (item["action_id"],)
        )
    with store.locked(item["run_id"]) as (_, current):
        current.update(state="cancelled", recovery={"remaining": [], "errors": []})
    assert fenced(store)
    with store.connect() as connection:
        assert list_unresolved(connection)["items"] == [
            {"action_id": item["action_id"], "run_id": item["run_id"]}
        ]


def test_unresolved_list_paginates_old_records(factory, store):
    first = factory()
    items = [first]
    # Explicit synthetic old history, not four new requests bypassing admission.
    with store.connect() as connection:
        for _ in range(3):
            action_id = str(uuid4())
            connection.execute(
                "INSERT INTO actions VALUES(%s,%s,'model','acknowledged',%s)",
                (action_id, first["run_id"], Jsonb({"transport_mode": MODE})),
            )
            items.append({"action_id": action_id, "run_id": first["run_id"]})
    found = []
    cursor = None
    with store.connect() as connection:
        while True:
            page = list_unresolved(connection, limit=2, after_id=cursor)
            found.extend(item["action_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert found == sorted(item["action_id"] for item in items)


def test_operator_resolution_preserves_unknown_reservation_and_original_truth(
    factory, store, settings
):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    item["registry"].block(blocked(item, prepared, reaped=True))
    item["ledger"].settle(item["reservation"], None)
    before_usage = copy.deepcopy(store.get(item["run_id"])["usage"])
    with store.connect() as connection:
        before_action = connection.execute(
            "SELECT status,data FROM actions WHERE id=%s", (item["action_id"],)
        ).fetchone()
    current = record(store, item)
    assert current["state"] == "blocked" and current["reap"] is None
    result = resolve(settings, store, item, current)
    assert result["valid"] and result["record"]["state"] == "operator_resolved"
    assert result["record"]["reap"] is None and not fenced(store)
    assert result["record"]["resolution"]["original_state"] == "blocked"
    assert store.get(item["run_id"])["usage"] == before_usage
    with store.connect() as connection:
        after_action = connection.execute(
            "SELECT status,data FROM actions WHERE id=%s", (item["action_id"],)
        ).fetchone()
    assert after_action["status"] == before_action["status"] == "unknown"
    assert {
        k: v for k, v in after_action["data"].items() if k != "transport_helper"
    } == {k: v for k, v in before_action["data"].items() if k != "transport_helper"}


@pytest.mark.parametrize("mode", ["held", "missing", "wrong_inode"])
def test_operator_rejects_held_missing_and_wrong_inode(factory, store, settings, mode):
    item = factory()
    item["registry"].register(item["registration"])
    current = record(store, item)

    def inspector(spec):
        if mode == "missing":
            raise FileNotFoundError("synthetic missing lease")
        return LeaseInspection(
            LeaseIdentity(1, 99) if mode == "wrong_inode" else spec.expected_identity,
            mode == "held",
        )

    with pytest.raises((RegistryError, FileNotFoundError)):
        resolve(settings, store, item, current, lease_inspector=inspector)
    assert record(store, item) == current and fenced(store)


def test_operator_rejects_active_worker_and_stale_hash(factory, store, settings):
    item = factory()
    item["registry"].register(item["registration"])
    current = record(store, item)
    with store.connect() as owner:
        owner.execute("SELECT pg_advisory_lock(%s)", (WORKER_LOCK,))
        with pytest.raises(RegistryError, match="worker_active"):
            resolve(settings, store, item, current)
    with pytest.raises(RegistryError, match="resolution_binding"):
        resolve(settings, store, item, {**current, "record_sha256": "0" * 64})
    assert fenced(store)


def test_history_lease_binding_and_first_install_adoption(factory, store, settings):
    first = lease_spec(settings, store, check_owner=lambda: None)
    assert first.expected_identity.inode == first.path.stat().st_ino
    assert not first.allow_create
    adopted = lease_spec(settings, store, check_owner=lambda: None)
    assert adopted == first
    item = factory(lease=first.expected_identity)
    prepared = item["registry"].register(item["registration"])
    item["registry"].complete(completion(item, prepared, actual_reap()))
    expected = lease_spec(settings, store, check_owner=lambda: None)
    assert expected == first

    def wrong(spec):
        return LeaseInspection(
            LeaseIdentity(
                first.expected_identity.device, first.expected_identity.inode + 1
            ),
            False,
        )

    with pytest.raises(RegistryError, match="lease_changed"):
        lease_spec(settings, store, check_owner=lambda: None, lease_inspector=wrong)


def test_v2_index_install_preserves_old_definition_and_resolves_reserved(
    factory, store, settings
):
    old_predicate = HELPER_FENCE_PREDICATE.replace(
        "IN ('reserved','prepared','armed','blocked')",
        "IN ('prepared','armed','blocked')",
    )
    assert old_predicate != HELPER_FENCE_PREDICATE
    with store.connect() as connection:
        connection.execute("DROP INDEX actions_helper_fenced_v2")
        connection.execute(
            f"CREATE INDEX actions_helper_fenced ON actions(id) WHERE {old_predicate}"
        )
        old_definition = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname='actions_helper_fenced'"
        ).fetchone()["indexdef"]
        connection.execute(HELPER_REGISTRY_SCHEMA)
        definitions = {
            row["indexname"]: row["indexdef"]
            for row in connection.execute(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname IN ('actions_helper_fenced','actions_helper_fenced_v2')"
            ).fetchall()
        }
    assert definitions["actions_helper_fenced"] == old_definition
    assert "'reserved'" in definitions["actions_helper_fenced_v2"]
    item = factory()
    initial = record(store, item)
    assert initial["state"] == "reserved" and fenced(store)
    result = resolve(settings, store, item, initial)
    assert result["record"]["resolution"]["original_state"] == "reserved"
    assert not fenced(store)
    next_item = factory()
    assert record(store, next_item)["state"] == "reserved"
    assert next_item["base"]["attempt_id"] != item["base"]["attempt_id"]
    assert fenced(store)


def test_matching_partial_index_avoids_terminal_history_scan(factory, store, settings):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    item["registry"].complete(completion(item, prepared, actual_reap()))
    current = record(store, item)
    resolved_item = factory()
    resolved = resolve(settings, store, resolved_item, record(store, resolved_item))[
        "record"
    ]
    count = int(os.environ.get("DOONEX_REGISTRY_SCALE", "10000"))
    with store.connect() as connection:
        for start in range(1, count + 1, 10000):
            connection.execute(
                """WITH ids AS (
            SELECT md5('registry-scale-' || n::text)::uuid AS id FROM generate_series(%s::integer,%s::integer) n
        ), records AS (
            SELECT id,(CASE WHEN substr(id::text,1,1)<'8' THEN %s::jsonb ELSE %s::jsonb END) || jsonb_build_object('action_id',id::text,'run_id',%s::text) AS record FROM ids
        ) INSERT INTO actions(id,run_id,kind,status,data)
        SELECT id,%s,'model','completed',jsonb_build_object('transport_mode','isolated_direct_v1','transport_helper',
          record || jsonb_build_object('record_sha256',encode(sha256(jsonb_send(record-'record_sha256')),'hex')))
        FROM records""",
                (
                    start,
                    min(start + 9999, count),
                    Jsonb(current),
                    Jsonb(resolved),
                    item["run_id"],
                    item["run_id"],
                ),
            )
        connection.execute("ANALYZE actions")
        plan = connection.execute(
            f"EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON,TIMING OFF) SELECT id FROM actions WHERE {HELPER_FENCE_PREDICATE} ORDER BY id LIMIT 1"
        ).fetchone()["QUERY PLAN"][0]
        nodes = []

        def walk(node):
            nodes.append(node)
            for child in node.get("Plans", []):
                walk(child)

        walk(plan["Plan"])
        print(json.dumps({"terminal_history_rows": count + 1, "fence_plan": plan}))
        assert any(
            node.get("Index Name") == "actions_helper_fenced_v2" for node in nodes
        )
        assert not any(node["Node Type"] == "Seq Scan" for node in nodes)
        assert not helper_fenced(connection)


def test_duplicate_transition_still_requires_original_cas_hash(factory, store):
    item = factory()
    prepared = item["registry"].register(item["registration"])
    reap = actual_reap()
    armed = arm(item, prepared, reap)
    bad_arm = {
        **item["base"],
        "previous_record_sha256": "0" * 64,
        "child": {"pid": reap["pid"], "pgid": reap["pid"]},
        "lease": item["registration"]["lease"],
        "deadline_monotonic_ns": item["registration"]["deadline_monotonic_ns"],
    }
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].arm(bad_arm)
    done = item["registry"].complete(completion(item, armed, reap))
    with pytest.raises(RegistryError, match="cas"):
        item["registry"].complete(
            {**completion(item, armed, reap), "previous_record_sha256": "0" * 64}
        )
    assert record(store, item)["record_sha256"] == done["record_sha256"]


def test_real_fd_resolution_keeps_inherited_lock_and_unknown_budget(
    factory, store, settings
):
    path = settings.home / "responses-helper" / "lifetime.lease"
    path.parent.mkdir(mode=0o700, parents=True)
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        info = os.fstat(descriptor)
        item = factory(lease=LeaseIdentity(info.st_dev, info.st_ino))
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        item["registry"].register(item["registration"])
        item["ledger"].settle(item["reservation"], None)
        usage = copy.deepcopy(store.get(item["run_id"])["usage"])
        current = record(store, item)
        inspection = inspect_helper(settings, store, item["action_id"])
        assert inspection["lease"]["locked"]
        from doonex.responses_process import inspect_lease

        with pytest.raises(RegistryError, match="lease_locked"):
            resolve(settings, store, item, current, lease_inspector=inspect_lease)
        # Registry/inspector must close their own FD, never LOCK_UN the holder.
        assert inspect_helper(settings, store, item["action_id"])["lease"]["locked"]
    finally:
        os.close(descriptor)
    resolved = resolve(settings, store, item, current, lease_inspector=inspect_lease)
    assert resolved["record"]["state"] == "operator_resolved"
    assert path.stat().st_ino == info.st_ino
    assert store.get(item["run_id"])["usage"] == usage


@pytest.mark.parametrize("mode", ["missing", "replaced"])
def test_real_missing_or_replaced_lease_is_never_recreated(
    factory, store, settings, mode
):
    path = settings.home / "responses-helper" / "lifetime.lease"
    path.parent.mkdir(mode=0o700, parents=True)
    path.touch(mode=0o600)
    info = path.stat()
    item = factory(lease=LeaseIdentity(info.st_dev, info.st_ino))
    item["registry"].register(item["registration"])
    current = record(store, item)
    path.unlink()
    if mode == "replaced":
        path.touch(mode=0o600)
    from doonex.responses_process import inspect_lease

    with pytest.raises((OSError, RuntimeError)):
        resolve(settings, store, item, current, lease_inspector=inspect_lease)
    assert record(store, item) == current and fenced(store)
    assert path.exists() is (mode == "replaced")


def test_unregistered_direct_action_cannot_register_helper(factory):
    item = factory(marker=False)
    with pytest.raises(RegistryError, match="transport_mode"):
        item["registry"].register(item["registration"])
