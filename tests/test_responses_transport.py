"""Native fixed LiteLLM sync calls, in-memory streams and local HTTP only."""

import copy
import gzip
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from doonex import responses_protocol as protocol
from doonex import responses_transport as transport
from test_responses_protocol import CLIENT_ID, sample_arm, sample_response


@pytest.fixture(autouse=True)
def direct_offline_environment(monkeypatch):
    for name in protocol.NETWORK_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def only_loopback(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            assert address[0] in {"127.0.0.1", "::1"}, (
                "External network forbidden in helper tests"
            )
        return original(sock, address)

    def only_loopback_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            assert address[0] in {"127.0.0.1", "::1"}, (
                "External network forbidden in helper tests"
            )
        return original_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", only_loopback)
    monkeypatch.setattr(socket.socket, "connect_ex", only_loopback_ex)


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def execute_mock(
    monkeypatch,
    *,
    body=None,
    chunks=None,
    headers=None,
    status=200,
    error=None,
    arm=None,
    hook=None,
):
    arm = sample_arm() if arm is None else arm
    stream = TrackedStream(
        chunks
        if chunks is not None
        else [json.dumps(body or sample_response()).encode()]
    )
    calls = []
    hooks = []

    def fixture(request):
        calls.append(request)
        assert len(calls) == 1
        assert request.method == "POST" and request.url == httpx.URL(
            arm["call"]["api_base"].rstrip("/") + "/responses"
        )
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["x-client-request-id"] == CLIENT_ID
        assert request.headers["authorization"] == "Bearer " + arm["api_key"]
        if error:
            raise error("private error text must never escape", request=request)
        return httpx.Response(status, stream=stream, headers=headers, request=request)

    monkeypatch.setattr(
        transport, "_new_http_transport", lambda httpx: httpx.MockTransport(fixture)
    )

    def before_request():
        hooks.append(1)
        if hook:
            hook()

    terminal = transport.run_transport(arm, before_request=before_request)
    protocol.decode_frame_payload(
        protocol.encode_frame(terminal, "terminal")[4:], "terminal"
    )
    return terminal, stream, calls, hooks


def test_native_success_preserves_pinned_response_and_narrow_private_cost(monkeypatch):
    arm = sample_arm()
    terminal, stream, calls, hooks = execute_mock(
        monkeypatch,
        arm=arm,
        headers={
            "x-litellm-response-cost": "0.125",
            "x-private-key-echo": arm["api_key"],
        },
    )
    assert terminal["kind"] == "result" and terminal["cost_usd"] == 0.125
    rebuilt = transport.rebuild_response(terminal)
    assert (
        rebuilt.output[0].encrypted_content
        == sample_response()["output"][0]["encrypted_content"]
    )
    assert rebuilt.output[1].call_id == "call_one"
    assert rebuilt.output[1].arguments == '{"message":"done"}'
    assert rebuilt.usage.input_tokens_details.cached_tokens == 7
    assert rebuilt.usage.output_tokens_details.reasoning_tokens == 3
    assert rebuilt._hidden_params == {
        "additional_headers": {transport.COST_HEADER: "0.125"}
    }
    assert arm["api_key"] not in json.dumps(terminal)
    assert stream.closed and len(calls) == len(hooks) == 1


@pytest.mark.parametrize(
    "cost,expected",
    [
        (None, None),
        ("0", 0),
        ("NaN", None),
        ("-1", None),
        ("1e99", None),
        ("true", None),
    ],
)
def test_native_cost_header_is_not_accounting_authority(monkeypatch, cost, expected):
    terminal, *_ = execute_mock(
        monkeypatch, headers={} if cost is None else {"x-litellm-response-cost": cost}
    )
    assert terminal["kind"] == "result" and terminal["cost_usd"] == expected
    rebuilt = transport.rebuild_response(terminal)
    assert rebuilt._hidden_params == (
        {}
        if expected is None
        else {"additional_headers": {transport.COST_HEADER: "0.0"}}
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "BadRequestError"),
        (401, "AuthenticationError"),
        (429, "RateLimitError"),
        (500, "InternalServerError"),
        (504, "Timeout"),
    ],
)
def test_native_http_errors_have_actual_status_and_finite_diagnostics(
    monkeypatch, status, expected
):
    terminal, stream, calls, _ = execute_mock(
        monkeypatch,
        status=status,
        body={"error": {"message": "private provider body"}},
        headers={"x-request-id": "offline-real-http-status"},
    )
    assert terminal["kind"] == "error" and terminal["reason"] == "provider_error"
    assert terminal["error_type"] == expected
    details = terminal["diagnostics"]
    assert details["source"] == "helper" and details["http_status"] == status
    assert details["server_request_id"] == "offline-real-http-status"
    assert "private provider body" not in json.dumps(terminal)
    assert stream.closed and len(calls) == 1


