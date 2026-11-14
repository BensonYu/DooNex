"""PostgreSQL is the authority for run state, dispatch intent and budget."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from doonex.models import model_for_role, run_roles


def now():
    return datetime.now(UTC).isoformat()


class OwnershipLost(RuntimeError):
    """A former executor must not mutate the new executor's run."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
 id uuid PRIMARY KEY, path text UNIQUE NOT NULL, data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS library_versions (
 kind text NOT NULL, id text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(kind,id)
);
CREATE TABLE IF NOT EXISTS skill_discoveries (
 id uuid PRIMARY KEY, data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS arena_evaluations (
 id uuid PRIMARY KEY, state text NOT NULL, data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS arena_recommendations (
 id uuid PRIMARY KEY, data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS arena_current (
 fingerprint text NOT NULL, position text NOT NULL, recommendation_id uuid NOT NULL
 REFERENCES arena_recommendations(id), PRIMARY KEY(fingerprint,position)
);
CREATE TABLE IF NOT EXISTS runs (
 id uuid PRIMARY KEY, state text NOT NULL, data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 run_id uuid NOT NULL REFERENCES runs(id), kind text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
CREATE TABLE IF NOT EXISTS actions (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(id),
 kind text NOT NULL, status text NOT NULL, data jsonb NOT NULL
);
"""


DISPATCH_LOCK = 4142061110
CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 15_000
LOCK_TIMEOUT_MS = 5_000
RUN_STATES = frozenset(
    {
        "queued",
        "running",
        "verifying",
        "pausing",
        "paused",
        "interrupted",
        "yielded",
        "completed_verified",
        "completed_unverified",
        "failed",
        "cancelled",
        "budget_exceeded",
    }
)

TERMINATION_UNKNOWN = (
    "COALESCE(jsonb_array_length(data->'recovery'->'remaining'),0)>0 OR "
    "COALESCE(jsonb_array_length(data->'recovery'->'errors'),0)>0"
)
# Keep SQL predicates literal: generic prepared plans must be able to prove the
# partial-index predicates, including empty safety queries.
SCHEDULING_INDEXES = {
    "runs_queued_order_v1": (
        "runs",
        (
            "((CASE WHEN data->'request' ? 'background' THEN 2 "
            "WHEN data->'request' ? 'arena' THEN 1 ELSE 0 END),"
            "(data->>'created_at'),id) WHERE state='queued'"
        ),
    ),
    "arena_active_text_v1": (
        "arena_evaluations",
        "((id::text)) WHERE state IN ('queued','running')",
    ),
    "runs_termination_unknown_v1": ("runs", f"(id) WHERE {TERMINATION_UNKNOWN}"),
    "runs_arena_text_v1": (
        "runs",
        (
            "((data->'request'->'arena'->>'evaluation_id'),id) "
            "WHERE data->'request'->'arena'->>'evaluation_id' IS NOT NULL"
        ),
    ),
    "runs_state_order_v1": ("runs", "(state,(data->>'created_at') DESC,id DESC)"),
    "arena_state_order_v1": (
        "arena_evaluations",
        "(state,(data->>'created_at') DESC,id)",
    ),
    "actions_unresolved_run_v1": (
        "actions",
        "(run_id) WHERE status IN ('pending','unknown','acknowledged')",
    ),
    "allocations_day_v1": ("background_allocations", "(day,id)"),
    "allocations_reserved_v1": (
        "background_allocations",
        "(id) INCLUDE(evaluation_id) WHERE state='reserved'",
    ),
    "authorizations_enabled_order_v1": (
        "background_authorizations",
        "((COALESCE(data->>'created_at','')),id) WHERE state='enabled'",
    ),
}


def ensure_scheduling_indexes(connection):
    """Build missing indexes, but reject drift instead of silently replacing it.

    Ask this PostgreSQL parser to canonicalize the expected definition on empty
    temporary copies. Comparing the complete USING clause also checks opclasses,
    collations, ordering, predicates and INCLUDE columns, without relying on
    pretty-print spelling across PostgreSQL versions or search paths.
    """
    prefix = "doonex_index_check_" + uuid.uuid4().hex[:12]
    tables = {}
    for number, (name, (table, definition)) in enumerate(SCHEDULING_INDEXES.items()):
        if table not in tables:
            tables[table] = f"{prefix}_t{len(tables)}"
            connection.execute(
                sql.SQL("CREATE TEMP TABLE {} (LIKE {}) ON COMMIT DROP").format(
                    sql.Identifier(tables[table]), sql.Identifier(table)
                )
            )
        reference = f"{prefix}_i{number}"
        connection.execute(
            sql.SQL("CREATE INDEX {} ON {} " + definition).format(
                sql.Identifier(reference), sql.Identifier(tables[table])
            )
        )
        expected = connection.execute(
            "SELECT pg_get_indexdef(%s::regclass) AS definition",
            ("pg_temp." + reference,),
        ).fetchone()["definition"]
        connection.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} " + definition).format(
                sql.Identifier(name), sql.Identifier(table)
            )
        )
        actual = connection.execute(
            "SELECT i.indrelid=%s::regclass AS correct_table,"
            "i.indisvalid,i.indisready,i.indislive,i.indisunique,i.indisexclusion,"
            "pg_get_indexdef(i.indexrelid) AS definition "
            "FROM pg_class c LEFT JOIN pg_index i ON i.indexrelid=c.oid "
            "WHERE c.relnamespace=(SELECT relnamespace FROM pg_class "
            "WHERE oid=%s::regclass) AND c.relname=%s",
            (table, table, name),
        ).fetchone()
        if (
            not actual
            or not all(
                actual[key]
                for key in ("correct_table", "indisvalid", "indisready", "indislive")
            )
            or actual["indisunique"]
            or actual["indisexclusion"]
            or actual["definition"].split(" USING ", 1)[1]
            != expected.split(" USING ", 1)[1]
        ):
            raise RuntimeError(f"调度索引 {name} 定义或有效性不符；须显式迁移")


def dispatch_lock(connection):
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (DISPATCH_LOCK,))


def termination_unknown(connection):
    return bool(
        connection.execute(
            f"SELECT 1 FROM runs WHERE {TERMINATION_UNKNOWN} ORDER BY id LIMIT 1"
        ).fetchone()
    )


def external_execution_fenced(connection):
    """Fences outside the controller registry, also checked by live groups."""
    from doonex.responses_registry import helper_fenced

    return termination_unknown(connection) or helper_fenced(connection)


def execution_fenced(connection):
    """Every unresolved execution record blocks public admission globally."""
    from doonex.controller_operations import fenced

    return external_execution_fenced(connection) or fenced(connection)


class Store:
    def __init__(self, database_url):
        self.database_url = database_url

    def connect(self):
        # Let libpq resolve DSN, environment and service-file options first;
        # replacing startup options could discard schema or read-only settings.
        connection = psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
        try:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, false), "
                    "set_config('lock_timeout', %s, false)",
                    (str(STATEMENT_TIMEOUT_MS), str(LOCK_TIMEOUT_MS)),
                )
        except BaseException:
            connection.close()
            raise
        # These are server-side statement/lock limits after setup, not an
        # end-to-end scheduling or failed-host/network guarantee.
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (4142061107,))
            connection.execute(SCHEMA)
            from doonex import controller_operations

            controller_operations.initialize(connection)
            from doonex.background import BACKGROUND_SCHEMA

            connection.execute(BACKGROUND_SCHEMA)
            from doonex.generation import GENERATION_SCHEMA

            connection.execute(GENERATION_SCHEMA)
            from doonex.community import COMMUNITY_SCHEMA

            connection.execute(COMMUNITY_SCHEMA)
            from doonex.community_evidence import COMMUNITY_EVIDENCE_SCHEMA

            connection.execute(COMMUNITY_EVIDENCE_SCHEMA)
            from doonex.responses_registry import HELPER_REGISTRY_SCHEMA

            connection.execute(HELPER_REGISTRY_SCHEMA)
            ensure_scheduling_indexes(connection)
            # Keep the M1 source row intact. Repeated startup never overwrites
            # updated M2 metadata, identities, runs or their private artifacts.
            connection.execute(
                """INSERT INTO projects(id,path,data)
                SELECT (data->>'id')::uuid,data->>'path',data
                FROM project WHERE singleton ON CONFLICT DO NOTHING"""
            )

    def project(self, project_id=None):
        with self.connect() as c:
            if project_id is not None:
                row = c.execute(
                    "SELECT data FROM projects WHERE id=%s", (project_id,)
                ).fetchone()
                if not row:
                    raise KeyError("找不到项目")
                return row["data"]
            rows = c.execute("SELECT data FROM projects LIMIT 2").fetchall()
        if len(rows) > 1:
            raise ValueError("存在多个项目，请明确选择项目")
        return rows[0]["data"] if rows else None

    def list_projects(self):
        with self.connect() as c:
            return [
                row["data"]
                for row in c.execute("SELECT data FROM projects ORDER BY path,id")
            ]

    def set_project(self, data):
        with self.connect() as c:
            c.execute("LOCK TABLE projects IN EXCLUSIVE MODE")
            existing = c.execute(
                "SELECT data FROM projects WHERE path=%s", (data["path"],)
            ).fetchone()
            data = {
                **data,
                "id": existing["data"]["id"] if existing else data["id"],
            }
            c.execute(
                """INSERT INTO projects(id,path,data) VALUES(%s,%s,%s)
                ON CONFLICT(path) DO UPDATE SET data=EXCLUDED.data""",
                (data["id"], data["path"], Jsonb(data)),
            )
        return data

    def create_run(self, request, connection=None):
        run_id = str(uuid.uuid4())
        roles = run_roles(request)

        def initial_usage(subscription):
            return {
                "calls": 0,
                "tokens": 0,
                "cost_usd": None if subscription else 0.0,
                "reserved_tokens": 0,
                "reserved_cost_usd": None if subscription else 0.0,
                "metered_cost_usd": 0.0,
                "reserved_metered_cost_usd": 0.0,
                "unknown_calls": 0,
            }

        data = {
            "id": run_id,
            "state": "queued",
            "reason": "等待执行",
            "created_at": now(),
            "updated_at": now(),
            "request": request,
            "usage": initial_usage(
                any(
                    model_for_role(request, role).get("billing_mode") == "subscription"
                    for role in roles
                )
            ),
            "first_started_at": None,
            "evidence": None,
            "phase": "execution",
            "active_assignment": "executor",
            "review": None,
        }
        data["assignments"] = {
            role: {
                "id": str(uuid.uuid4()),
                "role": role,
                "parent_run_id": run_id,
                "state": "pending",
                "started_at": None,
                "ended_at": None,
                "session_generation": 0,
                "resume_session": False,
                "usage": initial_usage(
                    model_for_role(request, role).get("billing_mode") == "subscription"
                ),
            }
            for role in roles
        }

        def insert(c):
            dispatch_lock(c)
            c.execute(
                "INSERT INTO runs VALUES(%s,%s,%s)", (run_id, "queued", Jsonb(data))
            )
            self.event(run_id, "queued", {"acceptance_version": 1}, connection=c)

        if connection is not None:
            insert(connection)
        else:
            with self.connect() as c:
                insert(c)
        return data

    def get(self, run_id):
        with self.connect() as c:
            row = c.execute("SELECT data FROM runs WHERE id=%s", (run_id,)).fetchone()
        if not row:
            raise KeyError("找不到运行")
        return row["data"]

    def next_queued_run(self):
        """Read one candidate; owner checks and dispatch admission still apply."""
        with self.connect() as c:
            row = c.execute(
                """SELECT r.data FROM runs r
                WHERE r.state='queued'
                AND (NOT (r.data->'request' ? 'arena') OR EXISTS (
                    SELECT 1 FROM arena_evaluations a
                    WHERE a.id::text=r.data->'request'->'arena'->>'evaluation_id'
                    AND a.state IN ('queued','running')
                ))
                ORDER BY
                    CASE
                        WHEN r.data->'request' ? 'background' THEN 2
                        WHEN r.data->'request' ? 'arena' THEN 1
                        ELSE 0
                    END,
                    r.data->>'created_at', r.id
                LIMIT 1"""
            ).fetchone()
        return row["data"] if row else None

    def scheduling_runs(self, *, states, capacity, deadline):
        """Read the complete bounded safety census, never a truncated prefix.

        The state-first ordering uses runs_state_order_v1. Column/payload
        disagreement cannot turn a damaged row into execution authority.
        """
        from doonex.deadline import check_deadline

        check_deadline(deadline)
        if type(capacity) is not int or capacity < 1:
            raise ValueError("调度核查容量无效")
        if not isinstance(states, (set, frozenset)) or not states <= RUN_STATES:
            raise ValueError("调度核查状态无效")
        if not states:
            return []
        with self.connect() as c:
            check_deadline(deadline)
            rows = c.execute(
                "SELECT id,state,data FROM runs WHERE state IN ("
                + ",".join("'" + state + "'" for state in sorted(states))
                + ") ORDER BY state,data->>'created_at' DESC,id DESC LIMIT %s",
                (capacity + 1,),
            ).fetchall()
            check_deadline(deadline)
        if len(rows) > capacity:
            raise ValueError("调度核查容量超限；保留全部运行并停止新派发")
        for row in rows:
            if (
                not isinstance(row["data"], dict)
                or row["data"].get("id") != str(row["id"])
                or row["data"].get("state") != row["state"]
            ):
                raise ValueError("调度运行标识或状态绑定无效；停止新派发")
        check_deadline(deadline)
        return [row["data"] for row in rows]

    def list_runs(self, limit=100, project_id=None, *, states=None):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("运行数量上限必须为非负整数或 None")
        if states is not None:
            if not isinstance(states, (list, tuple, set, frozenset)) or any(
                type(state) is not str or state not in RUN_STATES for state in states
            ):
                raise ValueError("运行状态筛选无效")
            states = sorted(set(states))
            if not states:
                return []
        with self.connect() as c:
            query = "SELECT data FROM runs"
            conditions, values = [], []
            if project_id is not None:
                conditions.append("data->'request'->>'project_id'=%s")
                values.append(project_id)
            if states is not None:
                # These are validated RUN_STATES constants. A bound array hides
                # their selectivity from generic prepared plans and scans history.
                conditions.append(
                    "state IN (" + ",".join("'" + state + "'" for state in states) + ")"
                )
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY data->>'created_at' DESC,id DESC"
            if limit is not None:
                query += " LIMIT %s"
                values.append(limit)
            return [r["data"] for r in c.execute(query, values)]

    @contextmanager
    def locked(self, run_id, owner=None, *, dispatch=False):
        with self.connect() as c:
            if dispatch:
                dispatch_lock(c)
            row = c.execute(
                "SELECT data FROM runs WHERE id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError("找不到运行")
            data = row["data"]
            if owner is not None and data.get("owner") != owner:
                raise OwnershipLost("执行权已移交，旧 worker 停止操作")
            yield c, data
            data["updated_at"] = now()
            c.execute(
                "UPDATE runs SET state=%s,data=%s WHERE id=%s",
                (data["state"], Jsonb(data), run_id),
            )

    def event(self, run_id, kind, payload, connection=None):
        if connection is None:
            with self.connect() as c:
                return self.event(run_id, kind, payload, c)
        connection.execute(
            "INSERT INTO events(run_id,kind,payload) VALUES(%s,%s,%s)",
            (run_id, kind, Jsonb(payload)),
        )

    def events(self, run_id, after=0):
        with self.connect() as c:
            rows = c.execute(
                "SELECT id,kind,created_at,payload FROM events WHERE run_id=%s AND id>%s ORDER BY id LIMIT 200",
                (run_id, after),
            ).fetchall()
        for row in rows:
            row["created_at"] = row["created_at"].isoformat()
        return {"events": rows, "cursor": rows[-1]["id"] if rows else after}

    def transition(self, run_id, state, reason, allowed=None):
        with self.locked(run_id) as (c, run):
            if allowed is not None and run["state"] not in allowed:
                raise ValueError(f"当前状态 {run['state']} 不允许该操作")
            run.update(state=state, reason=reason)
            self.event(run_id, state, {"reason": reason}, c)
        return self.get(run_id)

    def pending(self, run_id):
        with self.connect() as c:
            rows = c.execute(
                "SELECT id,kind,status,data FROM actions WHERE run_id=%s AND status IN ('pending','unknown') ORDER BY data->>'started_at'",
                (run_id,),
            ).fetchall()
        for row in rows:
            row["id"] = str(row["id"])
        return rows

    def action(self, run_id, kind, data, connection=None):
        if connection is None:
            with self.connect() as c:
                return self.action(run_id, kind, data, c)
        action_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO actions VALUES(%s,%s,%s,'pending',%s)",
            (action_id, run_id, kind, Jsonb({**data, "started_at": now()})),
        )
        return action_id

    def finish_action(self, action_id, status, result, connection=None, owner=None):
        if connection is None:
            with self.connect() as c:
                return self.finish_action(action_id, status, result, c, owner)
        if owner is not None:
            row = connection.execute(
                "SELECT runs.data FROM runs JOIN actions ON actions.run_id=runs.id WHERE actions.id=%s FOR UPDATE OF runs",
                (action_id,),
            ).fetchone()
            if not row or row["data"].get("owner") != owner:
                raise OwnershipLost("动作所属执行权已失效")
        connection.execute(
            "UPDATE actions SET status=%s,data=data || %s WHERE id=%s",
            (status, Jsonb({"result": result, "ended_at": now()}), action_id),
        )

    def mark_pending_unknown(self, run_id, connection, run):
        pending = connection.execute(
            "SELECT data FROM actions WHERE run_id=%s AND kind='model' AND status='pending'",
            (run_id,),
        ).fetchall()
        run["usage"]["unknown_calls"] += len(pending)
        from doonex.budget import mark_batch_unknown

        mark_batch_unknown(connection, run, len(pending))
        assignments = {item["id"]: item for item in run.get("assignments", {}).values()}
        for row in pending:
            assignment = assignments.get(row["data"].get("assignment_id"))
            if assignment:
                assignment["usage"]["unknown_calls"] += 1
        connection.execute(
            "UPDATE actions SET status='unknown' WHERE run_id=%s AND status='pending'",
            (run_id,),
        )
