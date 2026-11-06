import subprocess

import pytest
from doonex.api import create_app
from doonex.config import Settings
from fastapi.testclient import TestClient


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        yield client


def repository(tmp_path):
    repo = tmp_path / "sample"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Offline test")
    (repo / "hello.py").write_text("answer = 1\n")
    git("add", "hello.py")
    git("commit", "-m", "fixture")
    return repo, git


def test_auth_and_origin_block_local_cross_site_requests(client):
    assert (
        client.get("/api/settings", headers={"Authorization": "bad"}).status_code == 401
    )
    assert (
        client.get(
            "/api/settings", headers={"Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.get("/api/settings", headers={"Host": "attacker.invalid"}).status_code
        == 403
    )


def test_project_and_run_preserve_dirty_source_and_immutable_inputs(client, tmp_path):
    repo, git = repository(tmp_path)
    (repo / "hello.py").write_text("answer = 2\n")
    git("add", "hello.py")
    (repo / "hello.py").write_text("answer = 3\n")
    (repo / "private-untracked").write_text("must remain outside task")
    before = git("status", "--porcelain=v1")
    project = client.post("/api/project", json={"path": str(repo)})
    assert project.status_code == 200 and project.json()["dirty"]
    response = client.post(
        "/api/runs", json={"prompt": "Fix fixture", "checks": ["python -m unittest"]}
    )
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["request"]["base_commit"] == git("rev-parse", "HEAD")
    assert run["request"]["acceptance_version"] == 1
    assert git("status", "--porcelain=v1") == before
    assert (
        client.patch(f"/api/runs/{run['id']}", json={"checks": ["true"]}).status_code
        == 405
    )
    assert (
        client.get(f"/api/runs/{run['id']}/events").json()["events"][0]["kind"]
        == "queued"
    )
    assert client.get(f"/api/runs/{run['id']}").json()["id"] == run["id"]
    assert client.post(f"/api/runs/{run['id']}/cancel").json()["state"] == "cancelled"


def test_missing_credentials_or_prices_fail_before_dispatch(
    settings, store, tmp_path, monkeypatch
):
    repo, _ = repository(tmp_path)
    monkeypatch.delenv("DOONEX_LLM_API_KEY")
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.post("/api/project", json={"path": str(repo)}).status_code == 200
        assert client.post("/api/runs", json={"prompt": "blocked"}).status_code == 400
        assert store.list_runs() == []
    monkeypatch.setenv("DOONEX_LLM_API_KEY", "fake")
    model = settings.model.model_copy(update={"input_price_per_million": None})
    config = Settings(settings.home, settings.database_url, settings.token, model)
    with TestClient(create_app(config)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.post("/api/runs", json={"prompt": "blocked"}).status_code == 400
        assert store.list_runs() == []


def test_adoption_detects_branch_drift_and_dirty_worktree(client, tmp_path):
    repo, git = repository(tmp_path)
    client.post("/api/project", json={"path": str(repo)})
    run = client.post("/api/runs", json={"prompt": "fixture"}).json()
    (repo / "new").write_text("user edit")
    assert not client.post(f"/api/runs/{run['id']}/adoption-check").json()["safe"]
    git("add", "new")
    git("commit", "-m", "advance target")
    result = client.post(f"/api/runs/{run['id']}/adoption-check").json()
    assert not result["safe"] and "分支已变化" in result["reason"]


def test_missing_project_path_returns_readable_client_error(client, tmp_path):
    response = client.post("/api/project", json={"path": str(tmp_path / "missing")})
    assert response.status_code == 400
    assert "无法注册项目" in response.json()["detail"]


def test_artifact_does_not_follow_parent_symlink(
    client, store, settings, request_data, tmp_path
):
    run = store.create_run(request_data)
    store.transition(run["id"], "completed_unverified", "fixture")
    directory = settings.home / "runs" / run["id"]
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "diff.patch").write_text("must never read")
    (directory / "evidence").symlink_to(outside, target_is_directory=True)
    assert client.get(f"/api/runs/{run['id']}/artifact?name=patch").status_code == 404
    assert "must never read" not in client.get(f"/api/runs/{run['id']}/diff").text