@pytest.mark.parametrize(
    "error,phase",
    [
        (httpx.ConnectTimeout, "connect"),
        (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"),
        (httpx.PoolTimeout, "pool"),
        (httpx.ConnectError, "unknown"),
    ],
)
def test_native_transport_errors_never_manufacture_http_status_or_retry(
    monkeypatch, error, phase
):
    terminal, _, calls, _ = execute_mock(monkeypatch, error=error)
    assert terminal["kind"] == "error" and terminal["reason"] == "provider_error"
    assert terminal["diagnostics"]["transport_phase"] == phase
    assert terminal["diagnostics"]["http_status"] is None
    assert "private error" not in json.dumps(terminal)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "headers,status,reason",
    [
        (
            {"content-length": str(protocol.MAX_HTTP_BODY + 1)},
            200,
            "response_body_limit",
        ),
        ({"content-encoding": "gzip"}, 200, "content_encoding"),
        (
            [("content-encoding", "identity"), ("content-encoding", "identity")],
            200,
            "content_encoding",
        ),
        ({"content-length": "-1"}, 200, "content_length"),
        ([("content-length", "1"), ("content-length", "1")], 200, "content_length"),
        ({"location": "https://other.invalid"}, 302, "redirect"),
        ({"location": "https://other.invalid"}, 307, "redirect"),
        (
            {"x-large": "x" * (transport.MAX_HTTP_HEADER_BYTES + 1)},
            200,
            "response_headers",
        ),
        ([(f"x-{index}", "x") for index in range(65)], 200, "response_headers"),
    ],
)
def test_header_policy_rejects_before_stream_or_native_parser(
    monkeypatch, headers, status, reason
):
    terminal, stream, calls, _ = execute_mock(
        monkeypatch, headers=headers, status=status
    )
    assert terminal["kind"] == "error" and terminal["reason"] == reason
    assert terminal["diagnostics"]["http_status"] == status
    assert stream.reads == 0 and stream.closed and len(calls) == 1


def test_identity_body_exact_limit_then_overflow_before_json(monkeypatch):
    body = json.dumps(sample_response()).encode()
    exact = body + b" " * (protocol.MAX_HTTP_BODY - len(body))
    terminal, stream, *_ = execute_mock(monkeypatch, chunks=[exact])
    assert terminal["kind"] == "result" and stream.closed
    terminal, stream, calls, _ = execute_mock(
        monkeypatch, chunks=[exact, b" "], status=504
    )
    assert terminal["kind"] == "error" and terminal["reason"] == "response_body_limit"
    assert stream.reads == 2 and stream.closed and len(calls) == 1


