"""Durable ownership and recovery evidence for one Responses helper attempt.

This module never reserves, settles, retries, signals a PID, or claims an orphan
was reaped.  Hooks are bound to one already-reserved model action by trusted
controller code; request payloads cannot select their database or action.
"""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from psycopg.types.json import Jsonb

from .responses_process import (
    CLEANUP_ERROR_CODES,
    FAILURE_REASONS,
    LeaseIdentity,
    LeaseSpec,
    inspect_lease,
)
from .store import OwnershipLost, dispatch_lock

MODE = "isolated_direct_v1"
TERMINAL_STATES = frozenset({"reaped", "not_spawned", "operator_resolved"})
STATES = TERMINAL_STATES | {"prepared", "armed", "blocked"}
HASH_ALGORITHM = "postgresql18-jsonb-send-v1-sha256"
WORKER_LOCK = 4142061106
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = _HASH
_MAX_INT = 2**63 - 1
_BASE_FIELDS = {"schema", "transport", "attempt_id", "client_request_id", "nonce"}
_FIELDS = _BASE_FIELDS | {
    "run_id",
    "action_id",
    "owner",
    "parent",
    "deadline_monotonic_ns",
    "observed_at_utc",
    "lease",
    "state",
    "spawn_attempted",
    "child",
    "reap",
    "terminal_summary",
    "failure",
    "resolution",
    "record_sha256",
    "registered",
    "transition_from_sha256",
}
_REAP_FIELDS = {"pid", "wait_status", "exit_code", "observed_monotonic_ns"}
_FAILURE_FIELDS = {"reason", "spawn_attempted", "child_pid", "reaped", "errors"}
_RESOLUTION_FIELDS = {
    "previous_record_sha256",
    "original_state",
    "observed_at_utc",
    "reason",
}


