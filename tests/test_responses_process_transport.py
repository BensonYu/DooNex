"""Real fixed helper/bootstrap/transport against loopback, never a provider.

Hooks acknowledge synthetic local records; this is not database durability or
whole-session timing evidence. Keep an outer runner watchdog for these tests.
"""

import copy
import hashlib
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from doonex import responses_process as process
from doonex import responses_protocol as protocol
from doonex import responses_transport as transport

_SYNTHETIC_KEY = "offline-fixed-child-loopback-bearer"


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _response():
    return {
        "id": "resp_process_loopback",
        "created_at": 1,
        "object": "response",
        "status": "completed",
        "model": "gpt-4.1-mini",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_process_loopback",
                "summary": [{"type": "summary_text", "text": "synthetic summary"}],
                "encrypted_content": "opaque-synthetic-encrypted-content",
            },
            {
                "type": "function_call",
                "id": "fc_process_loopback",
                "call_id": "call_process_loopback",
                "name": "finish",
                "arguments": '{"message":"synthetic finish"}',
                "status": "completed",
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 7},
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    }


class _ServerState:
    def __init__(self, mode):
        self.mode = mode
        self.records = []
        self.requests = []
        self.started = threading.Event()
        self.peer_eof = threading.Event()
        self.errors = []


@pytest.fixture
def loopback_server():
    servers = []

    def start(mode):
        state = _ServerState(mode)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                state.requests.append(
                    {
                        "path": self.path,
                        "accept_encoding": self.headers.get("Accept-Encoding"),
                        "client_request_id": self.headers.get("X-Client-Request-Id"),
                        "static_bearer": self.headers.get("Authorization")
                        == "Bearer " + _SYNTHETIC_KEY,
                        "body": json.loads(body),
                        "record_state": state.records[-1]["state"]
                        if state.records
                        else None,
                    }
                )
                state.started.set()
                if mode == "hang":
                    self.connection.settimeout(8)
                    try:
                        if self.connection.recv(1) == b"":
                            state.peer_eof.set()
                    except OSError:
                        state.errors.append("socket_read")
                    return
                status = (
                    mode if type(mode) is int else 307 if mode == "redirect" else 200
                )
                self.send_response(status)
                self.send_header("Connection", "close")
                self.send_header("X-Request-Id", "offline-process-http-request")
                self.send_header("X-Litellm-Response-Cost", "0.125")
                self.send_header("X-Unused-Secret-Echo", _SYNTHETIC_KEY)
                if mode == "redirect":
                    self.send_header("Location", "https://must-never-follow.invalid/")
                if mode == "body_limit":
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    if mode == "body_limit":
                        for _ in range(17):
                            self.wfile.write(b"10000\r\n" + b"x" * 65536 + b"\r\n")
                        self.wfile.write(b"0\r\n\r\n")
                    elif type(mode) is int:
                        self.wfile.write(
                            b'{"error":{"message":"private synthetic provider detail"}}'
                        )
                    else:
                        self.wfile.write(json.dumps(_response()).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return state, f"http://127.0.0.1:{server.server_port}/v1"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


class _ObservedLifecycle:
    """Read-only wrappers retain the real product code and actual syscalls."""

    def __init__(self, monkeypatch):
        runtime = process._runtime()
        self.source_hashes = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in (
                ("bootstrap", runtime.bootstrap),
                ("protocol", runtime.protocol),
                ("transport", runtime.transport),
            )
        }
        assert runtime.bootstrap == Path(process.__file__).with_name(
            "responses_helper_bootstrap.py"
        )
        assert runtime.protocol == Path(protocol.__file__).resolve()
        assert runtime.transport == Path(transport.__file__).resolve()
        self.runtime = runtime
        self.spawns = []
        self.waits = []
        self.valid_eof_and_exit = False
        self.cleanup_ipc_eof = False
        self.pipe_fd = None
        spawn, wait = os.posix_spawn, os.waitpid
        read_frame, eof_and_exit = process._read_frame, process._eof_and_exit
        close = process._Resources.close

        def observe_spawn(path, argv, environment, **kwargs):
            assert argv[1:4] == ["-I", "-S", str(runtime.bootstrap)]
            assert argv[-2:] == [
                self.source_hashes["protocol"],
                self.source_hashes["transport"],
            ]
            assert environment == {"LANG": "C", "LC_ALL": "C"}
            assert _SYNTHETIC_KEY not in json.dumps([argv, environment])
            pid = spawn(path, argv, environment, **kwargs)
            self.spawns.append(pid)
            return pid

        def observe_wait(pid, flags):
            waited, status = wait(pid, flags)
            if waited:
                self.waits.append({"pid": waited, "wait_status": status})
            return waited, status

        def observe_read(resources, fd, limit, deadline):
            self.pipe_fd = fd
            return read_frame(resources, fd, limit, deadline)

        def observe_eof(resources, fd, deadline):
            result = eof_and_exit(resources, fd, deadline)
            self.valid_eof_and_exit = True
            return result

        def observe_close(resources, fd):
            if (
                fd is not None
                and fd == self.pipe_fd
                and fd in resources.fds
                and resources.reap is not None
                and not self.cleanup_ipc_eof
            ):
                try:
                    # Nonblocking inspection after actual reap, before product close.
                    self.cleanup_ipc_eof = os.read(fd, 1) == b""
                except BlockingIOError:
                    pass
            return close(resources, fd)

        monkeypatch.setattr(os, "posix_spawn", observe_spawn)
        monkeypatch.setattr(os, "waitpid", observe_wait)
        monkeypatch.setattr(process, "_read_frame", observe_read)
        monkeypatch.setattr(process, "_eof_and_exit", observe_eof)
        monkeypatch.setattr(process._Resources, "close", observe_close)

    def assert_reaped(self, pid):
        assert self.spawns == [pid]
        assert [item["pid"] for item in self.waits] == [pid]
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        assert self.cleanup_ipc_eof
        for name, expected in self.source_hashes.items():
            assert (
                hashlib.sha256(getattr(self.runtime, name).read_bytes()).hexdigest()
                == expected
            )


