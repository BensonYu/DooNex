"""Two projects, both SDK roles, real containers, shared ledger and delivery."""

import json

import pytest
from doonex.api import create_app
from doonex.config import Settings
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from fastapi.testclient import TestClient
from test_api import repository
from test_worker import response


@pytest.mark.docker
def test_two_projects_complete_isolated_reviewed_deliveries(
    settings, store, tmp_path, monkeypatch
):
    model = settings.model.model_copy(update={"model_id": "gpt-4.1-mini"})
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    replies = []
    for project in ("alpha", "beta"):
        replies.extend(
            [
                response(
                    "doonex_shell",
                    {"command": "printf 'answer = 42\\n' > hello.py"},
                    len(replies),
                ),
                response(
                    "finish",
                    {"message": "Fixture execution finished"},
                    len(replies) + 1,
                ),
                response(
                    "doonex_shell",
                    {
                        "command": "cat diff.patch; cd after && python -B -c 'from hello import answer; assert answer == 42'"
                    },
                    len(replies) + 2,
                ),
                response(
                    "finish",
                    {
                        "message": json.dumps(
                            {
                                "status": "approved",
                                "summary": "Fixture change inspected",
                                "findings": [],
                            }
                        )
                    },
                    len(replies) + 3,
                ),
            ]
        )
    dispatched = []

    def transport(*args, **kwargs):
        dispatched.append(1)
        return replies.pop(0)

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    records = []
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        for name in ("alpha", "beta"):
            parent = tmp_path / name
            parent.mkdir()
            repo, git = repository(parent)
            (repo / "project-marker.txt").write_text(name)
            git("add", "project-marker.txt")
            git("commit", "-m", "Project identity fixture")
            (repo / "hello.py").write_text(f"user_{name} = 999\n")
            git("add", "hello.py")
            (repo / "private-untracked.txt").write_text(name)
            original = git("status", "--porcelain=v1")
            project = client.post("/api/projects", json={"path": str(repo)}).json()
            created = client.post(
                "/api/runs",
                json={
                    "project_id": project["id"],
                    "workflow": "execute_review",
                    "prompt": f"Fix {name}",
                    "checks": [
                        (
                            'python -c "from pathlib import Path; from hello import answer; '
                            f"assert answer == 42; assert Path('project-marker.txt').read_text() == '{name}'\""
                        )
                    ],
                    "budget": {
                        "max_calls": 4,
                        "max_tokens": 100000,
                        "max_cost_usd": 2,
                        "max_seconds": 600,
                    },
                },
            )
            assert created.status_code == 200, created.text
            records.append((created.json(), project, repo, git, original))
        for record, project, repo, git, original in records:
            Worker(settings).run(once=True)
            run = store.get(record["id"])
            assert run["state"] == "completed_verified", run["reason"]
            assert run["usage"]["calls"] == 4 and run["usage"]["tokens"] == 480
            assert run["usage"]["reserved_tokens"] == 0
            assert (
                sum(item["usage"]["calls"] for item in run["assignments"].values()) == 4
            )
            assert (
                sum(item["usage"]["tokens"] for item in run["assignments"].values())
                == 480
            )
            assert all(
                item["state"] == "completed" for item in run["assignments"].values()
            )
            assert run["review"]["status"] == "approved"
            assert (
                run["review"]["snapshot_digest"]
                == run["handoff"]["snapshot_digest"]
                == run["evidence"]["snapshot_digest"]
            )
            assert git("status", "--porcelain=v1") == original
            assert (repo / "private-untracked.txt").read_text() == repo.parent.name
            assert (repo / "hello.py").read_text() == f"user_{repo.parent.name} = 999\n"
            query = f"?project_id={project['id']}"
            scoped = client.get("/api/runs" + query).json()
            assert [row["id"] for row in scoped] == [run["id"]]
            assert client.get(f"/api/runs/{run['id']}" + query).json()[
                "review_integrity"
            ]["valid"]
            patch = client.get(
                f"/api/runs/{run['id']}/artifact" + query + "&name=patch"
            )
            assert patch.status_code == 200 and "+answer = 42" in patch.text
            assert "user_" not in patch.text and "private-untracked" not in patch.text
            assert not client.post(
                f"/api/runs/{run['id']}/adoption-check" + query
            ).json()["safe"]
        assert len(dispatched) == 8 and not replies
        assert (
            store.get(records[0][0]["id"])["evidence"]["snapshot_digest"]
            != store.get(records[1][0]["id"])["evidence"]["snapshot_digest"]
        )
        # Tamper with one frozen review context after completion. Only that
        # delivery loses verified status; the other project remains verified.
        first, project = records[0][0], records[0][1]
        context = run_directory(settings, first["id"]) / "review/context/after/hello.py"
        context.write_text("answer = -1\n")
        changed = client.get(
            f"/api/runs/{first['id']}?project_id={project['id']}"
        ).json()
        assert changed["state"] == "completed_unverified"
        assert not changed["review_integrity"]["valid"]
        second, second_project = records[1][0], records[1][1]
        assert (
            client.get(
                f"/api/runs/{second['id']}?project_id={second_project['id']}"
            ).json()["state"]
            == "completed_verified"
        )
