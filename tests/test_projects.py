"""M1 migration and explicit M2 project boundaries use real PostgreSQL."""

from uuid import uuid4

import pytest
from doonex.api import create_app
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_api import repository


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        yield client


def two_projects(client, tmp_path):
    records = []
    for name in ("first", "second"):
        parent = tmp_path / name
        parent.mkdir()
        repo, git = repository(parent)
        project = client.post("/api/projects", json={"path": str(repo)})
        assert project.status_code == 200, project.text
        records.append((project.json(), repo, git))
    return records


def test_m1_migration_preserves_project_run_events_and_actions(store, request_data):
    project = {
        "id": str(uuid4()),
        "path": "/old/private/project",
        "head": "a" * 40,
        "dirty": True,
    }
    request_data["project_id"] = project["id"]
    run = store.create_run(request_data)
    action = store.action(run["id"], "model", {"tokens": 123})
    before_events = store.events(run["id"])
    with store.connect() as connection:
        connection.execute("INSERT INTO project(data) VALUES(%s)", (Jsonb(project),))
    store.initialize()
    store.initialize()
    assert store.project(project["id"]) == project
    assert store.list_projects() == [project]
    assert store.get(run["id"]) == run
    assert store.events(run["id"]) == before_events
    assert store.pending(run["id"])[0]["id"] == action
    updated = {**project, "name": "new display name"}
    store.set_project(updated)
    store.initialize()
    assert store.project(project["id"]) == updated


def test_registration_stable_ids_and_explicit_creation(client, tmp_path):
    first, second = two_projects(client, tmp_path)
    a, b = first[0], second[0]
    assert a["id"] != b["id"]
    repeated = client.post("/api/projects", json={"path": a["path"] + "/."}).json()
    assert repeated["id"] == a["id"]
    assert len(client.get("/api/projects").json()) == 2
    assert client.get(f"/api/projects/{b['id']}").json()["path"] == b["path"]
    assert client.post("/api/runs", json={"prompt": "ambiguous"}).status_code == 400
    assert client.get("/api/runs").status_code == 400
    assert (
        client.post(
            f"/api/runs?project_id={b['id']}",
            json={"prompt": "wrong context", "project_id": a["id"]},
        ).status_code
        == 400
    )
    for project, repo, git in (first, second):
        run = client.post(
            "/api/runs",
            json={
                "project_id": project["id"],
                "prompt": "isolated task",
                "workflow": "execute_review",
            },
        )
        assert run.status_code == 200, run.text
        data = run.json()
        assert data["request"]["repo_path"] == str(repo)
        assert data["request"]["base_commit"] == git("rev-parse", "HEAD")
        assert data["request"]["base_branch"] == "refs/heads/main"
        assert set(data["assignments"]) == {"executor", "reviewer"}
        assert all(
            role["parent_run_id"] == data["id"] for role in data["assignments"].values()
        )
        own = client.get(f"/api/runs?project_id={project['id']}").json()
        assert [item["id"] for item in own] == [data["id"]]


@pytest.mark.parametrize(
    "path", ["", "/events", "/diff", "/evidence", "/recovery", "/artifact?name=patch"]
)
def test_wrong_project_cannot_read_run_or_artifacts(client, tmp_path, path):
    first, second = two_projects(client, tmp_path)
    a, b = first[0], second[0]
    run = client.post(
        "/api/runs", json={"project_id": a["id"], "prompt": "private A"}
    ).json()
    join = "&" if "?" in path else "?"
    result = client.get(f"/api/runs/{run['id']}{path}{join}project_id={b['id']}")
    assert result.status_code == 404
    assert "private A" not in result.text


@pytest.mark.parametrize("action", ["pause", "cancel", "resume", "adoption-check"])
def test_wrong_project_control_is_rejected_without_changing_run(
    client, store, tmp_path, action
):
    first, second = two_projects(client, tmp_path)
    a, b = first[0], second[0]
    run = client.post(
        "/api/runs", json={"project_id": a["id"], "prompt": "preserve A"}
    ).json()
    result = client.post(
        f"/api/runs/{run['id']}/{action}?project_id={b['id']}", json={}
    )
    assert result.status_code == 404
    assert store.get(run["id"]) == run


def test_adoption_rejects_switch_to_another_branch_at_same_commit(client, tmp_path):
    repo, git = repository(tmp_path)
    project = client.post("/api/projects", json={"path": str(repo)}).json()
    run = client.post(
        "/api/runs", json={"project_id": project["id"], "prompt": "fixture"}
    ).json()
    git("switch", "-c", "different-target")
    assert git("rev-parse", "HEAD") == run["request"]["base_commit"]
    result = client.post(f"/api/runs/{run['id']}/adoption-check").json()
    assert result["safe"] is False
    assert "分支名称" in result["reason"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", ""),
        ("get", "/events"),
        ("get", "/artifact?name=patch"),
        ("post", "/pause"),
        ("post", "/cancel"),
        ("post", "/resume"),
        ("post", "/adoption-check"),
    ],
)
def test_multi_project_run_access_requires_explicit_context(
    client, store, tmp_path, method, path
):
    first, _ = two_projects(client, tmp_path)
    run = client.post(
        "/api/runs", json={"project_id": first[0]["id"], "prompt": "explicit context"}
    ).json()
    response = getattr(client, method)(f"/api/runs/{run['id']}{path}")
    assert response.status_code == 400
    assert store.get(run["id"]) == run


def test_pending_unknown_is_attributed_to_role_without_double_count(
    store, request_data
):
    request_data["workflow"] = "execute_review"
    run = store.create_run(request_data)
    assignment_id = run["assignments"]["reviewer"]["id"]
    store.action(
        run["id"], "model", {"assignment_id": assignment_id, "role": "reviewer"}
    )
    for _ in range(2):
        with store.locked(run["id"]) as (connection, current):
            store.mark_pending_unknown(run["id"], connection, current)
    final = store.get(run["id"])
    assert final["usage"]["unknown_calls"] == 1
    assert final["assignments"]["executor"]["usage"]["unknown_calls"] == 0
    assert final["assignments"]["reviewer"]["usage"]["unknown_calls"] == 1
