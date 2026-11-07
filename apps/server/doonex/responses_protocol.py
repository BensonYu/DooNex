"""Bounded JSON wire contract for the opt-in synchronous Responses helper.

This module must remain stdlib-only: bootstrap imports it before site packages.
Framing I/O, exact EOF, process exit/reap and frozen-model authorization belong
respectively to the process controller and parent SDK adapter.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import struct
from collections.abc import Mapping
from functools import wraps
from urllib.parse import urlsplit
from uuid import UUID

TRANSPORT = "isolated_direct_v1"
FRAME_LIMITS = {"ready": 4096, "arm": 1024 * 1024, "terminal": 2 * 1024 * 1024}
MAX_HTTP_BODY = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 65536
MAX_INT = 2**63 - 1
COMMON_FIELDS = frozenset(
    {"v", "transport", "kind", "attempt_id", "client_request_id", "nonce"}
)
IDENTITY_FIELDS = frozenset({"attempt_id", "client_request_id", "nonce"})
ERROR_TYPES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "AuthenticationError",
        "BadGatewayError",
        "BadRequestError",
        "BaseLLMException",
        "ConnectError",
        "ConnectTimeout",
        "ContextWindowExceededError",
        "Exception",
        "HTTPStatusError",
        "InternalServerError",
        "LLMAuthenticationError",
        "LLMBadRequestError",
        "LLMContentPolicyViolationError",
        "LLMContextWindowExceedError",
        "LLMMalformedConversationHistoryError",
        "LLMNoResponseError",
        "LLMRateLimitError",
        "LLMServiceUnavailableError",
        "LLMTimeoutError",
        "MaskedHTTPStatusError",
        "PoolTimeout",
        "RateLimitError",
        "ReadTimeout",
        "RuntimeError",
        "ServiceUnavailableError",
        "Timeout",
        "TimeoutError",
        "TypeError",
        "ValueError",
        "WriteTimeout",
    }
)
ERROR_REASONS = frozenset(
    {
        "provider_error",
        "response_body_limit",
        "response_headers",
        "content_encoding",
        "content_length",
        "redirect",
        "dispatch_policy",
        "response_json",
        "response_schema",
        "internal",
    }
)
TOOL_NAMES = frozenset({"doonex_shell", "finish"})
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
NETWORK_ENVIRONMENT = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_VERIFY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSL_CERTIFICATE",
        "SSL_SECURITY_LEVEL",
        "SSL_ECDH_CURVE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSLKEYLOGFILE",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT",
        "OPENAI_PROJECT_ID",
        "OPENAI_API_TYPE",
        "OPENAI_API_VERSION",
        "OPENAI_IDENTITY_PROVIDER_ID",
        "OPENAI_SERVICE_ACCOUNT_ID",
        "OPENAI_IDENTITY_TOKEN_FILE",
    }
)
CALL_REQUIRED = frozenset(
    {
        "model",
        "custom_llm_provider",
        "input",
        "instructions",
        "tools",
        "api_base",
        "api_version",
        "timeout",
        "drop_params",
        "seed",
        "stream",
        "max_retries",
        "num_retries",
        "use_chat_completions_api",
        "extra_headers",
        "max_output_tokens",
        "tool_choice",
        "store",
    }
)
CALL_OPTIONAL = frozenset(
    {"include", "reasoning", "prompt_cache_key", "prompt_cache_retention"}
)
RESPONSE_FIELDS = frozenset(
    {
        "id",
        "created_at",
        "error",
        "incomplete_details",
        "instructions",
        "metadata",
        "model",
        "object",
        "output",
        "parallel_tool_calls",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
        "max_output_tokens",
        "previous_response_id",
        "reasoning",
        "status",
        "text",
        "truncation",
        "usage",
        "user",
        "store",
    }
)


class ProtocolError(ValueError):
    """Only finite local messages escape; never format an untrusted value."""


def _validated(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ProtocolError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ProtocolError("Invalid isolated Responses structure") from None

    return guarded


def _require(condition: bool) -> None:
    if not condition:
        raise ProtocolError("Unsupported or invalid isolated Responses protocol")


def _fields(value, required, optional=frozenset()):
    _require(type(value) is dict)
    _require(required <= value.keys() <= required | optional)


def _int(value, lower=0, upper=MAX_INT):
    _require(type(value) is int and lower <= value <= upper)


def _string(value, limit=MAX_HTTP_BODY, *, empty=True):
    _require(type(value) is str and (empty or bool(value)))
    try:
        _require(len(value.encode("utf-8", "strict")) <= limit)
    except UnicodeError:
        raise ProtocolError("Invalid isolated Responses Unicode") from None


def _identifier(value, *, response_id=False):
    _string(value, 2048 if response_id else 512, empty=False)
    pattern = (
        r"[A-Za-z0-9][A-Za-z0-9._:/+=\-]*"
        if response_id
        else r"[A-Za-z0-9][A-Za-z0-9._:-]*"
    )
    _require(re.fullmatch(pattern, value) is not None)


def _uuid(value):
    _string(value, 36, empty=False)
    try:
        _require(str(UUID(value)) == value)
    except (ValueError, AttributeError):
        raise ProtocolError("Invalid isolated Responses identity") from None


@_validated
def validate_identity(value: dict) -> dict:
    _fields(value, IDENTITY_FIELDS)
    _uuid(value["attempt_id"])
    _uuid(value["client_request_id"])
    _string(value["nonce"], 64, empty=False)
    _require(re.fullmatch(r"[a-f0-9]{64}", value["nonce"]) is not None)
    return value


def _tree(value, *, depth=0, counter=None, limit=FRAME_LIMITS["terminal"]):
    # Count serialized bytes before json.dumps allocates a full encoded frame.
    # Controls, quotes and backslashes expand even with ensure_ascii=False.
    counter = [0, 0] if counter is None else counter
    counter[0] += 1
    _require(counter[0] <= MAX_JSON_NODES and depth <= MAX_JSON_DEPTH)
    kind = type(value)
    if kind is dict:
        cost = 2 + len(value) + max(0, len(value) - 1)
    elif kind is list:
        cost = 2 + max(0, len(value) - 1)
    elif kind is str:
        _string(value, min(limit, FRAME_LIMITS["terminal"]))
        cost = len(value.encode("utf-8")) + 2
        cost += sum(
            1 if char in '"\\\b\f\n\r\t' else 5 if ord(char) < 32 else 0
            for char in value
        )
    elif kind is int:
        _int(value, -MAX_INT)
        cost = len(str(value))
    elif kind is float:
        _require(math.isfinite(value))
        cost = len(str(value))
    else:
        _require(kind is bool or value is None)
        cost = 4 if value is None or value is True else 5
    counter[1] += cost
    _require(counter[1] <= limit)
    if kind is dict:
        for key, item in value.items():
            _require(type(key) is str)
            _tree(key, depth=depth + 1, counter=counter, limit=limit)
            _tree(item, depth=depth + 1, counter=counter, limit=limit)
    elif kind is list:
        for item in value:
            _tree(item, depth=depth + 1, counter=counter, limit=limit)


def strict_json_loads(payload: bytes, *, limit: int) -> object:
    """Bound bytes/depth before parsing, then reject duplicates and unsafe values."""
    _require(type(payload) is bytes and 0 < len(payload) <= limit)
    try:
        text = payload.decode("utf-8", "strict")
        quoted, escaped, depth = False, False, 0
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                _require(depth <= MAX_JSON_DEPTH)
            elif char in "]}":
                depth -= 1

        def unique(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result

        def integer(value):
            _require(len(value.lstrip("-")) <= 19)
            parsed = int(value)
            _int(parsed, -MAX_INT)
            return parsed

        def floating(value):
            parsed = float(value)
            _require(math.isfinite(parsed))
            return parsed

        def constant(_value):
            raise ProtocolError("Nonfinite isolated Responses JSON")

        result = json.loads(
            text,
            object_pairs_hook=unique,
            parse_int=integer,
            parse_float=floating,
            parse_constant=constant,
        )
        _tree(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid isolated Responses JSON") from None


def assert_direct_environment(environ: Mapping[str, str] | None = None) -> None:
    """Parent preflight: never print ambient proxy, certificate or CA values."""
    environment = os.environ if environ is None else environ
    if any(environment.get(name) for name in NETWORK_ENVIRONMENT):
        raise ProtocolError(
            "Isolated direct Responses rejects ambient network overrides"
        )


@_validated
def validate_api_base(value: str) -> str:
    _string(value, 4096, empty=False)
    _require("\\" not in value and "?" not in value and "#" not in value)
    _require(
        not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    )
    try:
        url = urlsplit(value)
        _require(
            bool(url.hostname)
            and url.username is None
            and url.password is None
            and not (url.query or url.fragment)
        )
        _ = url.port
        private = False
        try:
            address = ipaddress.ip_address(url.hostname)
            private = address.is_private or address in ipaddress.ip_network(
                "100.64.0.0/10"
            )
        except ValueError:
            pass
        _require(
            url.scheme == "https"
            or (
                url.scheme == "http"
                and (url.hostname in {"localhost", "127.0.0.1", "::1"} or private)
            )
        )
    except ValueError:
        raise ProtocolError("Invalid isolated direct Responses endpoint") from None
    # HTTP permission is independently checked against frozen ModelConfig by parent.
    return value


def _reasoning(value):
    _fields(value, {"effort"}, {"summary"})
    _require(type(value["effort"]) is str and value["effort"] in REASONING_EFFORTS)
    if "summary" in value:
        _require(
            type(value["summary"]) is str
            and value["summary"] in {"auto", "concise", "detailed"}
        )


def _text_parts(value, part_type):
    _require(type(value) is list and len(value) <= 4096)
    for part in value:
        _fields(part, {"type", "text"})
        _require(part["type"] == part_type)
        _string(part["text"])


def _reasoning_item(value):
    _fields(
        value, {"type", "id", "summary"}, {"content", "encrypted_content", "status"}
    )
    _identifier(value["id"])
    _text_parts(value["summary"], "summary_text")
    if value.get("content") is not None:
        _text_parts(value["content"], "reasoning_text")
    if value.get("encrypted_content") is not None:
        _string(value["encrypted_content"])
    if value.get("status") is not None:
        _require(value["status"] in {"completed", "incomplete"})


def _function_call(value, *, output=False):
    _fields(
        value,
        {"type", "id", "call_id", "name", "arguments"},
        {"status", "caller", "namespace"},
    )
    _require(value.get("caller") is None and value.get("namespace") is None)
    _identifier(value["id"])
    _identifier(value["call_id"])
    _require(type(value["name"]) is str and value["name"] in TOOL_NAMES)
    _string(value["arguments"])
    if output:
        # Explicitly incomplete calls never reach the parent SDK/tool dispatcher.
        _require(value.get("status") in {None, "completed"})
    elif value.get("status") is not None:
        _require(value["status"] == "completed")


def _inputs(value):
    if type(value) is str:
        _require(value == "")
        return
    _require(type(value) is list and len(value) <= 4096)
    for item in value:
        _require(type(item) is dict and type(item.get("type")) is str)
        match item["type"]:
            case "message":
                _fields(item, {"type", "role", "content"})
                _require(item["role"] in {"user", "assistant"})
                _text_parts(
                    item["content"],
                    "input_text" if item["role"] == "user" else "output_text",
                )
            case "function_call":
                _function_call(item)
            case "function_call_output":
                _fields(item, {"type", "call_id", "output"})
                _identifier(item["call_id"])
                _string(item["output"])
            case "reasoning":
                _reasoning_item(item)
            case _:
                raise ProtocolError("Unsupported isolated Responses input item")


def _tools(value):
    if value is None:
        return
    _require(type(value) is list and len(value) <= 2)
    names = set()
    for tool in value:
        _fields(tool, {"type", "name", "description", "parameters"}, {"strict"})
        _require(tool["type"] == "function" and type(tool["name"]) is str)
        _require(tool["name"] in TOOL_NAMES and tool["name"] not in names)
        names.add(tool["name"])
        _string(tool["description"])
        _require(type(tool["parameters"]) is dict)
        if "strict" in tool:
            _require(type(tool["strict"]) is bool)


@_validated
def validate_call(call: dict, *, client_request_id: str | None = None) -> dict:
    """Finite SDK kwargs; the parent must also compare its frozen configuration."""
    _tree(call)
    _fields(call, CALL_REQUIRED, CALL_OPTIONAL)
    _string(call["model"], 200, empty=False)
    _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]*", call["model"]) is not None)
    _require(call["custom_llm_provider"] == "openai")
    validate_api_base(call["api_base"])
    _int(call["timeout"], 1, 300)
    _int(call["max_output_tokens"], 128, 16384)
    for name in ("stream", "use_chat_completions_api", "store"):
        _require(call[name] is False)
    for name in ("max_retries", "num_retries"):
        _int(call[name], 0, 0)
    _require(
        call["drop_params"] is True
        and call["api_version"] is None
        and call["seed"] is None
    )
    _require(call["tool_choice"] == "auto")
    _inputs(call["input"])
    if call["instructions"] is not None:
        _string(call["instructions"])
    _tools(call["tools"])
    headers = call["extra_headers"]
    _fields(headers, {"X-Client-Request-Id"}, {"x-litellm-session-id"})
    _uuid(headers["X-Client-Request-Id"])
    if client_request_id is not None:
        _require(headers["X-Client-Request-Id"] == client_request_id)
    if "x-litellm-session-id" in headers:
        _uuid(headers["x-litellm-session-id"])
    if "include" in call:
        _require(
            call["include"] == [] or call["include"] == ["reasoning.encrypted_content"]
        )
    if "reasoning" in call:
        _reasoning(call["reasoning"])
    if "prompt_cache_key" in call:
        _uuid(call["prompt_cache_key"])
    if "prompt_cache_retention" in call:
        _require(call["prompt_cache_retention"] in {None, "in-memory", "24h"})
    return call


def project_cost(value) -> float | None:
    """Only the SDK's one private telemetry cost value crosses IPC."""
    if type(value) not in {int, float, str}:
        return None
    if type(value) is str and (
        len(value) > 64
        or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value)
        is None
    ):
        return None
    try:
        cost = float(value)
    except (ValueError, OverflowError):
        return None
    return cost if math.isfinite(cost) and 0 <= cost <= 1_000_000 else None