class _ChainedHooks:
    def __init__(self, request, state, observer):
        self.request = request
        self.state = state
        self.observer = observer
        self.previous = None
        self.child_pid = None

    def invoke(self, target):
        def callback(data):
            assert _SYNTHETIC_KEY not in json.dumps(data)
            assert data["schema"] == 1 and data["transport"] == protocol.TRANSPORT
            assert data["client_request_id"] == self.request.client_request_id
            if self.previous is not None:
                assert data["attempt_id"] == self.previous["attempt_id"]
                assert data["nonce"] == self.previous["nonce"]
                assert data["previous_record_sha256"] == self.previous["record_sha256"]
            if target == "prepared":
                assert self.previous is None and not self.state.requests
                assert data["parent"]["pid"] == os.getpid()
                assert data["parent"]["boot_id"] == self.request.parent_boot_id
                assert (
                    data["deadline_monotonic_ns"] == self.request.deadline_monotonic_ns
                )
                inspection = process.inspect_lease(
                    process.LeaseSpec(self.request.lease.path)
                )
                assert inspection.locked and data["lease"] == inspection.identity.data()
            elif target == "armed":
                assert self.previous["state"] == "prepared" and not self.state.requests
                self.child_pid = data["child"]["pid"]
                assert (
                    data["child"]["pgid"]
                    == self.child_pid
                    == os.getpgid(self.child_pid)
                )
                assert (
                    data["deadline_monotonic_ns"] == self.request.deadline_monotonic_ns
                )
                inspection = process.inspect_lease(
                    process.LeaseSpec(self.request.lease.path)
                )
                assert inspection.locked and data["lease"] == inspection.identity.data()
            elif target == "reaped":
                assert self.previous["state"] == "armed"
                assert data["reap"]["pid"] == self.child_pid
                self.observer.assert_reaped(self.child_pid)
                actual_status = self.observer.waits[0]["wait_status"]
                assert data["reap"]["wait_status"] == actual_status
                assert data["reap"]["exit_code"] == os.waitstatus_to_exitcode(
                    actual_status
                )
                assert data["reap"]["observed_monotonic_ns"] < time.monotonic_ns()
                assert not process.inspect_lease(
                    process.LeaseSpec(self.request.lease.path)
                ).locked
            else:
                pytest.fail(
                    "This real transport fixture must complete with actual reap"
                )
            record = {
                "state": target,
                "data": copy.deepcopy(data),
                "record_sha256": _hash(data),
            }
            self.state.records.append(record)
            ack = {
                "schema": 1,
                "attempt_id": data["attempt_id"],
                "nonce": data["nonce"],
                "state": target,
                "record_sha256": record["record_sha256"],
            }
            self.previous = ack
            return ack

        return callback

    def hooks(self):
        return process.HelperHooks(
            *(
                self.invoke(state)
                for state in ("prepared", "armed", "reaped", "blocked")
            )
        )


