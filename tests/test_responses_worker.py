"""Real SDK, helper process, PostgreSQL and Docker; only provider is loopback."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from doonex import responses_adapter
from doonex.api import create_app
from doonex.responses_registry import helper_fenced
from doonex.worker import Worker
from fastapi.testclient import TestClient
from test_sdk import responses_answer
from test_worker import prepare_run

pytestmark = [pytest.mark.postgres, pytest.mark.docker]


@contextmanager
def provider(status=200):
    requests = []
    answers = [
        responses_answer(
            "doonex_shell", {"command": "printf 'answer = 42\\n' > hello.py"}
        ),
        responses_answer("finish", {"message": "synthetic complete"}, sequence=2),
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {"body": body, "request_id": self.headers.get("X-Client-Request-Id")}
            )
            payload = (
                answers.pop(0).model_dump(mode="json")
                if status == 200
                else {"error": {"message": "synthetic refusal", "type": "rate_limit"}}
            )
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("x-request-id", "synthetic-server-request")
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def isolated_run(store, settings, request_data, tmp_path, url):
    request_data["model"].update(
        api_mode="responses",
        responses_transport="isolated_direct_v1",
        base_url=url,
        allow_private_http=True,
        timeout_seconds=20,
    )
    return prepare_run(store, settings, request_data, tmp_path)[0]


def actions(store, run_id):
    with store.connect() as connection:
        return connection.execute(
            "SELECT id,status,data FROM actions WHERE run_id=%s AND kind='model' ORDER BY data->>'started_at'",
            (run_id,),
        ).fetchall()


def test_real_isolated_worker_delivers_and_uses_same_lease(
    settings, store, request_data, tmp_path, monkeypatch
):
    def forbidden(**kwargs):
        pytest.fail("Isolated worker must not use parent provider transport")

    monkeypatch.setattr(responses_adapter, "_ORIGINAL", forbidden)
    with provider() as (url, requests):
        run = isolated_run(store, settings, request_data, tmp_path, url)
        Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert len(requests) == final["usage"]["calls"] == 2
    assert final["usage"]["tokens"] == 240
    assert final["usage"]["unknown_calls"] == final["usage"]["reserved_tokens"] == 0
    records = actions(store, run["id"])
    helpers = [row["data"]["transport_helper"] for row in records]
    assert all(row["status"] == "completed" for row in records)
    assert all(item["state"] == "reaped" for item in helpers)
    assert all(item["terminal_summary"]["outcome"] == "success" for item in helpers)
    assert helpers[0]["lease"] == helpers[1]["lease"]
    assert [item["client_request_id"] for item in helpers] == [
        request["request_id"] for request in requests
    ]
    assert len({item["attempt_id"] for item in helpers}) == 2
    with store.connect() as connection:
        assert not helper_fenced(connection)


def test_helper_http_failure_keeps_reservation_and_real_diagnostics(
    settings, store, request_data, tmp_path
):
    with provider(429) as (url, requests):
        run = isolated_run(store, settings, request_data, tmp_path, url)
        Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "failed", final["reason"]
    assert (
        len(requests) == final["usage"]["calls"] == final["usage"]["unknown_calls"] == 1
    )
    assert final["usage"]["reserved_tokens"] > 0
    (row,) = actions(store, run["id"])
    assert row["status"] == "unknown"
    helper = row["data"]["transport_helper"]
    assert helper["state"] == "reaped"
    assert helper["terminal_summary"]["outcome"] == "failed"
    events = [
        event
        for event in store.events(run["id"])["events"]
        if event["kind"] == "model_request_failed"
    ]
    assert len(events) == 1
    diagnostic = events[0]["payload"]
    assert diagnostic["source"] == "helper"
    assert diagnostic["http_status"] == 429
    assert diagnostic["server_request_id"] == "synthetic-server-request"
    assert diagnostic["client_request_id"] == requests[0]["request_id"]
    with store.connect() as connection:
        assert not helper_fenced(connection)


def test_sdk_preparation_failure_records_original_parent_not_spawned(
    settings, store, request_data, tmp_path, monkeypatch
):
    from doonex.sdk import GuardedLLM

    def fail(*args, **kwargs):
        raise ValueError("synthetic SDK preparation failure")

    monkeypatch.setattr(GuardedLLM, "_prepare_responses_params", fail)
    with provider() as (url, requests):
        run = isolated_run(store, settings, request_data, tmp_path, url)
        Worker(settings).run(once=True)
    assert requests == []
    final = store.get(run["id"])
    assert final["state"] == "failed"
    assert final["usage"]["unknown_calls"] == 1
    assert final["usage"]["reserved_tokens"] > 0
    (row,) = actions(store, run["id"])
    helper = row["data"]["transport_helper"]
    assert helper["state"] == "not_spawned"
    assert helper["registered"] is False
    assert helper["lease"] is None
    with store.connect() as connection:
        assert not helper_fenced(connection)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.get("/api/transport-helpers").json()["items"] == []