def test_gzip_expansion_is_never_read_or_decompressed(monkeypatch):
    terminal, stream, *_ = execute_mock(
        monkeypatch,
        chunks=[gzip.compress(b"x" * (protocol.MAX_HTTP_BODY * 4))],
        headers={"content-encoding": "gzip"},
    )
    assert (
        terminal["reason"] == "content_encoding" and stream.reads == 0 and stream.closed
    )


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b'{"created_at":1,"created_at":2}', "response_json"),
        (b'{"id":"resp_one","created_at":true,"output":[]}', "response_schema"),
        (
            b'{"id":"resp_one","created_at":1,"output":[{"type":"web_search_call"}]}',
            "response_schema",
        ),
    ],
)
def test_raw_json_is_strict_before_litellm_model_construct_fallback(
    monkeypatch, raw, reason
):
    terminal, stream, calls, _ = execute_mock(monkeypatch, chunks=[raw])
    assert terminal["kind"] == "error" and terminal["reason"] == reason
    assert stream.closed and len(calls) == 1


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_dispatch_guard_failures_propagate_unchanged_with_zero_post(
    monkeypatch, exception
):
    marker = exception("controller guard failed")
    called = []
    monkeypatch.setattr(
        transport,
        "_new_http_transport",
        lambda httpx: httpx.MockTransport(lambda req: called.append(req)),
    )

    def abort():
        raise marker

    with pytest.raises(exception) as caught:
        transport.run_transport(sample_arm(), before_request=abort)
    assert caught.value is marker and called == []


def test_runtime_or_ambient_drift_never_constructs_a_client(monkeypatch):
    called = []
    monkeypatch.setattr(
        transport, "_new_http_transport", lambda httpx: called.append(1)
    )
    monkeypatch.setenv("HTTPS_PROXY", "private-must-not-echo")
    terminal = transport.run_transport(sample_arm(), before_request=lambda: None)
    assert terminal["reason"] == "internal" and called == []
    assert "private-must-not-echo" not in json.dumps(terminal)
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.setattr(
        transport.importlib.metadata, "version", lambda package: "future-version"
    )
    terminal = transport.run_transport(sample_arm(), before_request=lambda: None)
    assert terminal["reason"] == "internal" and called == []


def test_second_dispatch_and_url_change_never_reach_inner_transport():
    calls = []
    inner = httpx.MockTransport(
        lambda req: (
            calls.append(req),
            httpx.Response(200, content=b"ok", request=req),
        )[1]
    )
    guard = transport._guard_transport(
        httpx,
        inner,
        url=httpx.URL("https://offline.invalid/v1/responses"),
        secret="synthetic",
        before_request=lambda: None,
        observation={},
    )
    with httpx.Client(transport=guard, trust_env=False) as client:
        with pytest.raises(transport.TransportPolicyError):
            client.post("https://wrong.invalid/v1/responses")
        client.post("https://offline.invalid/v1/responses")
        with pytest.raises(transport.TransportPolicyError):
            client.post("https://offline.invalid/v1/responses")
    assert len(calls) == 1


def test_http_transport_default_ca_and_no_retry_or_proxy():
    import ssl

    native = transport._new_http_transport(httpx)
    try:
        pool = native._pool
        assert type(pool).__name__ == "ConnectionPool"
        assert (
            pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
            and pool._ssl_context.check_hostname
        )
        assert pool._http1 is True and pool._http2 is False and pool._retries == 0
    finally:
        native.close()