class RegistryError(ValueError):
    """A fixed non-secret controller error; raw payloads are never formatted."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Responses helper registry rejected: {reason}")


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def _array(values):
    return "ARRAY[" + ",".join(_literal(value) for value in sorted(values)) + "]"


def _object(expression, fields, condition="TRUE"):
    # CASE is deliberate: JSONB deletion/array functions reject scalar input.
    expression = f"({expression})"
    return (
        f"CASE WHEN jsonb_typeof({expression})='object' THEN "
        f"({expression} ?& {_array(fields)} AND "
        f"({expression} - {_array(fields)})='{{}}'::jsonb AND ({condition})) "
        "ELSE FALSE END"
    )


def _integer(expression, *, minimum=0, maximum=_MAX_INT):
    return (
        f"CASE WHEN jsonb_typeof({expression})='number' AND "
        f"({expression} #>> '{{}}') ~ '^[0-9]+$' THEN "
        f"({expression} #>> '{{}}')::numeric BETWEEN {minimum} AND {maximum} "
        "ELSE FALSE END"
    )


def _text(expression, *, maximum=128, minimum=1):
    return (
        f"jsonb_typeof({expression})='string' AND "
        f"length({expression} #>> '{{}}') BETWEEN {minimum} AND {maximum} AND "
        f"({expression} #>> '{{}}') !~ '[[:cntrl:]]'"
    )


def _uuid_sql(expression):
    return (
        f"jsonb_typeof({expression})='string' AND ({expression} #>> '{{}}') ~ "
        "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
    )


def _hash_sql(expression):
    return (
        f"jsonb_typeof({expression})='string' AND "
        f"({expression} #>> '{{}}') ~ '^[0-9a-f]{{64}}$'"
    )


def _nullable(expression, condition):
    return f"({expression}='null'::jsonb OR ({condition}))"


_R = "(data->'transport_helper')"
_LEASE = _object(
    f"{_R}->'lease'",
    {"device", "inode"},
    f"{_integer(f"{_R}->'lease'->'device'")} AND "
    f"{_integer(f"{_R}->'lease'->'inode'", minimum=1)}",
)
_PARENT = _object(
    f"{_R}->'parent'",
    {"pid", "observed_monotonic_ns", "boot_id"},
    f"{_integer(f"{_R}->'parent'->'pid'", minimum=1, maximum=2**31 - 1)} AND "
    f"{_integer(f"{_R}->'parent'->'observed_monotonic_ns'", minimum=1)} AND "
    f"{_text(f"{_R}->'parent'->'boot_id'")}",
)
_CHILD = _object(
    f"{_R}->'child'",
    {"pid", "pgid"},
    f"{_integer(f"{_R}->'child'->'pid'", minimum=1, maximum=2**31 - 1)} AND "
    f"{_integer(f"{_R}->'child'->'pgid'", minimum=1, maximum=2**31 - 1)}",
)
_REAP = _object(
    f"{_R}->'reap'",
    _REAP_FIELDS,
    f"{_integer(f"{_R}->'reap'->'pid'", minimum=1, maximum=2**31 - 1)} AND "
    f"{_integer(f"{_R}->'reap'->'wait_status'", maximum=2**31 - 1)} AND "
    f"{_integer(f"{_R}->'reap'->'observed_monotonic_ns'", minimum=1)} AND "
    f"jsonb_typeof({_R}->'reap'->'exit_code')='number' AND "
    f"({_R}->'reap'->>'exit_code') ~ '^-?[0-9]+$'",
)
_SUMMARY = _object(
    f"{_R}->'terminal_summary'",
    {"kind", "outcome", "reason"},
    f"{_R}->'terminal_summary'->>'kind' IN ('result','error','none') AND "
    f"{_R}->'terminal_summary'->>'outcome' IN ('success','failed') AND "
    f"{_R}->'terminal_summary'->>'reason'=ANY({_array(FAILURE_REASONS)})",
)
_ERRORS = f"{_R}->'failure'->'errors'"
_SORTED_ERRORS = " || ".join(
    f"CASE WHEN {_ERRORS} ? {_literal(code)} THEN "
    f"'[\"{code}\"]'::jsonb ELSE '[]'::jsonb END"
    for code in sorted(CLEANUP_ERROR_CODES)
)
_FAILURE = _object(
    f"{_R}->'failure'",
    _FAILURE_FIELDS,
    f"{_R}->'failure'->>'reason'=ANY({_array(FAILURE_REASONS)}) AND "
    f"jsonb_typeof({_R}->'failure'->'spawn_attempted')='boolean' AND "
    f"jsonb_typeof({_R}->'failure'->'reaped')='boolean' AND "
    f"{_nullable(f"{_R}->'failure'->'child_pid'", _integer(f"{_R}->'failure'->'child_pid'", minimum=1, maximum=2**31 - 1))} AND "
    f"CASE WHEN jsonb_typeof({_ERRORS})='array' THEN "
    f"jsonb_array_length({_ERRORS})<=16 AND ({_SORTED_ERRORS})={_ERRORS} "
    "ELSE FALSE END",
)
_RESOLUTION = _object(
    f"{_R}->'resolution'",
    _RESOLUTION_FIELDS,
    f"{_hash_sql(f"{_R}->'resolution'->'previous_record_sha256'")} AND "
    f"{_R}->'resolution'->>'original_state' IN ('prepared','armed','blocked') AND "
    f"{_text(f"{_R}->'resolution'->'observed_at_utc'", maximum=40)} AND "
    f"{_text(f"{_R}->'resolution'->'reason'", maximum=512)}",
)
_COMMON = " AND ".join(
    [
        "kind='model'",
        f"data->>'transport_mode'='{MODE}'",
        f"{_R}->'schema'='1'::jsonb",
        f"{_R}->>'transport'='{MODE}'",
        *(
            _uuid_sql(f"{_R}->'{name}'")
            for name in ("run_id", "action_id", "attempt_id", "client_request_id")
        ),
        f"{_R}->>'run_id'=run_id::text",
        f"{_R}->>'action_id'=id::text",
        _hash_sql(f"{_R}->'nonce'"),
        _hash_sql(f"{_R}->'record_sha256'"),
        _nullable(
            f"{_R}->'transition_from_sha256'",
            _hash_sql(f"{_R}->'transition_from_sha256'"),
        ),
        _text(f"{_R}->'owner'"),
        (
            f"(({_R}->'registered'='true'::jsonb AND ({_PARENT}) AND ({_LEASE}) AND "
            f"({_integer(f"{_R}->'deadline_monotonic_ns'", minimum=1)})) OR "
            f"({_R}->'registered'='false'::jsonb AND {_R}->>'state'='not_spawned' AND "
            f"{_R}->'parent'='null'::jsonb AND {_R}->'lease'='null'::jsonb AND "
            f"{_R}->'deadline_monotonic_ns'='null'::jsonb))"
        ),
        _text(f"{_R}->'observed_at_utc'", maximum=40),
        f"jsonb_typeof({_R}->'spawn_attempted')='boolean'",
        _nullable(f"{_R}->'child'", _CHILD),
        _nullable(f"{_R}->'reap'", _REAP),
        _nullable(f"{_R}->'terminal_summary'", _SUMMARY),
        _nullable(f"{_R}->'failure'", _FAILURE),
        _nullable(f"{_R}->'resolution'", _RESOLUTION),
        f"{_R}->>'record_sha256'=encode(sha256(jsonb_send({_R}-'record_sha256')),'hex')",
    ]
)
_TERMINAL = (
    f"({_R}->>'state'='not_spawned' AND {_R}->'spawn_attempted'='false'::jsonb "
    f"AND {_R}->'child'='null'::jsonb AND {_R}->'reap'='null'::jsonb "
    f"AND {_R}->'terminal_summary'='null'::jsonb AND {_R}->'resolution'='null'::jsonb "
    f"AND {_R}->'failure'->'spawn_attempted'='false'::jsonb "
    f"AND {_R}->'failure'->'child_pid'='null'::jsonb "
    f"AND {_R}->'failure'->'reaped'='false'::jsonb) OR "
    f"({_R}->>'state'='reaped' AND {_R}->'spawn_attempted'='true'::jsonb "
    f"AND ({_REAP}) AND ({_SUMMARY}) AND {_R}->'resolution'='null'::jsonb) OR "
    f"({_R}->>'state'='operator_resolved' AND ({_RESOLUTION}))"
)
_VALID_TERMINAL = (
    f"COALESCE(({_object(_R, _FIELDS, f'({_COMMON}) AND ({_TERMINAL})')}),FALSE)"
)
_CANDIDATE = f"(data ? 'transport_helper' OR data->>'transport_mode'='{MODE}')"
_FENCED = f"({_CANDIDATE}) AND NOT ({_VALID_TERMINAL})"
HELPER_FENCE_PREDICATE = _FENCED
HELPER_REGISTRY_SCHEMA = f"""
CREATE INDEX IF NOT EXISTS actions_helper_fenced ON actions(id) WHERE {_FENCED};
CREATE INDEX IF NOT EXISTS actions_helper_history ON actions(id)
 WHERE data ? 'transport_helper' AND data->'transport_helper'->'registered'='true'::jsonb;
