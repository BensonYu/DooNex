"""Child-owned, one-POST synchronous transport for isolated_direct_v1.

Import this module only after bootstrap ARM. Parent code may use rebuild_response
once a valid terminal, EOF, exit0 and actual child reap have been established.
The process controller owns deadlines, interruption, cleanup and dispatch fences.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import re
import sys
from collections.abc import Callable
from http.cookiejar import CookieJar, DefaultCookiePolicy

from .responses_protocol import (
    COMMON_FIELDS,
    ERROR_REASONS,
    ERROR_TYPES,
    MAX_HTTP_BODY,
    ProtocolError,
    assert_direct_environment,
    decode_frame_payload,
    encode_frame,
    project_cost,
    strict_json_loads,
    validate_diagnostics,
    validate_response,
)

PINNED_VERSIONS = {
    "openhands-sdk": "1.60.1",
    "litellm": "1.105.7",
    "openai": "2.54.0",
    "httpx": "0.28.1",
    "httpcore": "1.0.9",
    "pydantic": "2.14.3",
}
MAX_HTTP_HEADERS = 64
MAX_HTTP_HEADER_BYTES = 16 * 1024
CALLBACK_FIELDS = (
    "callbacks",
    "input_callback",
    "success_callback",
    "failure_callback",
    "service_callback",
    "audit_log_callbacks",
    "_async_input_callback",
    "_async_success_callback",
    "_async_failure_callback",
)
COST_HEADER = "llm_provider-x-litellm-response-cost"


class _BeforeRequestFailure(BaseException):
    def __init__(self, error):
        super().__init__("Isolated Responses dispatch guard failed")
        self.error = error


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class TransportPolicyError(BaseException):
    """A finite child-local policy failure bypasses native LiteLLM retries."""

    def __init__(self, reason: str):
        if reason not in ERROR_REASONS:
            raise ProtocolError("Invalid isolated Responses transport reason")
        super().__init__("Isolated Responses transport policy rejected the response")
        self.reason = reason


def assert_runtime_versions() -> None:
    if sys.version_info[:2] != (3, 12) or any(
        importlib.metadata.version(package) != expected
        for package, expected in PINNED_VERSIONS.items()
    ):
        raise ProtocolError("Isolated Responses requires the pinned runtime")


def _safe_error_type(error: BaseException) -> str:
    name = type.__dict__["__name__"].__get__(type(error))
    return name if name in ERROR_TYPES else "Exception"


def _safe_request_id(value, secret: str) -> str | None:
    if (
        type(value) is str
        and len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value)
        and not (secret and secret.casefold() in value.casefold())
    ):
        return value
    return None


def _diagnostics(error, observation, secret, httpx):
    result = {
        "source": "helper",
        "exception_chain": [],
        "chain_truncated": False,
        "transport_phase": "unknown",
        "http_status": observation.get("http_status"),
        "server_request_id": observation.get("server_request_id"),
    }
    queue, seen, phases = [error], set(), set()
    while queue and len(seen) < 12:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        result["exception_chain"].append(_safe_error_type(current))
        for kind, phase in (
            (httpx.ConnectTimeout, "connect"),
            (httpx.ReadTimeout, "read"),
            (httpx.WriteTimeout, "write"),
            (httpx.PoolTimeout, "pool"),
        ):
            if isinstance(current, kind):
                phases.add(phase)
        # Do not format or use arbitrary exception attributes/properties.
        for name in ("__cause__", "__context__"):
            linked = BaseException.__dict__[name].__get__(current)
            if linked is not None and id(linked) not in seen:
                queue.append(linked)
    result["chain_truncated"] = any(id(item) not in seen for item in queue)
    if len(phases) == 1:
        result["transport_phase"] = phases.pop()
    return validate_diagnostics(result, secret=secret)


def _new_http_transport(httpx):
    return httpx.HTTPTransport(
        verify=True,
        trust_env=False,
        http1=True,
        http2=False,
        retries=0,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )


def _guard_transport(httpx, inner, *, url, secret, before_request, observation):
    class BoundedStream(httpx.SyncByteStream):
        def __init__(self, stream):
            self.stream = stream
            self.total = 0

        def __iter__(self):
            try:
                for chunk in self.stream:
                    self.total += len(chunk)
                    if self.total > MAX_HTTP_BODY:
                        raise TransportPolicyError("response_body_limit")
                    yield chunk
            finally:
                self.stream.close()

        def close(self):
            self.stream.close()

    class DirectTransport(httpx.BaseTransport):
        def __init__(self):
            self.calls = 0

        def handle_request(self, request):
            if self.calls or request.method != "POST" or request.url != url:
                raise TransportPolicyError("dispatch_policy")
            request.headers["Accept-Encoding"] = "identity"
            # Nothing can rearm the process alarm or extend the frozen deadline.
            # Hook failure propagates without becoming a provider retry/error.
            try:
                before_request()
            except BaseException as error:  # noqa: BLE001 -- preserve controller signals through LiteLLM
                raise _BeforeRequestFailure(error) from None
            self.calls += 1
            response = inner.handle_request(request)
            observation["http_status"] = response.status_code
            try:
                raw = response.headers.raw
                if (
                    len(raw) > MAX_HTTP_HEADERS
                    or sum(len(key) + len(value) for key, value in raw)
                    > MAX_HTTP_HEADER_BYTES
                ):
                    raise TransportPolicyError("response_headers")
                request_ids = response.headers.get_list("x-request-id")
                if len(request_ids) == 1:
                    observation["server_request_id"] = _safe_request_id(
                        request_ids[0], secret
                    )
                if 300 <= response.status_code <= 399:
                    raise TransportPolicyError("redirect")
                encodings = response.headers.get_list("content-encoding")
                if encodings and (
                    len(encodings) != 1 or encodings[0].strip().lower() != "identity"
                ):
                    raise TransportPolicyError("content_encoding")
                lengths = response.headers.get_list("content-length")
                if lengths:
                    if (
                        len(lengths) != 1
                        or re.fullmatch(r"[0-9]{1,10}", lengths[0]) is None
                    ):
                        raise TransportPolicyError("content_length")
                    if int(lengths[0]) > MAX_HTTP_BODY:
                        raise TransportPolicyError("response_body_limit")
                response.stream = BoundedStream(response.stream)
                return response
            except BaseException:
                response.close()
                raise

        def close(self):
            inner.close()

    return DirectTransport()


def _empty_diagnostics():
    return {
        "source": "helper",
        "exception_chain": [],
        "chain_truncated": False,
        "transport_phase": "unknown",
        "http_status": None,
        "server_request_id": None,
    }


def _error(common, *, reason, error_type="Exception", diagnostics=None):
    result = {
        **common,
        "kind": "error",
        "error_type": error_type,
        "reason": reason,
        "diagnostics": diagnostics if diagnostics is not None else _empty_diagnostics(),
    }
    encode_frame(result, "terminal")
    return result


def rebuild_response(terminal: dict):
    """Parent calls only after process success; repeat strict public validation."""
    terminal = decode_frame_payload(encode_frame(terminal, "terminal")[4:], "terminal")
    if terminal["kind"] != "result":
        raise ProtocolError("A helper error cannot be rebuilt as a response")
    assert_runtime_versions()
    from litellm.types.llms.openai import ResponsesAPIResponse

    response = ResponsesAPIResponse.model_validate(terminal["response"])
    # model_dump does not serialize PrivateAttr. Restore the one SDK cost key.
    cost = terminal["cost_usd"]
    response._hidden_params = (
        {"additional_headers": {COST_HEADER: str(project_cost(cost))}}
        if cost is not None
        else {}
    )
    return response


def run_transport(arm: dict, *, before_request: Callable[[], None]) -> dict:
    """One native sync Responses call; no automatic retry, redirect or fallback."""
    # Already validated by bootstrap; repeat before importing network dependencies.
    encode_frame(arm, "arm")
    common = {key: arm[key] for key in COMMON_FIELDS if key != "kind"}
    try:
        assert_direct_environment()
        assert_runtime_versions()
        # Set these before importing LiteLLM: no dotenv, remote cost map or OTEL.
        os.environ["LITELLM_MODE"] = "PRODUCTION"
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        os.environ["OTEL_SDK_DISABLED"] = "true"
        import httpx
        import litellm
        from litellm.llms.custom_httpx.http_handler import HTTPHandler
        from litellm.responses.main import responses
        from litellm.types.llms.openai import ResponsesAPIResponse

        if any(getattr(litellm, name, ()) for name in CALLBACK_FIELDS):
            raise ProtocolError("Isolated Responses rejects runtime callbacks")
        logging.disable(logging.CRITICAL)
        litellm.suppress_debug_info = True
        litellm.redact_messages_in_exceptions = True
    except Exception:  # noqa: BLE001 -- startup failures have finite, secret-free diagnostics
        return _error(common, reason="internal")

    class StrictHTTPHandler(HTTPHandler):
        def post(self, *args, **kwargs):
            response = super().post(*args, **kwargs)
            try:
                body = strict_json_loads(response.content, limit=MAX_HTTP_BODY)
            except ProtocolError:
                raise TransportPolicyError("response_json") from None
            try:
                validate_response(body)
            except ProtocolError:
                raise TransportPolicyError("response_schema") from None
            return response

    observation = {}
    secret = arm["api_key"]
    try:
        inner = _new_http_transport(httpx)
        transport = _guard_transport(
            httpx,
            inner,
            url=httpx.URL(arm["call"]["api_base"].rstrip("/") + "/responses"),
            secret=secret,
            before_request=before_request,
            observation=observation,
        )
        with httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            http2=False,
            timeout=arm["call"]["timeout"],
            cookies=CookieJar(policy=_NoCookies()),
        ) as client:
            response = responses(
                **arm["call"],
                api_key=secret,
                client=StrictHTTPHandler(client=client),
            )
        if type(response) is not ResponsesAPIResponse:
            raise TransportPolicyError("response_schema")
        public = response.model_dump(mode="json")
        try:
            validate_response(public)
            # Never permit LiteLLM's model_construct fallback to become success.
            ResponsesAPIResponse.model_validate(public)
        except (ProtocolError, ValueError, TypeError):
            raise TransportPolicyError("response_schema") from None
        hidden = response._hidden_params
        headers = hidden.get("additional_headers") if type(hidden) is dict else None
        cost = project_cost(headers.get(COST_HEADER)) if type(headers) is dict else None
        terminal = {**common, "kind": "result", "response": public, "cost_usd": cost}
        try:
            encode_frame(terminal, "terminal")
        except ProtocolError:
            raise TransportPolicyError("response_schema") from None
        return terminal
    except _BeforeRequestFailure as error:
        raise error.error from None
    except TransportPolicyError as error:
        return _error(
            common,
            reason=error.reason,
            diagnostics=_diagnostics(error, observation, secret, httpx),
        )
    except Exception as error:  # noqa: BLE001 -- providers expose heterogeneous exceptions
        return _error(
            common,
            reason="provider_error",
            error_type=_safe_error_type(error),
            diagnostics=_diagnostics(error, observation, secret, httpx),
        )