@pytest.fixture
def prepared(tmp_path, monkeypatch, loopback_server):
    # Runtime privacy controls precede any SDK import in this test module.
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OPENHANDS_SUPPRESS_BANNER", "1")
    # Deliberately inherited sentinels must never be copied into child env/receipts.
    monkeypatch.setenv("DOONEX_DATABASE_URL", "offline-do-not-inherit")
    monkeypatch.setenv("OTHER_PRIVATE_SENTINEL", "offline-do-not-inherit")

    def create(mode, seconds=6):
        from openhands.sdk.tool.builtins import FinishTool

        state, base_url = loopback_server(mode)
        private = tmp_path / ("private-" + uuid4().hex)
        private.mkdir(mode=0o700)
        client_request_id = str(uuid4())
        call = {
            "model": "gpt-4.1-mini",
            "custom_llm_provider": "openai",
            "api_base": base_url,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "private synthetic loopback task",
                        }
                    ],
                }
            ],
            "instructions": "Use finish for this synthetic fixture",
            "tools": [
                tool.to_responses_tool(add_security_risk_prediction=True)
                for tool in FinishTool.create()
            ],
            "api_version": None,
            "timeout": 30,
            "drop_params": True,
            "seed": None,
            "stream": False,
            "max_retries": 0,
            "num_retries": 0,
            "use_chat_completions_api": False,
            "extra_headers": {"X-Client-Request-Id": client_request_id},
            "max_output_tokens": 1000,
            "tool_choice": "auto",
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": "high"},
        }
        request = process.HelperRequest(
            call,
            _SYNTHETIC_KEY,
            time.monotonic_ns() + int(seconds * 1e9),
            process.LeaseSpec(private / "lease", allow_create=True),
            "offline-process-transport-test-boot",
            client_request_id,
        )
        observer = _ObservedLifecycle(monkeypatch)
        hooks = _ChainedHooks(request, state, observer)
        return request, state, observer, hooks

    return create


def _assert_request_once(request, state):
    assert len(state.requests) == 1
    observed = state.requests[0]
    assert observed["path"] == "/v1/responses" and observed["record_state"] == "armed"
    assert observed["accept_encoding"] == "identity" and observed["static_bearer"]
    assert observed["client_request_id"] == request.client_request_id
    assert observed["body"]["model"] == request.call["model"]
    assert observed["body"]["tools"] == request.call["tools"]
    assert observed["body"]["tools"][0]["name"] == "finish"
    assert [record["state"] for record in state.records] == [
        "prepared",
        "armed",
        "reaped",
    ]
    assert _SYNTHETIC_KEY not in json.dumps(state.records)


