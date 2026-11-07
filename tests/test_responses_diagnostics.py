"""Pinned native Responses diagnostics; all HTTP traffic is MockTransport."""

import json
import socket
from uuid import UUID

import httpx
import pytest
from doonex import sdk
from doonex.worker import Worker, run_directory
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from openhands.sdk.llm import Message, TextContent
from pydantic import SecretStr
from test_sdk import responses_answer
from test_worker import prepare_run

SECRET = "DOONEX_SYNTHETIC_DIAGNOSTIC_KEY_9182"
PROMPT = "PRIVATE_SYNTHETIC_PROMPT_MUST_NOT_ENTER_DIAGNOSTICS"
ACTION = "b740e918-d026-4dd9-88e5-9508c7a00a49"
CASES = ["connect", "read", "write", "pool", "http504", "ordinary"]
TIMEOUTS = {
    "connect": httpx.ConnectTimeout,
    "read": httpx.ReadTimeout,
    "write": httpx.WriteTimeout,
    "pool": httpx.PoolTimeout,
}


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Diagnostics contracts must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def failure(case, request, request_id="server-request-504"):
    if case in TIMEOUTS:
        raise TIMEOUTS[case](
            f"{SECRET} {PROMPT} https://private.invalid", request=request
        )
    if case.startswith("http"):
        return httpx.Response(
            int(case.removeprefix("http")),
            request=request,
            headers={"x-request-id": request_id, "x-private-header": SECRET},
            json={"error": {"message": f"{SECRET} {PROMPT}", "type": "server_error"}},
        )
    raise RuntimeError(f"{SECRET} {PROMPT} https://private.invalid")


def guarded():
    settlements = []
    llm = sdk.GuardedLLM(
        model="openai/gpt-4.1-mini",
        api_mode="responses",
        api_key=SecretStr(SECRET),
        base_url="https://offline.invalid/v1",
        num_retries=0,
        caching_prompt=False,
        fallback_strategy=None,
        log_completions=False,
        stream=False,
        timeout=10,
        max_input_tokens=100000,
        max_output_tokens=1000,
        extra_headers={"x-client-request-id": "untrusted-old-value"},
    )
    llm.bind(
        lambda *_: {"id": ACTION},
        lambda reservation, usage: settlements.append((reservation, usage)),
        lambda: False,
    )
    return llm, settlements


@pytest.mark.parametrize("case", [*CASES, "http408"])
def test_native_responses_failure_preserves_only_observed_diagnostics(case):
    requests = []

    def handle(request):
        requests.append(request)
        return failure(case, request)

    llm, settlements = guarded()
    with (
        httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client,
        pytest.raises(sdk.ModelCallError) as caught,
    ):
        llm.responses(
            messages=[Message(role="user", content=[TextContent(text=PROMPT)])],
            client=HTTPHandler(client=client),
            extra_headers={"X-CLIENT-REQUEST-ID": "injected-old-value"},
        )
    assert len(requests) == 1
    headers = requests[0].headers
    assert len(headers.get_list("x-client-request-id")) == 1
    identifier = headers["x-client-request-id"]
    assert str(UUID(identifier)) == identifier
    diagnostic = caught.value.diagnostics
    assert diagnostic["client_request_id"] == identifier
    assert diagnostic["action_id"] == ACTION
    assert diagnostic["transport_phase"] == (case if case in TIMEOUTS else "unknown")
    assert diagnostic["http_status"] == (
        int(case.removeprefix("http")) if case.startswith("http") else None
    )
    assert diagnostic["server_request_id"] == (
        "server-request-504" if case.startswith("http") else None
    )
    assert diagnostic["timeout_seconds"] == 10
    assert diagnostic["elapsed_ms"] >= 0
    assert len(diagnostic["exception_chain"]) <= 12
    assert not diagnostic["chain_truncated"]
    assert settlements == [({"id": ACTION}, None)]
    saved = json.dumps(diagnostic)
    assert all(
        value not in saved for value in (SECRET, PROMPT, "https://", "x-private-header")
    )
    assert SECRET not in str(caught.value)
    if case in TIMEOUTS:
        assert caught.value.provider_error_type == "LLMTimeoutError"
        assert TIMEOUTS[case].__name__ in diagnostic["exception_chain"]


def test_successive_success_and_failure_have_distinct_ids_without_replay():
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                request=request,
                json=responses_answer("finish", {"message": "done"}).model_dump(
                    mode="json"
                ),
            )
        return failure("read", request)

    llm, settlements = guarded()
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        kwargs = {
            "messages": [Message(role="user", content=[TextContent(text="offline")])],
            "client": HTTPHandler(client=client),
        }
        llm.responses(**kwargs)
        with pytest.raises(sdk.ModelCallError):
            llm.responses(**kwargs)
    assert len(requests) == len(settlements) == 2
    identifiers = [request.headers["x-client-request-id"] for request in requests]
    assert len(set(identifiers)) == 2
    assert all(UUID(value).version == 4 for value in identifiers)
    assert settlements[0][1] is not None and settlements[1][1] is None


@pytest.mark.parametrize("headers", ["not-a-mapping", {99: "invalid-key"}, [], "", 0])
def test_header_preparation_failure_does_not_reserve_or_dispatch(headers):
    llm, _ = guarded()
    reservations, settlements, requests = [], [], []
    llm.bind(
        lambda *args: reservations.append(args),
        lambda *args: settlements.append(args),
        lambda: False,
    )
    with pytest.raises(ValueError, match="Responses headers"):
        llm._guarded_request(
            lambda **kwargs: requests.append(kwargs),
            "responses",
            [],
            None,
            {"extra_headers": headers},
        )
    assert reservations == settlements == requests == []


