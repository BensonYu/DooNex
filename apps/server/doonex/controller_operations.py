"""Crash-durable admission for bounded controller Git and Docker groups.

This registry does not run subprocesses, signal stored PIDs, settle model usage,
or infer old process death. All writes commit before returning live capabilities.
The two production Popen sites must require those capabilities themselves.
"""

from __future__ import annotations

import copy
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID, uuid4, uuid5

from psycopg import sql
from psycopg.types.json import Jsonb

from .store import OwnershipLost, dispatch_lock

MAX_PROCESSES = 96
MAX_CONTAINERS = 64
HASH_ALGORITHM = "postgresql18-jsonb-send-v1-sha256"
KINDS = frozenset(
    {
        "git_validate",
        "git_export",
        "git_api_read",
        "sandbox_execute",
        "sandbox_probe",
        "sandbox_cleanup",
    }
)
PHASES = frozenset(
    {
        "git",
        "mount_validation",
        "docker_create",
        "docker_start",
        "docker_inspect",
        "docker_ps",
        "docker_rm",
    }
)
_CLEANUP_PHASES = frozenset({"docker_ps", "docker_rm"})
_REASONS = frozenset(
    {
        "success",
        "command_failed",
        "cancelled",
        "deadline",
        "operation_failed",
        "supervision_unknown",
        "daemon_unknown",
        "owner_lost",
        "interrupted",
        "legacy_unidentified",
    }
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CONTAINER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_LIVE = object()
_CURRENT = ContextVar("doonex_controller_registry", default=None)
_MAX_INT = 2**63 - 1
_FIELDS = frozenset(
    {
        "schema",
        "operation_id",
        "operation_kind",
        "run_id",
        "project_id",
        "parent_action_id",
        "actor",
        "expected_run_owner",
        "scope",
        "state",
        "revision",
        "previous_record_sha256",
        "business_deadline_monotonic_ns",
        "cleanup_allowance_seconds",
        "spawn_possible",
        "attempted_processes",
        "processes",
        "terminal_evidence",
        "reason",
        "repair_of",
        "record_sha256",
    }
)
_ACTOR_FIELDS = {"kind", "owner", "instance_nonce", "pid", "instance_started_at"}
_SCOPE_FIELDS = {"repository_digest", "container_name", "run_label", "intent_digest"}
_PROCESS_FIELDS = {"seq", "phase", "target", "pid", "exit_code", "reaped", "pipe_eof"}
_EVIDENCE_FIELDS = {
    "outcome",
    "reason",
    "local_process_unknown",
    "daemon_request_unknown",
    "docker",
}
_DOCKER_FIELDS = {
    "create_attempted",
    "create_acknowledged",
    "container_removed",
    "observation_complete",
    "run_label",
    "remaining_containers",
}
_FUNCTION = "doonex_controller_valid_terminal_v1"
FENCE_PREDICATE = f"NOT {_FUNCTION}(id,run_id,parent_action_id,data)"
_INDEX = "controller_operations_fenced_v1"


class RegistryError(RuntimeError):
    """Only fixed reason codes are included; never print caller payloads."""

    def __init__(self, reason, *, operation_id=None):
        self.reason, self.operation_id = reason, operation_id
        super().__init__(f"Controller operation rejected: {reason}")


def _serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if not self._mutex.acquire(blocking=False):
            raise RegistryError("capability_busy")
        try:
            return method(self, *args, **kwargs)
        finally:
            self._mutex.release()

    return guarded


def _exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise RegistryError("fields")


def _integer(value, minimum=0, maximum=_MAX_INT):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RegistryError("integer")


def _text(value, maximum=128):
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise RegistryError("text")


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


def _bool(value):
    if type(value) is not bool:
        raise RegistryError("boolean")


def _timestamp(value):
    _text(value, 40)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RegistryError("timestamp") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise RegistryError("timestamp")


@dataclass(frozen=True)
class Actor:
    kind: str
    owner: str
    instance_nonce: str
    pid: int
    instance_started_at: str
    _seal: object = field(repr=False, compare=False, default=None)

    def data(self):
        return {name: getattr(self, name) for name in _ACTOR_FIELDS}

    def check_live(self):
        if self._seal is not _LIVE or self.pid != os.getpid():
            raise RegistryError("actor_not_live")


def fresh_actor(kind):
    if kind not in {"worker", "api", "cli"}:
        raise RegistryError("actor_kind")
    # This random instance identifies this live process only, not an OS boot or
    # proof that any process from an earlier instance has stopped.
    return Actor(
        kind,
        str(uuid4()),
        secrets.token_hex(32),
        os.getpid(),
        datetime.now(UTC).isoformat(),
        _LIVE,
    )


def scope(
    *, intent_digest, repository_digest=None, container_name=None, run_label=None
):
    value = {
        "intent_digest": intent_digest,
        "repository_digest": repository_digest,
        "container_name": container_name,
        "run_label": run_label,
    }
    _validate_scope(value)
    return value


def _validate_scope(value):
    _exact(value, _SCOPE_FIELDS)
    _hash(value["intent_digest"])
    if value["repository_digest"] is not None:
        _hash(value["repository_digest"])
    if value["container_name"] is not None and (
        type(value["container_name"]) is not str
        or not _CONTAINER.fullmatch(value["container_name"])
    ):
        raise RegistryError("container_name")
    if value["run_label"] is not None:
        _text(value["run_label"], 128)


def _validate_process(value, seq, *, terminal=False):
    _exact(value, _PROCESS_FIELDS)
    _integer(value["seq"], 1, MAX_PROCESSES)
    if value["seq"] != seq or value["phase"] not in PHASES:
        raise RegistryError("process_binding")
    if value["target"] is not None:
        _text(value["target"])
    if value["pid"] is not None:
        _integer(value["pid"], 1, 2**31 - 1)
    if value["exit_code"] is not None:
        _integer(value["exit_code"], -255, 255)
    _bool(value["reaped"])
    _bool(value["pipe_eof"])
    if value["reaped"] != (value["exit_code"] is not None) or (
        terminal
        and (value["pid"] is None or not value["reaped"] or not value["pipe_eof"])
    ):
        raise RegistryError("process_unknown")


def _validate_evidence(value, record):
    _exact(value, _EVIDENCE_FIELDS)
    if value["outcome"] not in {"success", "failed"} or value["reason"] not in {
        "success",
        "command_failed",
        "cancelled",
        "deadline",
        "operation_failed",
    }:
        raise RegistryError("outcome")
    if (value["outcome"] == "success") != (value["reason"] == "success"):
        raise RegistryError("outcome")
    if (
        value["local_process_unknown"] is not False
        or value["daemon_request_unknown"] is not False
    ):
        raise RegistryError("process_unknown")
    if record["operation_kind"].startswith("git_"):
        if value["docker"] is not None:
            raise RegistryError("docker_evidence")
        return
    docker = value["docker"]
    _exact(docker, _DOCKER_FIELDS)
    for name in (
        "create_attempted",
        "create_acknowledged",
        "container_removed",
        "observation_complete",
    ):
        _bool(docker[name])
    _integer(docker["remaining_containers"], 0, MAX_CONTAINERS)
    creates = sum(item["phase"] == "docker_create" for item in record["processes"])
    if creates > 1 or docker["create_attempted"] != bool(creates):
        raise RegistryError("docker_evidence")
    if (
        docker["run_label"] != record["scope"]["run_label"]
        or not docker["observation_complete"]
        or docker["create_attempted"] != docker["create_acknowledged"]
    ):
        raise RegistryError("docker_unknown")
    if record["operation_kind"] != "sandbox_execute" and docker["create_attempted"]:
        raise RegistryError("docker_evidence")
    if record["operation_kind"] in {"sandbox_execute", "sandbox_cleanup"} and (
        docker["remaining_containers"] != 0
        or (docker["create_attempted"] and not docker["container_removed"])
    ):
        raise RegistryError("docker_unknown")


def _validate_record(value):
    _exact(value, _FIELDS)
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise RegistryError("schema")
    _uuid(value["operation_id"])
    for name in ("run_id", "project_id", "parent_action_id", "repair_of"):
        if value[name] is not None:
            _uuid(value[name])
    if value["parent_action_id"] is not None and value["run_id"] is None:
        raise RegistryError("action_binding")
    _validate_scope(value["scope"])
    _integer(value["revision"])
    _integer(value["cleanup_allowance_seconds"], 0, 60)
    _integer(value["attempted_processes"], 0, MAX_PROCESSES)
    _bool(value["spawn_possible"])
    if value["previous_record_sha256"] is not None:
        _hash(value["previous_record_sha256"])
    _hash(value["record_sha256"])
    if value["reason"] is not None and value["reason"] not in _REASONS:
        raise RegistryError("reason")
    if (
        type(value["processes"]) is not list
        or len(value["processes"]) != value["attempted_processes"]
    ):
        raise RegistryError("process_count")
    for seq, item in enumerate(value["processes"], 1):
        _validate_process(item, seq, terminal=value["state"] == "terminal")
    if value["operation_kind"] == "legacy_unidentified_controller":
        if (
            value["state"] != "unknown"
            or value["actor"] is not None
            or value["business_deadline_monotonic_ns"] is not None
            or value["reason"] != "legacy_unidentified"
            or value["processes"]
            or value["terminal_evidence"] is not None
            or value["spawn_possible"] is not True
        ):
            raise RegistryError("legacy_record")
        return
    if value["operation_kind"] not in KINDS:
        raise RegistryError("kind")
    actor = value["actor"]
    _exact(actor, _ACTOR_FIELDS)
    if actor["kind"] not in {"worker", "api", "cli"}:
        raise RegistryError("actor_kind")
    _uuid(actor["owner"])
    _hash(actor["instance_nonce"])
    _integer(actor["pid"], 1, 2**31 - 1)
    _timestamp(actor["instance_started_at"])
    _integer(value["business_deadline_monotonic_ns"], 1)
    if value["expected_run_owner"] is not None:
        _text(value["expected_run_owner"])
    if value["run_id"] is None and value["expected_run_owner"] is not None:
        raise RegistryError("run_binding")
    state = value["state"]
    if state not in {"reserved", "launching", "terminal", "not_spawned", "unknown"}:
        raise RegistryError("state")
    if state == "reserved" and (
        value["revision"] != 0
        or value["previous_record_sha256"] is not None
        or value["spawn_possible"]
        or value["processes"]
        or value["reason"] is not None
    ):
        raise RegistryError("state")
    if state in {"launching", "terminal"} and (
        not value["spawn_possible"]
        or value["revision"] < 1
        or value["previous_record_sha256"] is None
        or value["reason"] is not None
    ):
        raise RegistryError("state")
    if state == "launching" and value["processes"]:
        raise RegistryError("state")
    if state == "not_spawned" and (
        value["spawn_possible"]
        or value["revision"] != 1
        or value["previous_record_sha256"] is None
        or value["processes"]
        or value["reason"] not in {"cancelled", "deadline", "operation_failed"}
    ):
        raise RegistryError("state")
    if state == "unknown" and value["reason"] not in _REASONS - {
        "success",
        "command_failed",
        "legacy_unidentified",
    }:
        raise RegistryError("reason")
    if state == "terminal":
        if not value["processes"] or value["revision"] < 2:
            raise RegistryError("process_count")
        _validate_evidence(value["terminal_evidence"], value)
    elif value["terminal_evidence"] is not None:
        raise RegistryError("state")


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def _array(values):
    return "ARRAY[" + ",".join(_literal(value) for value in sorted(values)) + "]"


def _object(expression, fields, condition="TRUE"):
    expression = f"({expression})"
    return f"CASE WHEN jsonb_typeof({expression})='object' THEN ({expression} ?& {_array(fields)} AND ({expression} - {_array(fields)})='{{}}'::jsonb AND ({condition})) ELSE FALSE END"


def _int_sql(expression, minimum=0, maximum=_MAX_INT):
    return f"CASE WHEN jsonb_typeof({expression})='number' AND ({expression} #>> '{{}}') ~ '^-?[0-9]+$' THEN ({expression} #>> '{{}}')::numeric BETWEEN {minimum} AND {maximum} ELSE FALSE END"


def _text_sql(expression, maximum=128):
    return f"jsonb_typeof({expression})='string' AND length({expression} #>> '{{}}') BETWEEN 1 AND {maximum} AND ({expression} #>> '{{}}') !~ '[[:cntrl:]]'"


def _regex(expression, pattern):
    return f"jsonb_typeof({expression})='string' AND ({expression} #>> '{{}}') ~ {_literal(pattern)}"


def _hash_sql(expression):
    return _regex(expression, "^[0-9a-f]{64}$")


def _uuid_sql(expression):
    return _regex(
        expression, "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )


def _nullable(expression, valid):
    return f"({expression}='null'::jsonb OR ({valid}))"


_S = "data->'scope'"
_A = "data->'actor'"
_D = "data->'terminal_evidence'->'docker'"
_E = "data->'terminal_evidence'"
_COMMON_SQL = " AND ".join(
    [
        "data->'schema'='1'::jsonb AND data->>'schema'='1'",
        "data->>'operation_id'=id::text",
        "data->'run_id'=COALESCE(to_jsonb(run_id),'null'::jsonb)",
        "data->'parent_action_id'=COALESCE(to_jsonb(parent_action_id),'null'::jsonb)",
        "(parent_action_id IS NULL OR run_id IS NOT NULL)",
        _nullable("data->'project_id'", _uuid_sql("data->'project_id'")),
        _nullable("data->'repair_of'", _uuid_sql("data->'repair_of'")),
        f"data->>'operation_kind'=ANY({_array(KINDS)})",
        _object(
            _A,
            _ACTOR_FIELDS,
            " AND ".join(
                [
                    f"{_A}->>'kind' IN ('worker','api','cli')",
                    _uuid_sql(f"{_A}->'owner'"),
                    _hash_sql(f"{_A}->'instance_nonce'"),
                    _int_sql(f"{_A}->'pid'", 1, 2**31 - 1),
                    _regex(
                        f"{_A}->'instance_started_at'",
                        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?\+00:00$",
                    ),
                    f"make_date(substring({_A}->>'instance_started_at',1,4)::int,substring({_A}->>'instance_started_at',6,2)::int,substring({_A}->>'instance_started_at',9,2)::int) IS NOT NULL",
                ]
            ),
        ),
        _object(
            _S,
            _SCOPE_FIELDS,
            " AND ".join(
                [
                    _hash_sql(f"{_S}->'intent_digest'"),
                    _nullable(
                        f"{_S}->'repository_digest'",
                        _hash_sql(f"{_S}->'repository_digest'"),
                    ),
                    _nullable(
                        f"{_S}->'container_name'",
                        _regex(
                            f"{_S}->'container_name'",
                            "^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$",
                        ),
                    ),
                    _nullable(f"{_S}->'run_label'", _text_sql(f"{_S}->'run_label'")),
                ]
            ),
        ),
        _nullable(
            "data->'expected_run_owner'", _text_sql("data->'expected_run_owner'")
        ),
        "(run_id IS NOT NULL OR data->'expected_run_owner'='null'::jsonb)",
        _int_sql("data->'business_deadline_monotonic_ns'", 1),
        _int_sql("data->'cleanup_allowance_seconds'", 0, 60),
        _int_sql("data->'attempted_processes'", 0, MAX_PROCESSES),
        _int_sql("data->'revision'", 1),
        _hash_sql("data->'previous_record_sha256'"),
        _hash_sql("data->'record_sha256'"),
        "data->>'record_sha256'=encode(sha256(jsonb_send(data-'record_sha256')),'hex')",
        "octet_length(jsonb_send(data))<=65536",
        "(data->'repair_of'='null'::jsonb OR data->>'operation_kind'='sandbox_cleanup')",
        f"(data->>'operation_kind' NOT LIKE 'git_%' OR run_id IS NOT NULL OR data->'project_id'<>'null'::jsonb OR {_S}->'repository_digest'<>'null'::jsonb)",
        f"(data->>'operation_kind' LIKE 'git_%' OR ({_S}->'run_label'<>'null'::jsonb AND (data->>'operation_kind'<>'sandbox_execute' OR {_S}->'container_name'<>'null'::jsonb)))",
    ]
)
_DOCKER_SQL = _object(
    _D,
    _DOCKER_FIELDS,
    " AND ".join(
        [
            f"{_D}->'create_attempted' IN ('true'::jsonb,'false'::jsonb)",
            f"{_D}->'create_acknowledged'={_D}->'create_attempted'",
            f"{_D}->'container_removed' IN ('true'::jsonb,'false'::jsonb)",
            f"{_D}->'observation_complete'='true'::jsonb",
            f"{_D}->'run_label'={_S}->'run_label'",
            _int_sql(f"{_D}->'remaining_containers'", 0, MAX_CONTAINERS),
            f"(data->>'operation_kind'='sandbox_execute' OR {_D}->'create_attempted'='false'::jsonb)",
            f"(data->>'operation_kind'='sandbox_probe' OR ({_D}->'remaining_containers'='0'::jsonb AND ({_D}->'create_attempted'='false'::jsonb OR {_D}->'container_removed'='true'::jsonb)))",
        ]
    ),
)
_EVIDENCE_SQL = _object(
    _E,
    _EVIDENCE_FIELDS,
    " AND ".join(
        [
            f"{_E}->>'outcome' IN ('success','failed')",
            f"{_E}->>'reason' IN ('success','command_failed','cancelled','deadline','operation_failed')",
            f"(({_E}->>'outcome'='success')=({_E}->>'reason'='success'))",
            f"{_E}->'local_process_unknown'='false'::jsonb",
            f"{_E}->'daemon_request_unknown'='false'::jsonb",
            f"((data->>'operation_kind' LIKE 'git_%' AND {_E}->'docker'='null'::jsonb) OR (data->>'operation_kind' NOT LIKE 'git_%' AND ({_DOCKER_SQL})))",
        ]
    ),
)
_PROCESS_SQL = _object(
    "item",
    _PROCESS_FIELDS,
    " AND ".join(
        [
            _int_sql("item->'seq'", 1, MAX_PROCESSES),
            "item->>'seq'=seq::text",
            f"item->>'phase'=ANY({_array(PHASES)})",
            _nullable("item->'target'", _text_sql("item->'target'")),
            _int_sql("item->'pid'", 1, 2**31 - 1),
            _int_sql("item->'exit_code'", -255, 255),
            "item->'reaped'='true'::jsonb",
            "item->'pipe_eof'='true'::jsonb",
            "((data->>'operation_kind' LIKE 'git_%' AND item->>'phase'='git' AND item->'target'='null'::jsonb) OR (data->>'operation_kind' NOT LIKE 'git_%' AND item->>'phase'<>'git'))",
            "(data->'repair_of'='null'::jsonb OR item->>'phase' IN ('docker_ps','docker_rm'))",
            "(data->>'operation_kind'<>'sandbox_probe' OR item->>'phase'='docker_ps')",
            "(data->>'operation_kind'<>'sandbox_cleanup' OR item->>'phase' IN ('docker_ps','docker_rm'))",
            f"(item->>'phase'<>'docker_ps' OR item->'target'={_S}->'run_label')",
            "(item->>'phase'<>'mount_validation' OR item->'target'='null'::jsonb)",
            f"(item->>'phase' NOT IN ('docker_create','docker_start','docker_inspect') OR item->'target'={_S}->'container_name')",
            f"(item->>'phase'<>'docker_rm' OR item->'target'={_S}->'container_name' OR ({_hash_sql("item->'target'")}))",
        ]
    ),
)
_FUNCTION_BODY = f"""
DECLARE item jsonb; seq bigint; creates integer := 0; cleanup_started boolean := FALSE;
BEGIN
  IF NOT COALESCE(({_object("data", _FIELDS, _COMMON_SQL)}),FALSE) THEN RETURN FALSE; END IF;
  IF data->>'state'='not_spawned' THEN
    RETURN COALESCE((data->'spawn_possible'='false'::jsonb AND data->'revision'='1'::jsonb
      AND data->'attempted_processes'='0'::jsonb AND data->'processes'='[]'::jsonb
      AND data->'terminal_evidence'='null'::jsonb AND data->>'reason' IN ('cancelled','deadline','operation_failed')),FALSE);
  END IF;
  IF NOT COALESCE((data->>'state'='terminal' AND data->'spawn_possible'='true'::jsonb
      AND {_int_sql("data->'revision'", 2)} AND data->'reason'='null'::jsonb
      AND {_int_sql("data->'attempted_processes'", 1, MAX_PROCESSES)}
      AND jsonb_typeof(data->'processes')='array' AND ({_EVIDENCE_SQL})),FALSE) THEN RETURN FALSE; END IF;
  IF jsonb_array_length(data->'processes')<>(data->>'attempted_processes')::int THEN RETURN FALSE; END IF;
  FOR item,seq IN SELECT value,ordinality FROM jsonb_array_elements(data->'processes') WITH ORDINALITY LOOP
    IF NOT COALESCE(({_PROCESS_SQL}),FALSE) THEN RETURN FALSE; END IF;
    IF data->>'operation_kind'='sandbox_execute' THEN
      IF cleanup_started AND item->>'phase' NOT IN ('docker_ps','docker_rm') THEN RETURN FALSE; END IF;
      IF item->>'phase' IN ('docker_ps','docker_rm') THEN cleanup_started := TRUE; END IF;
    END IF;
    IF item->>'phase'='docker_create' THEN creates := creates+1; END IF;
  END LOOP;
  IF data->>'operation_kind' NOT LIKE 'git_%' AND (creates>1 OR data->'terminal_evidence'->'docker'->'create_attempted'<>to_jsonb(creates=1)) THEN RETURN FALSE; END IF;
  RETURN TRUE;
EXCEPTION WHEN OTHERS THEN RETURN FALSE;
END
"""


def initialize(connection):
    """Create the narrow schema; reject function/index drift explicitly."""
    connection.execute("""CREATE TABLE IF NOT EXISTS controller_operations (
        id uuid PRIMARY KEY, run_id uuid REFERENCES runs(id),
        parent_action_id uuid REFERENCES actions(id), data jsonb NOT NULL)""")
    columns = connection.execute(
        "SELECT attname,atttypid::int AS type_id,attnotnull FROM pg_attribute WHERE attrelid='controller_operations'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum"
    ).fetchall()
    expected_columns = [
        {"attname": name, "type_id": type_id, "attnotnull": required}
        for name, type_id, required in [
            ("id", 2950, True),
            ("run_id", 2950, False),
            ("parent_action_id", 2950, False),
            ("data", 3802, True),
        ]
    ]
    constraints = connection.execute(
        "SELECT pg_get_constraintdef(oid) AS definition,convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid='controller_operations'::regclass AND contype<>'n' ORDER BY pg_get_constraintdef(oid)"
    ).fetchall()
    expected_constraints = [
        {
            "definition": definition,
            "convalidated": True,
            "condeferrable": False,
            "condeferred": False,
        }
        for definition in [
            "FOREIGN KEY (parent_action_id) REFERENCES actions(id)",
            "FOREIGN KEY (run_id) REFERENCES runs(id)",
            "PRIMARY KEY (id)",
        ]
    ]
    if columns != expected_columns or constraints != expected_constraints:
        raise RegistryError("schema_drift")
    existing = connection.execute(
        "SELECT p.prosrc,p.provolatile,p.proisstrict,p.prosecdef,p.proconfig,l.lanname,p.prorettype='boolean'::regtype AS returns_bool FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname=current_schema() AND p.proname=%s AND p.proargtypes='2950 2950 2950 3802'::oidvector",
        (_FUNCTION,),
    ).fetchone()
    if existing is None:
        connection.execute(
            sql.SQL(
                "CREATE FUNCTION {}(id uuid,run_id uuid,parent_action_id uuid,data jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS {} "
            ).format(sql.Identifier(_FUNCTION), sql.Literal(_FUNCTION_BODY))
        )
    elif existing != {
        "prosrc": _FUNCTION_BODY,
        "provolatile": "i",
        "proisstrict": False,
        "prosecdef": False,
        "proconfig": ["search_path=pg_catalog"],
        "lanname": "plpgsql",
        "returns_bool": True,
    }:
        raise RegistryError("function_drift")
    temporary = "controller_index_check_" + uuid4().hex
    connection.execute(
        sql.SQL(
            "CREATE TEMP TABLE {} (LIKE controller_operations) ON COMMIT DROP"
        ).format(sql.Identifier(temporary))
    )
    reference = temporary + "_idx"
    definition = f"(id) WHERE {FENCE_PREDICATE}"
    connection.execute(
        sql.SQL("CREATE INDEX {} ON {} " + definition).format(
            sql.Identifier(reference), sql.Identifier(temporary)
        )
    )
    expected = connection.execute(
        "SELECT pg_get_indexdef(%s::regclass) AS definition", ("pg_temp." + reference,)
    ).fetchone()["definition"]
    connection.execute(
        sql.SQL(
            "CREATE INDEX IF NOT EXISTS {} ON controller_operations " + definition
        ).format(sql.Identifier(_INDEX))
    )
    actual = connection.execute(
        "SELECT i.indrelid='controller_operations'::regclass AS correct_table,i.indisvalid,i.indisready,i.indislive,i.indisunique,i.indisexclusion,pg_get_indexdef(i.indexrelid) AS definition FROM pg_class c LEFT JOIN pg_index i ON i.indexrelid=c.oid WHERE c.relnamespace=(SELECT relnamespace FROM pg_class WHERE oid='controller_operations'::regclass) AND c.relname=%s",
        (_INDEX,),
    ).fetchone()
    if (
        not actual
        or not all(
            actual[k]
            for k in ("correct_table", "indisvalid", "indisready", "indislive")
        )
        or actual["indisunique"]
        or actual["indisexclusion"]
        or actual["definition"].split(" USING ", 1)[1]
        != expected.split(" USING ", 1)[1]
    ):
        raise RegistryError("index_drift")


def _fenced(connection, operation_id=None):
    # This private exclusion is used only by an exact live group continuation.
    suffix = " AND id<>%s" if operation_id is not None else ""
    return bool(
        connection.execute(
            f"SELECT id FROM controller_operations WHERE {FENCE_PREDICATE}{suffix} ORDER BY id LIMIT 1",
            (operation_id,) if operation_id is not None else (),
        ).fetchone()
    )


def fenced(connection):
    """No Run join or ordinary action status participates in this fence."""
    return _fenced(connection)


def list_unresolved(connection, *, limit=100, after_id=None):
    _integer(limit, 1, 100)
    if after_id is not None:
        _uuid(after_id)
    suffix = " AND id>%s" if after_id is not None else ""
    rows = connection.execute(
        f"SELECT id,run_id,parent_action_id FROM controller_operations WHERE {FENCE_PREDICATE}{suffix} ORDER BY id LIMIT %s",
        (after_id, limit + 1) if after_id is not None else (limit + 1,),
    ).fetchall()
    items = [
        {k: str(v) if v is not None else None for k, v in row.items()}
        for row in rows[:limit]
    ]
    return {
        "items": items,
        "next_cursor": items[-1]["id"] if len(rows) > limit else None,
    }


def _digest(connection, record):
    return connection.execute(
        "SELECT encode(sha256(jsonb_send(%s::jsonb-'record_sha256')),'hex') AS hash",
        (Jsonb(record),),
    ).fetchone()["hash"]


def _bound(connection, row):
    if row is None:
        raise RegistryError("record_missing")
    record = row["data"]
    _validate_record(record)
    if (
        record["operation_id"] != str(row["id"])
        or record["run_id"]
        != (str(row["run_id"]) if row["run_id"] is not None else None)
        or record["parent_action_id"]
        != (
            str(row["parent_action_id"])
            if row["parent_action_id"] is not None
            else None
        )
        or _digest(connection, record) != record["record_sha256"]
    ):
        raise RegistryError("record_binding")
    return record


class ControllerRegistry:
    def __init__(self, store, *, actor, check_owner, check_other_fences):
        if (
            type(actor) is not Actor
            or not callable(check_owner)
            or not callable(check_other_fences)
        ):
            raise RegistryError("registry_arguments")
        actor.check_live()
        self.store, self.actor = store, actor
        self.check_owner, self.check_other_fences = check_owner, check_other_fences
        self._uncertain = False
        self._closed = False

    def _live(self):
        if self._closed:
            raise RegistryError("actor_closed")
        self.actor.check_live()
        self.check_owner()

    def close(self):
        """Expire this request/worker context without resolving any group."""
        self._closed = True

    def _run(self, connection, record):
        if record["run_id"] is not None:
            run = connection.execute(
                "SELECT data FROM runs WHERE id=%s FOR UPDATE", (record["run_id"],)
            ).fetchone()
            if run is None or run["data"].get("owner") != record["expected_run_owner"]:
                raise OwnershipLost("控制操作对应的执行权已变化")
            request = run["data"].get("request")
            if (
                type(request) is not dict
                or request.get("project_id") != record["project_id"]
            ):
                raise RegistryError("project_binding")
        if record["parent_action_id"] is not None:
            action = connection.execute(
                "SELECT run_id FROM actions WHERE id=%s", (record["parent_action_id"],)
            ).fetchone()
            if action is None or str(action["run_id"]) != record["run_id"]:
                raise RegistryError("action_binding")

    def reserve(
        self,
        kind,
        *,
        scope,
        deadline_monotonic_ns,
        run_id=None,
        project_id=None,
        parent_action_id=None,
        expected_run_owner=None,
        cleanup_allowance_seconds=15,
    ):
        return self._reserve(
            kind,
            scope=scope,
            deadline_monotonic_ns=deadline_monotonic_ns,
            run_id=run_id,
            project_id=project_id,
            parent_action_id=parent_action_id,
            expected_run_owner=expected_run_owner,
            cleanup_allowance_seconds=cleanup_allowance_seconds,
            repair_of=None,
        )

    def repair_only(
        self, original_operation_id, *, deadline_monotonic_ns, expected_run_owner=None
    ):
        """New ps/rm-only group; never changes or resolves the original group."""
        _uuid(original_operation_id)
        self._live()
        with self.store.connect() as connection:
            original = _bound(
                connection,
                connection.execute(
                    "SELECT * FROM controller_operations WHERE id=%s",
                    (original_operation_id,),
                ).fetchone(),
            )
        if (
            original["operation_kind"]
            not in {"sandbox_execute", "sandbox_probe", "sandbox_cleanup"}
            or original["scope"]["run_label"] is None
        ):
            raise RegistryError("repair_scope")
        return self._reserve(
            "sandbox_cleanup",
            scope=original["scope"],
            deadline_monotonic_ns=deadline_monotonic_ns,
            run_id=original["run_id"],
            project_id=original["project_id"],
            parent_action_id=None,
            expected_run_owner=expected_run_owner,
            cleanup_allowance_seconds=15,
            repair_of=original_operation_id,
        )

    def _reserve(self, kind, **arguments):
        self._live()
        if self._uncertain:
            raise RegistryError("commit_unknown")
        record = dict(
            schema=1,
            operation_id=str(uuid4()),
            operation_kind=kind,
            actor=self.actor.data(),
            state="reserved",
            revision=0,
            previous_record_sha256=None,
            spawn_possible=False,
            attempted_processes=0,
            processes=[],
            terminal_evidence=None,
            reason=None,
            record_sha256="0" * 64,
            **copy.deepcopy(arguments),
        )
        record["business_deadline_monotonic_ns"] = record.pop("deadline_monotonic_ns")
        _validate_record(record)
        if record["business_deadline_monotonic_ns"] <= time.monotonic_ns():
            raise RegistryError("deadline")
        if kind.startswith("sandbox_") and (
            record["scope"]["run_label"] is None
            or (kind == "sandbox_execute" and record["scope"]["container_name"] is None)
        ):
            raise RegistryError("sandbox_scope")
        if (
            kind.startswith("git_")
            and record["run_id"] is None
            and record["project_id"] is None
            and record["scope"]["repository_digest"] is None
        ):
            raise RegistryError("source_scope")
        if (
            self.actor.kind == "worker"
            and record["run_id"] is not None
            and record["expected_run_owner"] is None
        ):
            raise RegistryError("run_owner_missing")
        if record["repair_of"] is not None and kind != "sandbox_cleanup":
            raise RegistryError("repair_scope")
        try:
            with self.store.connect() as connection:
                dispatch_lock(connection)
                self._live()
                self._run(connection, record)
                if record["repair_of"] is not None:
                    original = _bound(
                        connection,
                        connection.execute(
                            "SELECT * FROM controller_operations WHERE id=%s FOR UPDATE",
                            (record["repair_of"],),
                        ).fetchone(),
                    )
                    if (
                        original["state"] not in {"reserved", "launching", "unknown"}
                        or original["scope"] != record["scope"]
                        or original["run_id"] != record["run_id"]
                    ):
                        raise RegistryError("repair_scope")
                if record["repair_of"] is None and (
                    self.check_other_fences(connection) or fenced(connection)
                ):
                    raise RegistryError("fenced")
                record["record_sha256"] = _digest(connection, record)
                connection.execute(
                    "INSERT INTO controller_operations(id,run_id,parent_action_id,data) VALUES(%s,%s,%s,%s)",
                    (
                        record["operation_id"],
                        record["run_id"],
                        record["parent_action_id"],
                        Jsonb(record),
                    ),
                )
        except (RegistryError, OwnershipLost):
            raise
        except BaseException as error:
            self._uncertain = True
            raise RegistryError(
                "reserve_commit_unknown", operation_id=record["operation_id"]
            ) from error
        return Operation(self, record, _LIVE)


@dataclass(frozen=True)
class ProcessPermit:
    operation_id: str
    seq: int
    _seal: object = field(repr=False, compare=False)


class Operation:
    def __init__(self, registry, record, seal):
        if seal is not _LIVE:
            raise RegistryError("capability")
        self.registry, self._record = registry, copy.deepcopy(record)
        self._capability, self._launch_attempted = None, False
        self._pending_terminal = None
        self._mutex = threading.Lock()

    @property
    def operation_id(self):
        return self._record["operation_id"]

    @property
    def record(self):
        return copy.deepcopy(self._record)

    def _locked(self, connection):
        dispatch_lock(connection)
        self.registry._live()
        self.registry._run(connection, self._record)
        current = _bound(
            connection,
            connection.execute(
                "SELECT * FROM controller_operations WHERE id=%s FOR UPDATE",
                (self.operation_id,),
            ).fetchone(),
        )
        if (
            current["actor"] != self.registry.actor.data()
            or current["scope"] != self._record["scope"]
            or current["repair_of"] != self._record["repair_of"]
        ):
            raise RegistryError("actor_binding")
        return current

    def _transition(self, state, *, processes=None, evidence=None, reason=None):
        previous = self._record
        proposed = {
            **copy.deepcopy(previous),
            "state": state,
            "revision": previous["revision"] + 1,
            "previous_record_sha256": previous["record_sha256"],
            "reason": reason,
            "terminal_evidence": copy.deepcopy(evidence),
        }
        if state == "launching":
            proposed["spawn_possible"] = True
        if processes is not None:
            proposed.update(
                processes=copy.deepcopy(processes), attempted_processes=len(processes)
            )
        try:
            with self.registry.store.connect() as connection:
                current = self._locked(connection)
                proposed["record_sha256"] = _digest(connection, proposed)
                _validate_record(proposed)
                if (
                    self._pending_terminal is not None
                    and proposed != self._pending_terminal
                ):
                    raise RegistryError("receipt_changed")
                # A lost terminal commit ACK can only repeat this exact receipt.
                if state == "terminal" and current == proposed:
                    pass
                else:
                    if current != previous:
                        raise RegistryError("cas_conflict")
                    if (
                        state == "launching"
                        and proposed["repair_of"] is None
                        and (
                            self.registry.check_other_fences(connection)
                            or _fenced(connection, self.operation_id)
                        )
                    ):
                        raise RegistryError("fenced")
                    changed = connection.execute(
                        "UPDATE controller_operations SET data=%s WHERE id=%s AND data->>'state'=%s AND data->>'record_sha256'=%s AND data->>'revision'=%s",
                        (
                            Jsonb(proposed),
                            self.operation_id,
                            previous["state"],
                            previous["record_sha256"],
                            str(previous["revision"]),
                        ),
                    ).rowcount
                    if changed != 1:
                        raise RegistryError("cas_conflict")
        except (RegistryError, OwnershipLost):
            raise
        except BaseException as error:
            self.registry._uncertain = True
            if state == "terminal":
                self._pending_terminal = proposed
            raise RegistryError(
                "transition_commit_unknown", operation_id=self.operation_id
            ) from error
        self._record = proposed
        return self.record

    @_serialized
    def launch(self):
        if (
            self._launch_attempted
            or self._record["state"] != "reserved"
            or self.registry._uncertain
        ):
            raise RegistryError("launch_used")
        self._launch_attempted = True
        if time.monotonic_ns() >= self._record["business_deadline_monotonic_ns"]:
            raise RegistryError("deadline")
        self._transition("launching")
        self.registry._live()
        if time.monotonic_ns() >= self._record["business_deadline_monotonic_ns"]:
            raise RegistryError("deadline", operation_id=self.operation_id)
        self._capability = LaunchCapability(self, _LIVE)
        return self._capability

    @_serialized
    def not_spawned(self, reason="cancelled"):
        if self._launch_attempted or self._record["state"] != "reserved":
            raise RegistryError("spawn_possible")
        if reason not in {"cancelled", "deadline", "operation_failed"}:
            raise RegistryError("reason")
        return self._transition("not_spawned", reason=reason)

    @_serialized
    def mark_unknown(self, reason="interrupted"):
        if self._record["state"] not in {"reserved", "launching"}:
            raise RegistryError("state")
        return self._transition(
            "unknown",
            reason=reason,
            processes=self._capability._processes if self._capability else [],
        )

    @_serialized
    def finish(self, evidence):
        if self._record["state"] != "launching" or self._capability is None:
            raise RegistryError("state")
        return self._transition(
            "terminal", evidence=evidence, processes=self._capability._processes
        )


class LaunchCapability:
    def __init__(self, operation, seal):
        if seal is not _LIVE:
            raise RegistryError("capability")
        self._operation, self._seal = operation, object()
        self._processes, self._observed = [], set()
        self._receipted, self._observations = set(), set()
        self._cleanup_deadline_ns = None
        self._mutex = operation._mutex

    @_serialized
    def observe_containers(self, permit, ids, *, run_label, complete):
        self._operation.registry._live()
        if (
            type(permit) is not ProcessPermit
            or permit._seal is not self._seal
            or permit.operation_id != self._operation.operation_id
            or permit.seq not in self._receipted
            or permit.seq in self._observations
        ):
            raise RegistryError("process_permit")
        receipt = self._processes[permit.seq - 1]
        if (
            receipt["phase"] != "docker_ps"
            or not receipt["reaped"]
            or not receipt["pipe_eof"]
            or receipt["exit_code"] != 0
        ):
            raise RegistryError("container_observation")
        if (
            type(ids) is not list
            or len(ids) > MAX_CONTAINERS
            or complete is not True
            or run_label != self._operation._record["scope"]["run_label"]
        ):
            raise RegistryError("container_observation")
        for value in ids:
            _hash(value)
        if len(set(ids)) != len(ids) or len(self._observed | set(ids)) > MAX_CONTAINERS:
            raise RegistryError("container_observation")
        self._observed.update(ids)
        self._observations.add(permit.seq)

    @_serialized
    def before_process(self, phase, *, target=None):
        operation, registry = self._operation, self._operation.registry
        registry._live()
        record, bound = operation._record, operation._record["scope"]
        if (
            operation._capability is not self
            or record["state"] != "launching"
            or registry._uncertain
            or phase not in PHASES
        ):
            raise RegistryError("capability")
        git = record["operation_kind"].startswith("git_")
        if git != (phase == "git") or (
            record["repair_of"] is not None and phase not in _CLEANUP_PHASES
        ):
            raise RegistryError("phase")
        if phase == "docker_create" and any(
            item["phase"] == "docker_create" for item in self._processes
        ):
            raise RegistryError("phase_used")
        if (
            record["operation_kind"] == "sandbox_probe"
            and phase != "docker_ps"
            or record["operation_kind"] == "sandbox_cleanup"
            and phase not in _CLEANUP_PHASES
        ):
            raise RegistryError("phase")
        if phase in {"git", "mount_validation"} and target is not None:
            raise RegistryError("scope")
        if phase == "docker_ps" and target != bound["run_label"]:
            raise RegistryError("scope")
        if (
            phase in {"docker_create", "docker_start", "docker_inspect"}
            and target != bound["container_name"]
        ):
            raise RegistryError("scope")
        if phase == "docker_rm" and (
            target is None
            or target != bound["container_name"]
            and target not in self._observed
        ):
            raise RegistryError("scope")
        now = time.monotonic_ns()
        cleanup = (
            phase in _CLEANUP_PHASES and record["operation_kind"] != "sandbox_probe"
        )
        if not cleanup and self._cleanup_deadline_ns is not None:
            raise RegistryError("cleanup_started")
        if cleanup and self._cleanup_deadline_ns is None:
            self._cleanup_deadline_ns = (
                min(now, record["business_deadline_monotonic_ns"])
                + record["cleanup_allowance_seconds"] * 1_000_000_000
            )
        if (
            cleanup
            and now >= self._cleanup_deadline_ns
            or not cleanup
            and now >= record["business_deadline_monotonic_ns"]
        ):
            raise RegistryError("deadline")
        if not cleanup and any(
            not item["reaped"] or not item["pipe_eof"] for item in self._processes
        ):
            raise RegistryError("previous_process_unknown")
        if len(self._processes) >= MAX_PROCESSES:
            raise RegistryError("process_limit")
        with registry.store.connect() as connection:
            current = operation._locked(connection)
            if current != record:
                raise RegistryError("cas_conflict")
            if not cleanup and (
                registry.check_other_fences(connection)
                or _fenced(connection, operation.operation_id)
            ):
                raise RegistryError("fenced")
        registry._live()
        now = time.monotonic_ns()
        if (
            cleanup
            and now >= self._cleanup_deadline_ns
            or not cleanup
            and now >= record["business_deadline_monotonic_ns"]
        ):
            raise RegistryError("deadline", operation_id=operation.operation_id)
        item = {
            "seq": len(self._processes) + 1,
            "phase": phase,
            "target": target,
            "pid": None,
            "exit_code": None,
            "reaped": False,
            "pipe_eof": False,
        }
        self._processes.append(item)
        return ProcessPermit(operation.operation_id, item["seq"], self._seal)

    @_serialized
    def after_process(self, permit, *, pid, exit_code, reaped, pipe_eof):
        self._operation.registry.actor.check_live()
        if (
            type(permit) is not ProcessPermit
            or permit._seal is not self._seal
            or permit.operation_id != self._operation.operation_id
            or not 1 <= permit.seq <= len(self._processes)
        ):
            raise RegistryError("process_permit")
        previous = self._processes[permit.seq - 1]
        if permit.seq in self._receipted:
            raise RegistryError("receipt_used")
        item = {
            **previous,
            "pid": pid,
            "exit_code": exit_code,
            "reaped": reaped,
            "pipe_eof": pipe_eof,
        }
        _validate_process(item, permit.seq)
        self._processes[permit.seq - 1] = item
        self._receipted.add(permit.seq)


@contextmanager
def bind_registry(registry):
    """Bind an already-authorized server registry; never load private config."""
    if type(registry) is not ControllerRegistry:
        raise RegistryError("registry_arguments")
    registry._live()
    token = _CURRENT.set(registry)
    try:
        yield registry
    finally:
        _CURRENT.reset(token)
        registry.close()


def current_registry():
    registry = _CURRENT.get()
    if registry is None:
        raise RegistryError("registry_missing")
    registry._live()
    return registry


def register_legacy_unknown(connection, *, action_id):
    """Explicit migration primitive, not an automatic historical safety policy.

    The deployment owner chooses the audited action census. No old actor, PID,
    reap receipt or launch time is invented, and ordinary acknowledgement has no
    effect on the resulting independent permanent fence.
    """
    _uuid(action_id)
    dispatch_lock(connection)
    action = connection.execute(
        "SELECT run_id FROM actions WHERE id=%s", (action_id,)
    ).fetchone()
    if action is None:
        raise RegistryError("action_binding")
    run_id = str(action["run_id"])
    connection.execute(
        "SELECT id FROM runs WHERE id=%s FOR UPDATE", (run_id,)
    ).fetchone()
    operation_id = str(uuid5(UUID(action_id), "doonex-legacy-controller-v1"))
    existing = connection.execute(
        "SELECT * FROM controller_operations WHERE id=%s FOR UPDATE", (operation_id,)
    ).fetchone()
    if existing is not None:
        record = _bound(connection, existing)
        if (
            record["operation_kind"] != "legacy_unidentified_controller"
            or record["parent_action_id"] != action_id
        ):
            raise RegistryError("legacy_binding")
        return copy.deepcopy(record)
    import hashlib

    record = {
        "schema": 1,
        "operation_id": operation_id,
        "operation_kind": "legacy_unidentified_controller",
        "run_id": run_id,
        "project_id": None,
        "parent_action_id": action_id,
        "actor": None,
        "expected_run_owner": None,
        "scope": scope(
            intent_digest=hashlib.sha256(action_id.encode("ascii")).hexdigest()
        ),
        "state": "unknown",
        "revision": 0,
        "previous_record_sha256": None,
        "business_deadline_monotonic_ns": None,
        "cleanup_allowance_seconds": 0,
        "spawn_possible": True,
        "attempted_processes": 0,
        "processes": [],
        "terminal_evidence": None,
        "reason": "legacy_unidentified",
        "repair_of": None,
        "record_sha256": "0" * 64,
    }
    record["record_sha256"] = _digest(connection, record)
    _validate_record(record)
    connection.execute(
        "INSERT INTO controller_operations(id,run_id,parent_action_id,data) VALUES(%s,%s,%s,%s)",
        (operation_id, run_id, action_id, Jsonb(record)),
    )
    return copy.deepcopy(record)