"""


def _fenced(connection, exclude_action_id=None):
    exclusion = " AND id<>%s" if exclude_action_id is not None else ""
    return bool(
        connection.execute(
            f"SELECT id FROM actions WHERE {_FENCED}{exclusion} ORDER BY id LIMIT 1",
            (exclude_action_id,) if exclude_action_id is not None else (),
        ).fetchone()
    )


def helper_fenced(connection):
    """Use the matching partial index; invalid terminal hashes also fence."""
    return _fenced(connection)


def list_unresolved(connection, *, limit=100, after_id=None):
    """Keyset pagination over every unresolved action, regardless of Run state."""
    _int(limit, 1, 100)
    if after_id is not None:
        _uuid(after_id)
    cursor = " AND id>%s" if after_id is not None else ""
    parameters = (after_id, limit + 1) if after_id is not None else (limit + 1,)
    rows = connection.execute(
        f"SELECT id,run_id FROM actions WHERE {_FENCED}{cursor} ORDER BY id LIMIT %s",
        parameters,
    ).fetchall()
    more = len(rows) > limit
    items = [
        {"action_id": str(row["id"]), "run_id": str(row["run_id"])}
        for row in rows[:limit]
    ]
    return {"items": items, "next_cursor": items[-1]["action_id"] if more else None}


def _digest(connection, record):
    # jsonb_send is immutable and includes PostgreSQL JSONB wire version 1.
    # Computing exactly the indexed expression avoids trusting a cached flag.
    return connection.execute(
        "SELECT encode(sha256(jsonb_send(%s::jsonb-'record_sha256')),'hex') AS hash",
        (Jsonb(record),),
    ).fetchone()["hash"]


def _exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise RegistryError("fields")


def _int(value, minimum=0, maximum=_MAX_INT):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RegistryError("integer")


def _string(value, maximum=128):
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise RegistryError("string")


def _uuid(value):
    if type(value) is not str:
        raise RegistryError("uuid")
    try:
        if str(UUID(value)) != value:
            raise RegistryError("uuid")
    except ValueError as error:
        raise RegistryError("uuid") from error


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise RegistryError("hash")


def _base(value):
    if (
        value["schema"] != 1
        or type(value["schema"]) is not int
        or value["transport"] != MODE
    ):
        raise RegistryError("schema")
    _uuid(value["attempt_id"])
    _uuid(value["client_request_id"])
    _hash(value["nonce"])


def _lease(value):
    _exact(value, {"device", "inode"})
    _int(value["device"])
    _int(value["inode"], 1)
    return LeaseIdentity(**value)


def _parent(value):
    _exact(value, {"pid", "observed_monotonic_ns", "boot_id"})
    _int(value["pid"], 1, 2**31 - 1)
    _int(value["observed_monotonic_ns"], 1)
    _string(value["boot_id"])


def _timestamp(value):
    _string(value, 40)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RegistryError("timestamp") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise RegistryError("timestamp")


def _child(value):
    _exact(value, {"pid", "pgid"})
    _int(value["pid"], 1, 2**31 - 1)
    _int(value["pgid"], 1, 2**31 - 1)


def _reap(value):
    _exact(value, _REAP_FIELDS)
    _int(value["pid"], 1, 2**31 - 1)
    _int(value["wait_status"], 0, 2**31 - 1)
    _int(value["observed_monotonic_ns"], 1)
    if not (os.WIFEXITED(value["wait_status"]) or os.WIFSIGNALED(value["wait_status"])):
        raise RegistryError("reap_status")
    if (
        type(value["exit_code"]) is not int
        or os.waitstatus_to_exitcode(value["wait_status"]) != value["exit_code"]
    ):
        raise RegistryError("reap_status")


def _summary(value, reap):
    _exact(value, {"kind", "outcome", "reason"})
    if (
        value["kind"] not in {"result", "error", "none"}
        or value["outcome"] not in {"success", "failed"}
        or value["reason"] not in FAILURE_REASONS
    ):
        raise RegistryError("summary")
    success = value["outcome"] == "success"
    if success != (value["reason"] == "success") or (
        success and (value["kind"] != "result" or reap["exit_code"] != 0)
    ):
        raise RegistryError("summary")


def _failure(value):
    _exact(value, _FAILURE_FIELDS)
    if (
        value["reason"] not in FAILURE_REASONS
        or type(value["spawn_attempted"]) is not bool
        or type(value["reaped"]) is not bool
    ):
        raise RegistryError("failure")
    if value["child_pid"] is not None:
        _int(value["child_pid"], 1, 2**31 - 1)
    errors = value["errors"]
    if (
        type(errors) is not list
        or len(errors) > 16
        or any(
            type(error) is not str or error not in CLEANUP_ERROR_CODES
            for error in errors
        )
        or len(set(errors)) != len(errors)
    ):
        raise RegistryError("cleanup_errors")


def _record(connection, value, *, run_id=None, action_id=None):
    _exact(value, _FIELDS)
    _base(value)
    for name in ("run_id", "action_id"):
        _uuid(value[name])
    if (run_id is not None and value["run_id"] != run_id) or (
        action_id is not None and value["action_id"] != action_id
    ):
        raise RegistryError("binding")
    _string(value["owner"])
    if value["transition_from_sha256"] is not None:
        _hash(value["transition_from_sha256"])
    if type(value["registered"]) is not bool:
        raise RegistryError("registered")
    if value["registered"]:
        _parent(value["parent"])
        _lease(value["lease"])
        _int(value["deadline_monotonic_ns"], 1)
    elif value["state"] != "not_spawned" or any(
        value[key] is not None for key in ("parent", "lease", "deadline_monotonic_ns")
    ):
        raise RegistryError("registered")
    _timestamp(value["observed_at_utc"])
    if value["state"] not in STATES or type(value["spawn_attempted"]) is not bool:
        raise RegistryError("state")
    if value["child"] is not None:
        _child(value["child"])
    if value["reap"] is not None:
        _reap(value["reap"])
    if value["terminal_summary"] is not None:
        if value["reap"] is None:
            raise RegistryError("reap_missing")
        _summary(value["terminal_summary"], value["reap"])
    if value["failure"] is not None:
        _failure(value["failure"])
    if value["resolution"] is not None:
        item = value["resolution"]
        _exact(item, _RESOLUTION_FIELDS)
        _hash(item["previous_record_sha256"])
        if item["original_state"] not in {"prepared", "armed", "blocked"}:
            raise RegistryError("resolution")
        _timestamp(item["observed_at_utc"])
        _string(item["reason"], 512)
    state = value["state"]
    if state == "prepared" and (
        value["spawn_attempted"]
        or any(
            value[key] is not None
            for key in ("child", "reap", "terminal_summary", "failure", "resolution")
        )
    ):
        raise RegistryError("state")
    if state == "armed" and (
        not value["spawn_attempted"]
        or value["child"] is None
        or any(
            value[key] is not None
            for key in ("reap", "terminal_summary", "failure", "resolution")
        )
    ):
        raise RegistryError("state")
    if state == "reaped" and (
        not value["spawn_attempted"]
        or value["reap"] is None
        or value["terminal_summary"] is None
        or value["resolution"] is not None
    ):
        raise RegistryError("state")
    if state == "not_spawned" and (
        value["spawn_attempted"]
        or any(
            value[key] is not None
            for key in ("child", "reap", "terminal_summary", "resolution")
        )
        or value["failure"] is None
        or value["failure"]["spawn_attempted"]
        or value["failure"]["child_pid"] is not None
        or value["failure"]["reaped"]
    ):
        raise RegistryError("state")
    if state == "operator_resolved" and value["resolution"] is None:
        raise RegistryError("state")
    _hash(value["record_sha256"])
    if _digest(connection, value) != value["record_sha256"]:
        raise RegistryError("record_hash")
    return value


def historical_lease_identity(connection):
    """The first immutable helper identity binds every subsequent attempt."""
    row = connection.execute(
        "SELECT id,run_id,data->'transport_helper' AS record FROM actions "
        "WHERE data ? 'transport_helper' AND "
        "data->'transport_helper'->'registered'='true'::jsonb ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    record = _record(
        connection, row["record"], run_id=str(row["run_id"]), action_id=str(row["id"])
    )
    return _lease(record["lease"])


def _lease_path(settings):
    return Path(settings.home) / "responses-helper" / "lifetime.lease"


def lease_spec(
    settings, store, *, check_owner, lease_inspector=None, exclude_action_id=None
):
    """Select a permanent inode only while a trusted worker owns dispatch."""
    inspector = lease_inspector or inspect_lease
    check_owner()
    with store.connect() as connection:
        dispatch_lock(connection)
        check_owner()
        if _fenced(connection, exclude_action_id):
            raise RegistryError("fenced")
        expected = historical_lease_identity(connection)
        path = _lease_path(settings)
        if expected is not None:
            observed = inspector(LeaseSpec(path, expected, False))
            if observed.identity != expected or observed.locked:
                raise RegistryError(
                    "lease_locked" if observed.locked else "lease_changed"
                )
            return LeaseSpec(path, expected, False)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            observed = inspector(LeaseSpec(path, None, False))
            if observed.locked:
                raise RegistryError("lease_locked")
            return LeaseSpec(path, observed.identity, False)
        return LeaseSpec(path, None, True)


class ResponsesRegistry:
    def __init__(
        self,
        settings,
        store,
        *,
        run_id,
        owner,
        action_id,
        check_owner,
        lease_inspector=None,
    ):
        _uuid(run_id)
        _uuid(action_id)
        _string(owner)
        self.settings, self.store = settings, store
        self.run_id, self.owner, self.action_id = run_id, owner, action_id
        self.check_owner = check_owner
        self.lease_inspector = lease_inspector or inspect_lease

    def _locked(self, connection):
        dispatch_lock(connection)
        self.check_owner()
        run = connection.execute(
            "SELECT data FROM runs WHERE id=%s FOR UPDATE", (self.run_id,)
        ).fetchone()
        if run is None or run["data"].get("owner") != self.owner:
            raise OwnershipLost("执行权已移交，旧 helper 登记器停止操作")
        action = connection.execute(
            "SELECT id,run_id,kind,status,data FROM actions WHERE id=%s FOR UPDATE",
            (self.action_id,),
        ).fetchone()
        if (
            action is None
            or str(action["run_id"]) != self.run_id
            or action["kind"] != "model"
        ):
            raise RegistryError("action_binding")
        self.check_owner()
        action["_run"] = run["data"]
        return action

    def _bound_record(self, connection, action, payload):
        value = _record(
            connection,
            action["data"].get("transport_helper"),
            run_id=self.run_id,
            action_id=self.action_id,
        )
        if value["owner"] != self.owner or any(
            value[name] != payload[name] for name in _BASE_FIELDS
        ):
            raise RegistryError("binding")
        if value["registered"] and value["parent"]["pid"] != os.getpid():
            raise RegistryError("parent_identity")
        return value

    def _save(self, connection, record):
        record = {**record, "record_sha256": _digest(connection, record)}
        _record(connection, record, run_id=self.run_id, action_id=self.action_id)
        self.check_owner()
        connection.execute(
            "UPDATE actions SET data=data || %s WHERE id=%s",
            (
                Jsonb({"transport_mode": MODE, "transport_helper": record}),
                self.action_id,
            ),
        )
        return self._ack(record)

    @staticmethod
    def _ack(record):
        return {
            "schema": 1,
            "attempt_id": record["attempt_id"],
            "nonce": record["nonce"],
            "state": record["state"],
            "record_sha256": record["record_sha256"],
        }

    def register(self, payload):
        _exact(
            payload,
            _BASE_FIELDS
            | {"parent", "deadline_monotonic_ns", "observed_at_utc", "lease"},
        )
        _base(payload)
        _parent(payload["parent"])
        identity = _lease(payload["lease"])
        _int(payload["deadline_monotonic_ns"], 1)
        _timestamp(payload["observed_at_utc"])
        if (
            payload["parent"]["pid"] != os.getpid()
            or payload["deadline_monotonic_ns"] <= time.monotonic_ns()
        ):
            raise RegistryError("parent_identity")
        with self.store.connect() as connection:
            action = self._locked(connection)
            if (
                action["_run"]["state"] != "running"
                or action["_run"].get("requested_action") is not None
            ):
                raise RegistryError("run_stopped")
            if action["status"] != "pending":
                raise RegistryError("action_state")
            if action["data"].get("transport_mode") != MODE:
                raise RegistryError("transport_mode")
            if "transport_helper" in action["data"]:
                current = self._bound_record(connection, action, payload)
                if current["state"] == "prepared" and all(
                    current[name] == value for name, value in payload.items()
                ):
                    return self._ack(current)
                raise RegistryError("already_registered")
            if _fenced(connection, self.action_id):
                raise RegistryError("fenced")
            expected = historical_lease_identity(connection)
            if expected is not None and identity != expected:
                raise RegistryError("lease_changed")
            record = {
                **payload,
                "run_id": self.run_id,
                "action_id": self.action_id,
                "owner": self.owner,
                "registered": True,
                "transition_from_sha256": None,
                "state": "prepared",
                "spawn_attempted": False,
                "child": None,
                "reap": None,
                "terminal_summary": None,
                "failure": None,
                "resolution": None,
            }
            acknowledgment = self._save(connection, record)
        return acknowledgment

    def arm(self, payload):
        _exact(
            payload,
            _BASE_FIELDS
            | {"previous_record_sha256", "child", "lease", "deadline_monotonic_ns"},
        )
        _base(payload)
        _hash(payload["previous_record_sha256"])
        _child(payload["child"])
        _lease(payload["lease"])
        _int(payload["deadline_monotonic_ns"], 1)
        with self.store.connect() as connection:
            action = self._locked(connection)
            if (
                action["_run"]["state"] != "running"
                or action["_run"].get("requested_action") is not None
            ):
                raise RegistryError("run_stopped")
            record = self._bound_record(connection, action, payload)
            if (
                record["state"] == "armed"
                and record["transition_from_sha256"]
                == payload["previous_record_sha256"]
                and all(
                    record[name] == payload[name]
                    for name in ("child", "lease", "deadline_monotonic_ns")
                )
            ):
                return self._ack(record)
            if (
                action["status"] != "pending"
                or record["state"] != "prepared"
                or record["record_sha256"] != payload["previous_record_sha256"]
            ):
                raise RegistryError("cas")
            if (
                any(
                    record[name] != payload[name]
                    for name in ("lease", "deadline_monotonic_ns")
                )
                or payload["deadline_monotonic_ns"] <= time.monotonic_ns()
            ):
                raise RegistryError("arm_binding")
            acknowledgment = self._save(
                connection,
                {
                    **record,
                    "state": "armed",
                    "spawn_attempted": True,
                    "child": payload["child"],
                    "transition_from_sha256": record["record_sha256"],
                },
            )
        return acknowledgment

    def complete(self, payload):
        _exact(
            payload,
            _BASE_FIELDS | {"previous_record_sha256", "reap", "terminal_summary"},
        )
        _base(payload)
        _hash(payload["previous_record_sha256"])
        _reap(payload["reap"])
        _summary(payload["terminal_summary"], payload["reap"])
        with self.store.connect() as connection:
            action = self._locked(connection)
            record = self._bound_record(connection, action, payload)
            if (
                record["state"] == "reaped"
                and record["transition_from_sha256"]
                == payload["previous_record_sha256"]
                and all(
                    record[name] == payload[name]
                    for name in ("reap", "terminal_summary")
                )
            ):
                return self._ack(record)
            if (
                record["state"] not in {"prepared", "armed"}
                or record["record_sha256"] != payload["previous_record_sha256"]
            ):
                raise RegistryError("cas")
            if (
                record["child"] is not None
                and record["child"]["pid"] != payload["reap"]["pid"]
            ) or payload["reap"]["observed_monotonic_ns"] < record["parent"][
                "observed_monotonic_ns"
            ]:
                raise RegistryError("reap_binding")
            acknowledgment = self._save(
                connection,
                {
                    **record,
                    "state": "reaped",
                    "spawn_attempted": True,
                    "reap": payload["reap"],
                    "terminal_summary": payload["terminal_summary"],
                    "transition_from_sha256": record["record_sha256"],
                },
            )
        return acknowledgment

    def block(self, payload):
        _exact(payload, _BASE_FIELDS | {"previous_record_sha256"} | _FAILURE_FIELDS)
        _base(payload)
        if payload["previous_record_sha256"] is not None:
            _hash(payload["previous_record_sha256"])
        failure = {name: payload[name] for name in _FAILURE_FIELDS}
        _failure(failure)
        failure["errors"] = sorted(failure["errors"])
        with self.store.connect() as connection:
            action = self._locked(connection)
            if "transport_helper" not in action["data"]:
                if (
                    action["data"].get("transport_mode") != MODE
                    or payload["previous_record_sha256"] is not None
                    or payload["spawn_attempted"]
                    or payload["child_pid"] is not None
                    or payload["reaped"]
                ):
                    raise RegistryError("not_spawned_binding")
                record = {
                    **{name: payload[name] for name in _BASE_FIELDS},
                    "run_id": self.run_id,
                    "action_id": self.action_id,
                    "owner": self.owner,
                    "registered": False,
                    "transition_from_sha256": None,
                    "state": "not_spawned",
                    "parent": None,
                    "lease": None,
                    "deadline_monotonic_ns": None,
                    "observed_at_utc": datetime.now(UTC).isoformat(),
                    "spawn_attempted": False,
                    "child": None,
                    "reap": None,
                    "terminal_summary": None,
                    "failure": failure,
                    "resolution": None,
                }
            else:
                record = self._bound_record(connection, action, payload)
                if record["state"] in TERMINAL_STATES:
                    if payload["previous_record_sha256"] not in {
                        record["record_sha256"],
                        record["transition_from_sha256"],
                    }:
                        raise RegistryError("cas")
                    return self._ack(record)
                if record["record_sha256"] != payload[
                    "previous_record_sha256"
                ] and not (
                    payload["previous_record_sha256"] is None
                    and record["state"] == "prepared"
                    and not payload["spawn_attempted"]
                ):
                    raise RegistryError("cas")
                not_spawned = (
                    record["state"] == "prepared"
                    and not record["spawn_attempted"]
                    and not payload["spawn_attempted"]
                    and payload["child_pid"] is None
                    and not payload["reaped"]
                )
                record = {
                    **record,
                    "state": "not_spawned" if not_spawned else "blocked",
                    "spawn_attempted": record["spawn_attempted"]
                    or payload["spawn_attempted"],
                    "failure": failure,
                    "transition_from_sha256": payload["previous_record_sha256"],
                }
            acknowledgment = self._save(connection, record)
        return acknowledgment


def _inspection(settings, connection, action, inspector):
    record = action["data"].get("transport_helper")
    problems = []
    valid = False
    try:
        _record(
            connection,
            record,
            run_id=str(action["run_id"]),
            action_id=str(action["id"]),
        )
        valid = True
    except (RegistryError, TypeError, KeyError):
        problems.append("record_missing" if record is None else "record_invalid")
    observation = None
    if valid and record["registered"]:
        try:
            identity = _lease(record["lease"])
            evidence = inspector(LeaseSpec(_lease_path(settings), identity, False))
            if evidence.identity != identity:
                problems.append("lease_changed")
            else:
                observation = {**identity.data(), "locked": evidence.locked}
                if evidence.locked:
                    problems.append("lease_locked")
        except (OSError, ValueError, RuntimeError):
            problems.append("lease_unavailable")
    return {
        "schema": 1,
        "action_id": str(action["id"]),
        "run_id": str(action["run_id"]),
        "record_sha256": record.get("record_sha256") if valid else None,
        "record": record if valid else None,
        "valid": valid,
        "unresolved": not valid or record["state"] not in TERMINAL_STATES,
        "lease": observation,
        "problems": problems,
    }


def inspect_helper(settings, store, action_id, *, lease_inspector=None):
    """Read bounded evidence; no lock acquisition is treated as reap evidence."""
    _uuid(action_id)
    with store.connect() as connection:
        action = connection.execute(
            "SELECT id,run_id,data FROM actions WHERE id=%s AND kind='model'",
            (action_id,),
        ).fetchone()
        if action is None:
            raise RegistryError("action_binding")
        return _inspection(
            settings, connection, action, lease_inspector or inspect_lease
        )


def resolve_helper(
    settings,
    store,
    action_id,
    *,
    expected_record_sha256,
    attempt_id,
    nonce,
    lease_identity,
    reason,
    lease_inspector=None,
):
    """Hash-bound operator resolution while holding an exclusive worker session.

    The caller supplies explicit human authorization.  This function preserves
    all action status, reservation, usage and original reap evidence verbatim.
    """
    _uuid(action_id)
    _uuid(attempt_id)
    _hash(expected_record_sha256)
    _hash(nonce)
    identity = _lease(lease_identity)
    _string(reason, 512)
    inspector = lease_inspector or inspect_lease
    with store.connect() as worker_connection:
        worker_connection.autocommit = True
        acquired = worker_connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
        ).fetchone()["acquired"]
        if not acquired:
            raise RegistryError("worker_active")
        # Closing this session releases its lock, even if any later step fails.
        with store.connect() as connection:
            dispatch_lock(connection)
            row = connection.execute(
                "SELECT run_id FROM actions WHERE id=%s", (action_id,)
            ).fetchone()
            if row is None:
                raise RegistryError("action_binding")
            connection.execute(
                "SELECT id FROM runs WHERE id=%s FOR UPDATE", (row["run_id"],)
            ).fetchone()
            action = connection.execute(
                "SELECT id,run_id,kind,status,data FROM actions WHERE id=%s FOR UPDATE",
                (action_id,),
            ).fetchone()
            if action["kind"] != "model" or action["run_id"] != row["run_id"]:
                raise RegistryError("action_binding")
            record = _record(
                connection,
                action["data"].get("transport_helper"),
                run_id=str(action["run_id"]),
                action_id=action_id,
            )
            if (
                record["state"] in TERMINAL_STATES
                or record["record_sha256"] != expected_record_sha256
                or record["attempt_id"] != attempt_id
                or record["nonce"] != nonce
                or _lease(record["lease"]) != identity
            ):
                raise RegistryError("resolution_binding")
            evidence = inspector(LeaseSpec(_lease_path(settings), identity, False))
            if evidence.identity != identity or evidence.locked:
                raise RegistryError(
                    "lease_locked" if evidence.locked else "lease_changed"
                )
            resolution = {
                "previous_record_sha256": expected_record_sha256,
                "original_state": record["state"],
                "observed_at_utc": datetime.now(UTC).isoformat(),
                "reason": reason,
            }
            updated = {
                **record,
                "state": "operator_resolved",
                "resolution": resolution,
                "transition_from_sha256": record["record_sha256"],
            }
            updated["record_sha256"] = _digest(connection, updated)
            _record(
                connection, updated, run_id=str(action["run_id"]), action_id=action_id
            )
            connection.execute(
                "UPDATE actions SET data=jsonb_set(data,'{transport_helper}',%s) WHERE id=%s",
                (Jsonb(updated), action_id),
            )
        return inspect_helper(settings, store, action_id, lease_inspector=inspector)