def test_diagnostic_failure_preserves_provider_error_and_unknown_usage(monkeypatch):
    llm, settlements = guarded()

    def broken(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(sdk, "_provider_diagnostics", broken)
    with pytest.raises(sdk.ModelCallError) as caught:
        llm._guarded_request(broken, "responses", [], None, {})
    assert caught.value.provider_error_type == "RuntimeError"
    assert caught.value.diagnostics["diagnostic_unavailable"]
    assert caught.value.diagnostics["transport_phase"] == "unknown"
    assert settlements == [({"id": ACTION}, None)]
    assert SECRET not in str(caught.value)
    assert SECRET not in json.dumps(caught.value.diagnostics)


@pytest.mark.parametrize(
    "value",
    [
        SECRET,
        SECRET.lower(),
        "prefix-" + SECRET,
        "a" * 129,
        "bad\nheader",
        "has space",
        "非ASCII",
        "https://private.invalid",
        [],
        99,
        None,
        "",
    ],
)
def test_request_id_rejects_secrets_and_unbounded_or_non_identifier_values(value):
    error = RuntimeError(SECRET)
    error.headers = {"x-request-id": value}
    assert sdk._provider_diagnostics(error, SECRET)["server_request_id"] is None


def test_request_id_reads_exception_headers_even_with_empty_synthetic_response():
    error = RuntimeError(SECRET)
    error.headers = {"X-Request-ID": "safe-server-request:123"}
    error.response = httpx.Response(408)
    result = sdk._provider_diagnostics(error, SECRET)
    assert result["server_request_id"] == "safe-server-request:123"
    assert result["http_status"] is None


@pytest.mark.parametrize("header_type", [dict, httpx.Headers])
def test_request_id_header_search_is_bounded(header_type):
    headers = {f"ignored-{index}": "safe" for index in range(100)}
    headers["x-request-id"] = "unvisited-id"
    error = RuntimeError(SECRET)
    error.headers = header_type(headers)
    assert sdk._provider_diagnostics(error, SECRET)["server_request_id"] is None


def test_conflicting_transport_phases_remain_unknown():
    error = httpx.ReadTimeout(SECRET)
    error.__cause__ = httpx.ConnectTimeout(SECRET)
    assert sdk._provider_diagnostics(error, SECRET)["transport_phase"] == "unknown"


def test_diagnostics_bound_cycles_depth_names_and_ignore_hostile_properties():
    class Hostile(RuntimeError):
        @property
        def response(self):
            pytest.fail("Must not evaluate exception response properties")

        @property
        def headers(self):
            pytest.fail("Must not evaluate exception header properties")

        def __str__(self):
            raise AssertionError("Must not format provider errors")

    error = Hostile()
    error.__context__ = error
    assert sdk._provider_diagnostics(error, SECRET)["exception_chain"] == ["Exception"]
    current = error
    for _ in range(30):
        next_error = type(SECRET, (RuntimeError,), {"__module__": PROMPT})(SECRET)
        current.__cause__ = next_error
        current = next_error
    current.__cause__ = error
    result = sdk._provider_diagnostics(error, SECRET)
    assert len(result["exception_chain"]) == 12 and result["chain_truncated"]
    assert SECRET not in json.dumps(result)
    assert PROMPT not in json.dumps(result)


@pytest.mark.docker
@pytest.mark.parametrize("case", CASES)
def test_worker_persists_private_failure_event_and_unknown_reservation(
    settings, store, request_data, tmp_path, monkeypatch, case
):
    import litellm.llms.custom_httpx.llm_http_handler as handler_module

    requests = []
    api_key = settings.credential(request_data["model"])

    def handle(request):
        requests.append(request)
        return failure(
            case,
            request,
            request_id=api_key if case == "http504" else "server-safe-id",
        )

    request_data["model"].update(
        api_mode="responses", base_url="https://offline.invalid/v1"
    )
    run, _, _ = prepare_run(store, settings, request_data, tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        handler = HTTPHandler(client=client)
        monkeypatch.setattr(handler_module, "_get_httpx_client", lambda **_: handler)
        Worker(settings).run(once=True)
    final = store.get(run["id"])
    events = store.events(run["id"])["events"]
    failures = [
        event["payload"] for event in events if event["kind"] == "model_request_failed"
    ]
    assert len(requests) == len(failures) == 1
    assert final["state"] == "failed"
    assert final["usage"]["calls"] == final["usage"]["unknown_calls"] == 1
    assert final["usage"]["tokens"] == 0 and final["usage"]["reserved_tokens"] > 0
    diagnostic = failures[0]
    assert diagnostic["client_request_id"] == requests[0].headers["x-client-request-id"]
    with store.connect() as connection:
        actions = connection.execute(
            "SELECT * FROM actions WHERE run_id=%s", (run["id"],)
        ).fetchall()
    action = next(action for action in actions if action["kind"] == "model")
    assert diagnostic["action_id"] == str(action["id"])
    assert action["status"] == "unknown"
    assert diagnostic["transport_phase"] == (case if case in TIMEOUTS else "unknown")
    assert diagnostic["server_request_id"] is None
    persisted = json.dumps((final, events, actions), default=str)
    persisted += "".join(
        path.read_text() for path in run_directory(settings, run["id"]).rglob("*.json")
    )
    assert all(
        value not in persisted
        for value in (SECRET, PROMPT, api_key, "private.invalid", "x-private-header")
    )