def test_fixed_child_finish_reasoning_usage_cost_and_real_reap(prepared):
    request, state, observer, hooks = prepared("success")
    terminal = process.run_helper(request, hooks.hooks())
    _assert_request_once(request, state)
    assert terminal.envelope["kind"] == "result" and terminal.reap["exit_code"] == 0
    assert observer.valid_eof_and_exit and observer.cleanup_ipc_eof
    assert terminal.completion == hooks.previous
    response = transport.rebuild_response(terminal.envelope)
    assert response.output[1].id == "fc_process_loopback"
    assert response.output[1].call_id == "call_process_loopback"
    assert response.output[1].arguments == '{"message":"synthetic finish"}'
    assert response.output[0].encrypted_content == "opaque-synthetic-encrypted-content"
    assert terminal.envelope["cost_usd"] == 0.125
    assert response._hidden_params == {
        "additional_headers": {transport.COST_HEADER: "0.125"}
    }
    assert _SYNTHETIC_KEY not in json.dumps(terminal.envelope)

    from openhands.sdk.llm import Message
    from openhands.sdk.llm.utils.metrics import Metrics
    from openhands.sdk.llm.utils.telemetry import Telemetry

    message = Message.from_llm_responses_output(response.output)
    assert message.tool_calls[0].name == "finish"
    assert message.tool_calls[0].arguments == '{"message":"synthetic finish"}'
    assert (
        message.responses_reasoning_item.encrypted_content
        == "opaque-synthetic-encrypted-content"
    )
    metrics = Metrics(model_name="gpt-4.1-mini")
    telemetry = Telemetry(model_name="gpt-4.1-mini", metrics=metrics)
    telemetry.on_request(None)
    telemetry.on_response(response)
    snapshot = metrics.get_snapshot()
    assert snapshot.accumulated_cost == 0.125
    assert snapshot.accumulated_token_usage.prompt_tokens == 100
    assert snapshot.accumulated_token_usage.completion_tokens == 20
    assert snapshot.accumulated_token_usage.cache_read_tokens == 7
    assert snapshot.accumulated_token_usage.reasoning_tokens == 3


@pytest.mark.parametrize(
    "status,error_type",
    [(401, "AuthenticationError"), (429, "RateLimitError"), (504, "Timeout")],
)
def test_fixed_child_http_error_diagnostics_survive_eof_reap_and_complete(
    prepared, status, error_type
):
    request, state, observer, hooks = prepared(status)
    terminal = process.run_helper(request, hooks.hooks())
    _assert_request_once(request, state)
    assert terminal.reap["exit_code"] == 0 and observer.valid_eof_and_exit
    error = terminal.envelope
    assert error["kind"] == "error" and error["reason"] == "provider_error"
    assert error["error_type"] == error_type
    assert error["diagnostics"]["source"] == "helper"
    assert error["diagnostics"]["http_status"] == status
    assert error["diagnostics"]["server_request_id"] == "offline-process-http-request"
    assert error["diagnostics"]["transport_phase"] == "unknown"
    assert "private synthetic provider detail" not in json.dumps(error)
    assert state.records[-1]["data"]["terminal_summary"] == {
        "kind": "error",
        "outcome": "failed",
        "reason": "helper_error",
    }


@pytest.mark.parametrize(
    "mode,reason,status",
    [("body_limit", "response_body_limit", 200), ("redirect", "redirect", 307)],
)
def test_fixed_child_local_http_policy_failure_has_one_post_and_clean_exit(
    prepared, mode, reason, status
):
    request, state, observer, hooks = prepared(mode)
    terminal = process.run_helper(request, hooks.hooks())
    _assert_request_once(request, state)
    assert (
        terminal.envelope["kind"] == "error" and terminal.envelope["reason"] == reason
    )
    assert terminal.envelope["diagnostics"]["http_status"] == status
    assert terminal.reap["exit_code"] == 0 and observer.valid_eof_and_exit


def test_fixed_child_deadline_reaps_stalled_http_and_closes_both_channels(prepared):
    request, state, observer, hooks = prepared("hang", seconds=4)
    began = time.monotonic()
    with pytest.raises(process.HelperProcessError) as caught:
        process.run_helper(request, hooks.hooks())
    assert time.monotonic() - began < 5.5
    _assert_request_once(request, state)
    error = caught.value
    assert error.reason in {"deadline", "early_eof", "child_exit"}
    assert error.evidence["reaped"] and not error.evidence["errors"]
    assert observer.cleanup_ipc_eof
    assert state.peer_eof.wait(timeout=1)
    assert not state.errors
    completion = state.records[-1]["data"]
    assert completion["terminal_summary"]["outcome"] == "failed"
    assert completion["terminal_summary"]["kind"] == "none"
    assert completion["reap"]["exit_code"] in {
        -signal.SIGALRM,
        -signal.SIGTERM,
        -signal.SIGKILL,
        73,
    }
    observer.assert_reaped(completion["reap"]["pid"])
