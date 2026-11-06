"""PostgreSQL is the authority for run state, dispatch intent and budget."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def now():
    return datetime.now(UTC).isoformat()


class OwnershipLost(RuntimeError):
    """A former executor must not mutate the new executor's run."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), data jsonb NOT NULL
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


class Store:
    def __init__(self, database_url):
        self.database_url = database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def initialize(self):
        with self.connect() as connection:
            connection.execute(SCHEMA)

    def project(self):
        with self.connect() as c:
            row = c.execute("SELECT data FROM project WHERE singleton").fetchone()
        return row["data"] if row else None

    def set_project(self, data):
        with self.connect() as c:
            c.execute("LOCK TABLE project IN EXCLUSIVE MODE")
            existing = c.execute("SELECT data FROM project WHERE singleton").fetchone()
            if existing and existing["data"]["path"] != data["path"]:
                raise ValueError(
                    "M1 仅管理一个项目；请使用独立的数据目录和数据库注册其他项目"
                )
            c.execute(
                "INSERT INTO project(data) VALUES(%s) ON CONFLICT(singleton) DO UPDATE SET data=EXCLUDED.data",
                (Jsonb(data),),
            )
        return data

    def create_run(self, request):
        run_id = str(uuid.uuid4())
        data = {
            "id": run_id,
            "state": "queued",
            "reason": "等待执行",
            "created_at": now(),
            "updated_at": now(),
            "request": request,
            "usage": {
                "calls": 0,
                "tokens": 0,
                "cost_usd": None
                if request["model"].get("billing_mode") == "subscription"
                else 0.0,
                "reserved_tokens": 0,
                "reserved_cost_usd": None
                if request["model"].get("billing_mode") == "subscription"
                else 0.0,
                "unknown_calls": 0,
            },
            "first_started_at": None,
            "evidence": None,
        }
        with self.connect() as c:
            c.execute(
                "INSERT INTO runs VALUES(%s,%s,%s)", (run_id, "queued", Jsonb(data))
            )
            self.event(run_id, "queued", {"acceptance_version": 1}, connection=c)
        return data

    def get(self, run_id):
        with self.connect() as c:
            row = c.execute("SELECT data FROM runs WHERE id=%s", (run_id,)).fetchone()
        if not row:
            raise KeyError("找不到运行")
        return row["data"]

    def list_runs(self, limit=100):
        with self.connect() as c:
            query = "SELECT data FROM runs ORDER BY data->>'created_at' DESC"
            if limit is not None:
                return [r["data"] for r in c.execute(query + " LIMIT %s", (limit,))]
            return [r["data"] for r in c.execute(query)]

    @contextmanager
    def locked(self, run_id, owner=None):
        with self.connect() as c:
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
        count = connection.execute(
            "SELECT count(*) AS n FROM actions WHERE run_id=%s AND kind='model' AND status='pending'",
            (run_id,),
        ).fetchone()["n"]
        run["usage"]["unknown_calls"] += count
        connection.execute(
            "UPDATE actions SET status='unknown' WHERE run_id=%s AND status='pending'",
            (run_id,),
        )