@_validated
def validate_diagnostics(value: dict, *, secret: str = "") -> dict:
    _fields(
        value,
        {
            "source",
            "exception_chain",
            "chain_truncated",
            "transport_phase",
            "http_status",
            "server_request_id",
        },
    )
    _require(value["source"] == "helper")
    chain = value["exception_chain"]
    _require(type(chain) is list and len(chain) <= 12)
    _require(all(type(name) is str and name in ERROR_TYPES for name in chain))
    _require(type(value["chain_truncated"]) is bool)
    _require(
        type(value["transport_phase"]) is str
        and value["transport_phase"] in {"connect", "read", "write", "pool", "unknown"}
    )
    if value["http_status"] is not None:
        _int(value["http_status"], 100, 599)
    request_id = value["server_request_id"]
    if request_id is not None:
        _string(request_id, 128, empty=False)
        _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", request_id) is not None)
        _require(not (secret and secret.casefold() in request_id.casefold()))
    return value


def _usage(value):
    if value is None:
        return
    _fields(
        value,
        {"input_tokens", "output_tokens", "total_tokens"},
        {"input_tokens_details", "output_tokens_details", "cost"},
    )
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        _int(value[key])
    _require(value["total_tokens"] == value["input_tokens"] + value["output_tokens"])
    for name, allowed, media in (
        (
            "input_tokens_details",
            {
                "cached_tokens",
                "text_tokens",
                "audio_tokens",
                "image_tokens",
                "video_tokens",
                "cached_tokens_details",
            },
            {"audio_tokens", "image_tokens", "video_tokens", "cached_tokens_details"},
        ),
        (
            "output_tokens_details",
            {"reasoning_tokens", "text_tokens", "audio_tokens"},
            {"audio_tokens"},
        ),
    ):
        if value.get(name) is None:
            continue
        details = value[name]
        _fields(details, set(), allowed)
        for key, count in details.items():
            if count is None:
                continue
            _require(key not in media)
            _int(count)
    if value.get("cost") is not None:
        _require(
            type(value["cost"]) in {int, float}
            and project_cost(value["cost"]) is not None
        )


