"""Database-bound private Skill and task-set records; imports never execute code."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import Field

from doonex.models import StrictModel
from doonex.skills import SkillRegistry
from doonex.store import now
from doonex.tasksets import TaskSetRegistry
from doonex.workspace import Workspace, _git


class ImportSkillInput(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    metadata: dict = Field(default_factory=dict)


class ImportTaskSetInput(StrictModel):
    path: str = Field(min_length=1, max_length=4096)


class ReviewSkillInput(StrictModel):
    status: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=4000)


class Library:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.skills = SkillRegistry(settings.home)
        self.tasksets = TaskSetRegistry(settings.home)

    def get(self, kind, identifier, connection=None):
        if connection is None:
            with self.store.connect() as c:
                return self.get(kind, identifier, c)
        row = connection.execute(
            "SELECT data FROM library_versions WHERE kind=%s AND id=%s",
            (kind, identifier),
        ).fetchone()
        if not row:
            raise KeyError("找不到版本")
        return row["data"]

    def list(self, kind):
        with self.store.connect() as c:
            return [
                r["data"]
                for r in c.execute(
                    "SELECT data FROM library_versions WHERE kind=%s ORDER BY data->>'created_at',id",
                    (kind,),
                )
            ]

    def bind(self, kind, identifier, record, connection=None):
        # The original database receipt is the trust anchor, including metadata
        # excluded from content identity. Reimport must never rebind it.
        if connection is None:
            with self.store.connect() as c:
                return self.bind(kind, identifier, record, c)
        connection.execute(
            "INSERT INTO library_versions VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (kind, identifier, Jsonb(record)),
        )
        return self.get(kind, identifier, connection)

    def import_skill(self, path, metadata):
        record = self.skills.import_local(Path(path).expanduser(), metadata)
        saved = self.bind(
            "skill",
            record["version_id"],
            {
                **record,
                "review_status": "pending",
                "review_history": [],
            },
        )
        result = self.skill(saved)
        if not result["integrity"]["valid"]:
            raise ValueError("已导入 Skill 与原保存凭据不一致；禁止重新绑定")
        return result

    def skill(self, record, *, instructions=False):
        integrity = self.skills.inspect(record["version_id"], record["report_sha256"])
        result = {
            **record,
            "integrity": {
                "valid": integrity["valid"],
                "errors": integrity["errors"],
            },
        }
        if instructions and integrity["valid"]:
            result["instructions"] = self.skills.context(
                record["version_id"], record["report_sha256"]
            ).instructions
        return result

    def review(self, version_id, status, note):
        with self.store.connect() as c:
            row = c.execute(
                "SELECT data FROM library_versions WHERE kind='skill' AND id=%s FOR UPDATE",
                (version_id,),
            ).fetchone()
            if not row:
                raise KeyError("找不到 Skill")
            record = row["data"]
            if status == "approved" and not self.skill(record)["integrity"]["valid"]:
                raise ValueError("Skill 完整性无效，不能批准")
            record["review_status"] = status
            record["review_history"].append(
                {"status": status, "note": note, "at": now()}
            )
            c.execute(
                "UPDATE library_versions SET data=%s WHERE kind='skill' AND id=%s",
                (Jsonb(record), version_id),
            )
        return self.skill(record)

    def freeze_skill(self, version_id):
        if version_id is None:
            return None
        record = self.get("skill", version_id)
        if record["review_status"] != "approved":
            raise ValueError("只有明确审核批准的 Skill 版本可用于新运行")
        if not self.skill(record)["integrity"]["valid"]:
            raise ValueError("Skill 内容或保存凭据已变化")
        return {
            key: record[key]
            for key in ("version_id", "content_digest", "report_sha256", "metadata")
        }

    def skill_context(self, binding):
        saved = self.get("skill", binding["version_id"])
        if any(
            binding.get(k) != saved.get(k)
            for k in ("version_id", "content_digest", "report_sha256", "metadata")
        ):
            raise ValueError("运行中的 Skill 绑定已变化")
        # Review decisions affect new runs only. A frozen run still validates
        # its exact immutable bytes, rather than consulting mutable approval.
        return self.skills.context(binding["version_id"], binding["report_sha256"])

    def import_taskset(self, path):
        record = self.tasksets.import_local(Path(path).expanduser())
        try:
            saved = self.get("taskset", record["taskset_id"])
        except KeyError:
            project_ids = {}
            for task in record["tasks"]:
                source = record["provenance"][task["id"]]["repo_path"]
                repository = Workspace.validate_repository(Path(source))
                head = _git(repository, "rev-parse", "HEAD").stdout.decode().strip()
                project = self.store.set_project(
                    {
                        "id": str(uuid4()),
                        "name": repository.name,
                        "path": str(repository),
                        "head": head,
                        "dirty": bool(
                            _git(repository, "status", "--porcelain=v1").stdout
                        ),
                    }
                )
                project_ids[task["id"]] = project["id"]
            saved = self.bind(
                "taskset", record["taskset_id"], {**record, "project_ids": project_ids}
            )
        result = self.taskset(saved)
        if not result["valid"]:
            raise ValueError("已导入任务集与原保存凭据不一致；禁止重新绑定")
        return result

    def taskset(self, record):
        integrity = self.tasksets.inspect(record["taskset_id"], record["report_sha256"])
        return {
            "id": record["taskset_id"],
            "name": record["name"],
            "category": record["category"],
            "split": record["split"],
            "minimum_pass_rate": record["minimum_pass_rate"],
            "task_count": len(record["tasks"]),
            "tasks": [
                {k: task[k] for k in ("id", "title", "critical")}
                for task in record["tasks"]
            ],
            "created_at": record["created_at"],
            "valid": integrity["valid"],
            "errors": integrity["errors"],
        }


def install_library_routes(app, settings, store, auth):
    library = Library(settings, store)

    @app.get("/api/skills", dependencies=auth)
    def skills():
        return {"skills": [library.skill(r) for r in library.list("skill")]}

    @app.post("/api/skills/import", dependencies=auth)
    def import_skill(body: ImportSkillInput):
        return library.import_skill(body.path, body.metadata)

    @app.get("/api/skills/{version_id}", dependencies=auth)
    def skill(version_id: str):
        return library.skill(library.get("skill", version_id), instructions=True)

    @app.post("/api/skills/{version_id}/review", dependencies=auth)
    def review(version_id: str, body: ReviewSkillInput):
        return library.review(version_id, body.status, body.note)

    @app.get("/api/arena/tasksets", dependencies=auth)
    def tasksets():
        return {"tasksets": [library.taskset(r) for r in library.list("taskset")]}

    @app.post("/api/arena/tasksets/import", dependencies=auth)
    def import_taskset(body: ImportTaskSetInput):
        return library.import_taskset(body.path)