@pytest.mark.parametrize(
    "mode", ["success", "chunked_overflow", "gzip", "redirect", "headers"]
)
def test_real_loopback_httpcore_boundary(mode):
    """Real sockets exercise HTTPX/HTTPCore; they never contact a provider."""
    observations = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            observations.append(
                (
                    self.path,
                    self.headers.get("Accept-Encoding"),
                    self.headers.get("X-Client-Request-Id"),
                )
            )
            status = 307 if mode == "redirect" else 200
            self.send_response(status)
            self.send_header("Connection", "close")
            if mode == "redirect":
                self.send_header("Location", "https://must-never-follow.invalid/")
            if mode == "headers":
                self.send_header("X-Large", "x" * (transport.MAX_HTTP_HEADER_BYTES + 1))
            if mode == "gzip":
                self.send_header("Content-Encoding", "gzip")
            if mode == "chunked_overflow":
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                if mode == "chunked_overflow":
                    for _ in range(17):
                        chunk = b"x" * 65536
                        self.wfile.write(b"10000\r\n" + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                elif mode == "gzip":
                    self.wfile.write(gzip.compress(b"x" * (protocol.MAX_HTTP_BODY * 4)))
                else:
                    self.wfile.write(json.dumps(sample_response()).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        arm = sample_arm()
        arm["call"]["api_base"] = f"http://127.0.0.1:{server.server_port}/v1"
        terminal = transport.run_transport(arm, before_request=lambda: None)
        if mode == "success":
            assert terminal["kind"] == "result"
        else:
            expected = {
                "chunked_overflow": "response_body_limit",
                "gzip": "content_encoding",
                "redirect": "redirect",
                "headers": "response_headers",
            }[mode]
            assert terminal["kind"] == "error" and terminal["reason"] == expected
        assert observations == [("/v1/responses", "identity", CLIENT_ID)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_actual_pinned_sdk_text_and_finish_tool_kwargs_fit_contract(monkeypatch):
    # The SDK formats real message/tool/context data; no provider is called.
    import openhands.sdk.llm.llm as upstream
    from doonex.sdk import GuardedLLM
    from litellm.types.llms.openai import ResponsesAPIResponse
    from openhands.sdk.llm import Message, TextContent
    from openhands.sdk.llm.call_context import LLMCallContext
    from openhands.sdk.tool.builtins import FinishTool
    from pydantic import SecretStr

    captured = []

    def fixture(**kwargs):
        kwargs.pop("api_key")
        protocol.validate_call(
            kwargs, client_request_id=kwargs["extra_headers"]["X-Client-Request-Id"]
        )
        captured.append(copy.deepcopy(kwargs))
        return ResponsesAPIResponse.model_validate(sample_response())

    monkeypatch.setattr(upstream, "litellm_responses", fixture)
    llm = GuardedLLM(
        model="openai/gpt-5.1",
        api_mode="responses",
        api_key=SecretStr("offline-sdk-fixture"),
        base_url="https://offline.invalid/v1",
        timeout=2,
        num_retries=0,
        max_input_tokens=32768,
        max_output_tokens=1000,
        caching_prompt=False,
        fallback_strategy=None,
        openrouter_site_url="",
        openrouter_app_name="",
        log_completions=False,
        stream=False,
    )
    llm.bind(
        lambda *_: {"id": "44444444-4444-4444-8444-444444444444"},
        lambda *_: None,
        lambda: False,
    )
    llm.responses(
        [Message(role="user", content=[TextContent(text="offline SDK input")])],
        tools=FinishTool.create(),
        call_context=LLMCallContext(
            session_id="22222222-2222-4222-8222-222222222222",
            prompt_cache_key="22222222-2222-4222-8222-222222222222",
        ),
    )
    assert len(captured) == 1
    assert captured[0]["tools"][0]["name"] == "finish"
    assert captured[0].get("prompt_cache_retention") == "24h"


@pytest.mark.parametrize("usage", [None, "absent"])
def test_native_missing_usage_stays_unknown(monkeypatch, usage):
    body = sample_response()
    if usage == "absent":
        body.pop("usage")
    else:
        body["usage"] = usage
    terminal, *_ = execute_mock(monkeypatch, body=body)
    assert terminal["kind"] == "result"
    assert transport.rebuild_response(terminal).usage is None


def test_invalid_native_usage_is_rejected_before_pydantic_coercion(monkeypatch):
    body = sample_response()
    body["usage"]["input_tokens"] = True
    terminal, *_ = execute_mock(monkeypatch, body=body)
    assert terminal["kind"] == "error" and terminal["reason"] == "response_schema"


def test_request_id_echo_and_dynamic_exception_names_are_not_diagnostics(monkeypatch):
    arm = sample_arm()
    terminal, *_ = execute_mock(
        monkeypatch,
        arm=arm,
        status=500,
        body={"error": {"message": "offline"}},
        headers={"x-request-id": "prefix-" + arm["api_key"]},
    )
    assert terminal["diagnostics"]["server_request_id"] is None
    assert arm["api_key"] not in json.dumps(terminal)
    dynamic = type("private-secret-class", (Exception,), {})
    assert transport._safe_error_type(dynamic()) == "Exception"


def test_helper_error_class_allowlist_matches_existing_product_contract():
    from doonex import sdk

    assert protocol.ERROR_TYPES == sdk._ERROR_TYPES