@_validated
def validate_response(response: dict) -> dict:
    """Strict public subset before pinned Pydantic (which otherwise coerces)."""
    _tree(response)
    _fields(
        response,
        {"id", "created_at", "output"},
        RESPONSE_FIELDS - {"id", "created_at", "output"},
    )
    _identifier(response["id"], response_id=True)
    _int(response["created_at"])
    _require(response.get("error") is None)
    _require(response.get("object") in {None, "response"})
    _require(response.get("status") in {None, "completed", "incomplete"})
    _require(response.get("truncation") in {None, "auto", "disabled"})
    for name in ("instructions", "model", "previous_response_id", "user"):
        if response.get(name) is not None:
            _string(response[name])
    for name in ("parallel_tool_calls", "store"):
        if response.get(name) is not None:
            _require(type(response[name]) is bool)
    for name in ("temperature", "top_p"):
        if response.get(name) is not None:
            _require(
                type(response[name]) in {int, float}
                and math.isfinite(response[name])
                and 0 <= response[name] <= 2
            )
    if response.get("max_output_tokens") is not None:
        _int(response["max_output_tokens"], 1, 16384)
    if response.get("tool_choice") is not None:
        _require(response["tool_choice"] == "auto")
    if response.get("tools") is not None:
        _tools(response["tools"])
    if response.get("reasoning") is not None:
        value = response["reasoning"]
        _fields(value, set(), {"effort", "summary"})
        if value.get("effort") is not None:
            _require(value["effort"] in REASONING_EFFORTS)
        if value.get("summary") is not None:
            _require(value["summary"] in {"auto", "concise", "detailed"})
    if response.get("metadata") is not None:
        metadata = response["metadata"]
        _require(type(metadata) is dict and len(metadata) <= 16)
        for key, value in metadata.items():
            _string(key, 64)
            _string(value, 512)
    if response.get("text") is not None:
        _fields(response["text"], {"format"}, {"verbosity"})
        _fields(response["text"]["format"], {"type"})
        _require(response["text"]["format"]["type"] == "text")
        if "verbosity" in response["text"]:
            _require(response["text"]["verbosity"] in {None, "low", "medium", "high"})
    if response.get("incomplete_details") is not None:
        _fields(response["incomplete_details"], {"reason"})
        _require(
            response["incomplete_details"]["reason"]
            in {"max_output_tokens", "content_filter"}
        )
    _usage(response.get("usage"))
    output = response["output"]
    _require(type(output) is list and len(output) <= 4096)
    call_ids = set()
    for item in output:
        _require(type(item) is dict and type(item.get("type")) is str)
        match item["type"]:
            case "function_call":
                _function_call(item, output=True)
                _require(item["call_id"] not in call_ids)
                call_ids.add(item["call_id"])
            case "reasoning":
                _reasoning_item(item)
            case "message":
                _fields(item, {"type", "id", "role", "content"}, {"status"})
                _identifier(item["id"])
                _require(
                    item["role"] == "assistant"
                    and item.get("status") in {None, "completed", "incomplete"}
                )
                _require(type(item["content"]) is list and len(item["content"]) <= 4096)
                for part in item["content"]:
                    _fields(part, {"type", "text"}, {"annotations", "logprobs"})
                    _require(part["type"] == "output_text")
                    _string(part["text"])
                    for name in ("annotations", "logprobs"):
                        if name in part:
                            _require(part[name] == [] or part[name] is None)
            case _:
                raise ProtocolError("Unsupported isolated Responses output item")
    return response


