"""Kill a real worker while its real container is active; model traffic is fake."""

import multiprocessing
import time

import pytest
from doonex.api import create_app
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from fastapi.testclient import TestClient
from test_worker import prepare_run, response


def _worker_child(settings):
    from doonex.sdk import GuardedLLM
    from doonex.worker import Worker

    GuardedLLM._transport_call = lambda *a, **k: response(
        "doonex_shell",
        {"command": "printf x >> progress.txt; sleep 60; printf bad > late.txt"},
        1,
    )
    Worker(settings).run(once=True)


@pytest.mark.docker
def test_killed_worker_preserves_changes_and_resumes_without_replaying_tool(
    settings, store, request_data, tmp_path, monkeypatch
):
    request_data["budget"]["max_calls"] = 3
    run, _, _ = prepare_run(
        store,
        settings,
        request_data,
        tmp_path,
        [
            "python -c \"from pathlib import Path; assert Path('progress.txt').read_text() == 'x'; assert not Path('late.txt').exists()\""
        ],
    )
    process = multiprocessing.get_context("spawn").Process(
        target=_worker_child, args=(settings,)
    )
    process.start()
    directory = run_directory(settings, run["id"])
    progress = directory / "workspace" / "progress.txt"
    try:
        deadline = time.monotonic() + 20
        while (
            not progress.exists() and time.monotonic() < deadline and process.is_alive()
        ):
            time.sleep(0.05)
        assert progress.exists()
        process.kill()
        process.join(timeout=10)
        assert process.exitcode != 0
        Worker(settings).run(once=True)
        interrupted = store.get(run["id"])
        assert interrupted["state"] == "interrupted"
        assert progress.read_text() == "x"
        assert any(action["kind"] == "shell" for action in store.pending(run["id"]))
        with TestClient(create_app(settings)) as client:
            client.headers["Authorization"] = "Bearer " + settings.token
            assert (
                client.post(
                    f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": False}
                ).status_code
                == 400
            )
            assert (
                client.post(
                    f"/api/runs/{run['id']}/resume", json={"acknowledge_unknown": True}
                ).status_code
                == 200
            )
        monkeypatch.setattr(
            GuardedLLM,
            "_transport_call",
            lambda *a, **k: response(
                "finish", {"message": "Existing changes checked; do not repeat"}, 2
            ),
        )
        Worker(settings).run(once=True)
        final = store.get(run["id"])
        assert final["state"] == "completed_verified", final["reason"]
        assert final["usage"]["calls"] == 2
        assert final["session_generation"] == 1
        assert progress.read_text() == "x"
        assert not progress.with_name("late.txt").exists()
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        from doonex.sandbox import Sandbox

        Sandbox().stop_run(run["id"])
