"""Actual Worker.run + isolated Responses helper; only HTTP provider is local.

The frozen Chat timeline fixture supplies synthetic *response data* to this
HTTP server. Its transport monkeypatch is intercepted, never installed. The
SDK's actual Responses conversion, binding, process, pipes and leases execute.
"""

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import controller_test_support
import pytest
from doonex import responses_adapter, responses_process, sdk
from doonex.background import Background
from doonex.responses_registry import helper_fenced, inspect_helper
from test_scheduling import taskset_source as taskset_fixture
from test_scheduling import tuning as tuning_fixture
from test_sdk import responses_answer
from test_worker_wait_matrix import (
    test_actual_worker_wait_timeline as chat_timeline_fixture,
)

controller_registry = controller_test_support.controller_registry
taskset_source = taskset_fixture
tuning = tuning_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]


class ResponsesProviderPatch:
    """Adapt the existing fixture's data factory, not production execution."""

    def __init__(self, patch, provider):
        self.patch, self.provider = patch, provider

    def setattr(self, target, name, value):
        if target is sdk.GuardedLLM and name == "_transport_call":
            assert self.provider.dispatch is None
            self.provider.dispatch = value
        else:
            self.patch.setattr(target, name, value)


@contextmanager
def loopback_provider():
    class Provider:
        dispatch = None
        finish_near_deadline = False
        store = None

        def __init__(self):
            self.requests, self.errors = [], []

    provider = Provider()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/responses"
                size = int(self.headers["Content-Length"])
                assert 0 < size <= 20 * 1024 * 1024
                body = json.loads(self.rfile.read(size))
                request_id = self.headers.get("X-Client-Request-Id")
                assert request_id and callable(provider.dispatch)
                provider.requests.append(
                    {
                        "client_request_id": request_id,
                        "received_monotonic": time.monotonic(),
                        "body": body,
                    }
                )
                data = provider.dispatch()
                sequence = len(provider.requests)
                observed = provider.requests[-1]
                observed["gate_returned_monotonic"] = time.monotonic()
                if provider.finish_near_deadline and sequence == 1:
                    # This is the actual armed helper cutoff, including real
                    # SDK preparation/startup, not a fresh provider-only timer.
                    with provider.store.connect() as connection:
                        row = connection.execute(
                            "SELECT data->'transport_helper' AS helper FROM actions "
                            "WHERE data->'transport_helper'->>'client_request_id'=%s",
                            (request_id,),
                        ).fetchone()
                    deadline = row["helper"]["deadline_monotonic_ns"] / 1e9
                    assert deadline - time.monotonic() > 1.5
                    observed["actual_helper_deadline_monotonic"] = deadline
                    target = deadline - 1.25
                    while time.monotonic() < target:
                        time.sleep(max(0, min(0.05, target - time.monotonic())))
                calls = data.choices[0].message.tool_calls
                assert calls
                answer = responses_answer(
                    calls[0].function.name,
                    json.loads(calls[0].function.arguments),
                    sequence=sequence * 100,
                )
                for index, call in enumerate(calls[1:], start=1):
                    answer.output.extend(
                        responses_answer(
                            call.function.name,
                            json.loads(call.function.arguments),
                            sequence=sequence * 100 + index,
                        ).output
                    )
                encoded = answer.model_dump_json().encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("x-request-id", f"synthetic-http-{sequence}")
                self.end_headers()
                self.wfile.write(encoded)
                observed["response_sent_monotonic"] = time.monotonic()
            except BaseException as error:  # noqa: BLE001 - keep HTTP-thread failures
                provider.errors.append(error)
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider.url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield provider
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "background_stage", ["model", "shell", "verifier", "preparation"]
)
@pytest.mark.parametrize("foreground_kind", ["ordinary", "arena", "generation"])
def test_actual_isolated_worker_wait_timeline(
    tuning,
    taskset_source,
    store,
    request_data,
    tmp_path,
    monkeypatch,
    background_stage,
    foreground_kind,
    near_deadline=False,
):
    settings, _, body, repository_path = tuning
    eof, closed = [], []
    original_eof = responses_process._eof_and_exit
    original_close = responses_process._Resources.close_all

    def observed_eof(resources, *args, **kwargs):
        result = original_eof(resources, *args, **kwargs)
        assert resources.reap is not None and resources.reap["exit_code"] == 0
        eof.append({"pid": resources.pid, "at": time.monotonic()})
        return result

    def observed_close(resources):
        result = original_close(resources)
        assert not resources.fds and resources.selector is None
        if resources.pid is not None:
            closed.append({"pid": resources.pid, "at": time.monotonic()})
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("Isolated fixture reached parent provider transport")

    monkeypatch.setattr(responses_process, "_eof_and_exit", observed_eof)
    monkeypatch.setattr(responses_process._Resources, "close_all", observed_close)
    monkeypatch.setattr(responses_adapter, "_ORIGINAL", forbidden)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)
    with loopback_provider() as provider:
        provider.finish_near_deadline = near_deadline
        provider.store = store
        settings = replace(
            settings,
            model=settings.model.model_copy(
                update={
                    "api_mode": "responses",
                    "responses_transport": "isolated_direct_v1",
                    "base_url": provider.url,
                    "allow_private_http": True,
                    "timeout_seconds": 8 if near_deadline else 30,
                }
            ),
        )
        request_data["model"] = settings.model.model_dump()
        actual_tuning = settings, Background(settings, store), body, repository_path
        chat_timeline_fixture(
            actual_tuning,
            taskset_source,
            store,
            request_data,
            tmp_path,
            ResponsesProviderPatch(monkeypatch, provider),
            background_stage,
            foreground_kind,
        )
        assert not provider.errors, provider.errors
        if near_deadline:
            first = provider.requests[0]
            remaining = (
                first["actual_helper_deadline_monotonic"]
                - first["response_sent_monotonic"]
            )
            assert 0 < remaining <= 1.5
        with store.connect() as connection:
            actions = connection.execute(
                "SELECT id,run_id,status,data FROM actions WHERE kind='model' "
                "ORDER BY data->>'started_at',id"
            ).fetchall()
            assert not helper_fenced(connection)
        expected_calls = 1 if background_stage == "preparation" else 2
        assert len(actions) == len(provider.requests) == expected_calls
        assert all(row["status"] == "completed" for row in actions)
        helpers = [row["data"]["transport_helper"] for row in actions]
        request_ids = {item["client_request_id"] for item in provider.requests}
        assert len(request_ids) == expected_calls
        assert {item["client_request_id"] for item in helpers} == request_ids
        assert len({item["attempt_id"] for item in helpers}) == expected_calls
        assert all(item["lease"] == helpers[0]["lease"] for item in helpers)
        assert len(eof) == expected_calls
        assert {item["pid"] for item in closed} == {item["pid"] for item in eof}
        # cleanup() and its outer finally both call the idempotent close_all.
        # Every observed close must have closed real resources; do not treat
        # the repeated call as an additional helper or bypass either finally.
        assert len(closed) >= expected_calls
        for action, helper in zip(actions, helpers, strict=True):
            assert helper["state"] == "reaped"
            assert helper["terminal_summary"]["outcome"] == "success"
            assert helper["reap"]["exit_code"] == 0
            pid = helper["reap"]["pid"]
            assert any(item["pid"] == pid for item in eof)
            assert any(item["pid"] == pid for item in closed)
            with pytest.raises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            inspected = inspect_helper(settings, store, str(action["id"]))
            assert inspected["valid"] and not inspected["unresolved"]
            assert inspected["problems"] == []
            assert inspected["lease"]["locked"] is False
        print(
            "worker_wait_responses="
            + json.dumps(
                {
                    "background_stage": background_stage,
                    "foreground_kind": foreground_kind,
                    "received_requests": provider.requests,
                    "helper_eof": eof,
                    "helper_resources_closed": closed,
                    "helpers": helpers,
                    "real_model_calls": 0,
                    "request_timeout_seconds": settings.model.timeout_seconds,
                    "provider_finished_near_original_helper_deadline": near_deadline,
                },
                sort_keys=True,
            )
        )


@pytest.mark.parametrize("foreground_kind", ["ordinary", "arena", "generation"])
def test_isolated_request_completes_near_original_deadline(
    tuning, taskset_source, store, request_data, tmp_path, monkeypatch, foreground_kind
):
    test_actual_isolated_worker_wait_timeline(
        tuning,
        taskset_source,
        store,
        request_data,
        tmp_path,
        monkeypatch,
        "model",
        foreground_kind,
        near_deadline=True,
    )