def _envelope(value: dict, kind: str, identity: dict | None = None):
    _require(type(kind) is str and kind in FRAME_LIMITS)
    _require(type(value) is dict and COMMON_FIELDS <= value.keys())
    _require(
        type(value["v"]) is int and value["v"] == 1 and value["transport"] == TRANSPORT
    )
    actual_identity = {key: value[key] for key in IDENTITY_FIELDS}
    validate_identity(actual_identity)
    if identity is not None:
        validate_identity(identity)
        _require(actual_identity == identity)
    if kind == "ready":
        _fields(value, COMMON_FIELDS | {"pid", "pgid", "lease_dev", "lease_ino"})
        _require(value["kind"] == "ready")
        for name in ("pid", "pgid", "lease_ino"):
            _int(value[name], 1)
        _int(value["lease_dev"])
    elif kind == "arm":
        _fields(value, COMMON_FIELDS | {"deadline_monotonic_ns", "api_key", "call"})
        _require(value["kind"] == "arm")
        _int(value["deadline_monotonic_ns"], 1)
        key = value["api_key"]
        _string(key, 4096, empty=False)
        _require(all(33 <= ord(char) <= 126 for char in key))
        validate_call(value["call"], client_request_id=value["client_request_id"])
    elif value["kind"] == "result":
        _fields(value, COMMON_FIELDS | {"response", "cost_usd"})
        validate_response(value["response"])
        cost = value["cost_usd"]
        _require(
            cost is None
            or (type(cost) in {int, float} and project_cost(cost) is not None)
        )
    else:
        _fields(value, COMMON_FIELDS | {"error_type", "reason", "diagnostics"})
        _require(value["kind"] == "error")
        _require(
            type(value["error_type"]) is str and value["error_type"] in ERROR_TYPES
        )
        _require(type(value["reason"]) is str and value["reason"] in ERROR_REASONS)
        validate_diagnostics(value["diagnostics"])
    return value


@_validated
def encode_frame(envelope: dict, kind: str) -> bytes:
    _require(type(kind) is str and kind in FRAME_LIMITS)
    _tree(envelope, limit=FRAME_LIMITS[kind])
    _envelope(envelope, kind)
    try:
        payload = json.dumps(
            envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8", "strict")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid isolated Responses encoding") from None
    _require(len(payload) <= FRAME_LIMITS[kind])
    return struct.pack("!I", len(payload)) + payload


@_validated
def decode_frame_payload(
    payload: bytes, kind: str, identity: dict | None = None
) -> dict:
    _require(type(kind) is str and kind in FRAME_LIMITS)
    value = strict_json_loads(payload, limit=FRAME_LIMITS[kind])
    return _envelope(value, kind, identity)
