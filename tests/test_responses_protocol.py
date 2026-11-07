"""No models, database or network: strict opt-in helper wire contract."""

import copy
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from doonex import responses_protocol as protocol

CLIENT_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"


def sample_response():
    return {
        "id": "resp_offline",
        "created_at": 1,
        "object": "response",
        "model": "gpt-4.1-mini",
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_one",
                "summary": [],
                "encrypted_content": "opaque-offline-encrypted-reasoning",
            },
            {
                "type": "function_call",
                "id": "fc_one",
                "call_id": "call_one",
                "name": "finish",
                "arguments": '{"message":"done"}',
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


def sample_arm():
    return {
        "v": 1,
        "transport": protocol.TRANSPORT,
        "kind": "arm",
        "attempt_id": "33333333-3333-4333-8333-333333333333",
        "client_request_id": CLIENT_ID,
        "nonce": "a" * 64,
        "deadline_monotonic_ns": 100000000000,
        "api_key": "offline-synthetic-bearer",
        "call": {
            "model": "gpt-4.1-mini",
            "custom_llm_provider": "openai",
            "api_base": "https://offline.invalid/v1",
            "api_version": None,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "private offline fixture"}
                    ],
                }
            ],
            "instructions": "private synthetic instructions",
            "tools": None,
            "timeout": 2,
            "drop_params": True,
            "seed": None,
            "stream": False,
            "max_retries": 0,
            "num_retries": 0,
            "use_chat_completions_api": False,
            "extra_headers": {
                "X-Client-Request-Id": CLIENT_ID,
                "x-litellm-session-id": SESSION_ID,
            },
            "max_output_tokens": 1000,
            "tool_choice": "auto",
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": "high"},
            "prompt_cache_key": SESSION_ID,
        },
    }


def common(arm=None):
    arm = sample_arm() if arm is None else arm
    return {key: arm[key] for key in protocol.COMMON_FIELDS if key != "kind"}


def diagnostics():
    return {
        "source": "helper",
        "exception_chain": ["Timeout", "ReadTimeout"],
        "chain_truncated": False,
        "transport_phase": "read",
        "http_status": None,
        "server_request_id": "offline-request-id",
    }


def test_bootstrap_protocol_import_needs_no_site_packages():
    source = Path(protocol.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('wire',{str(source)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not any(name.split('.')[0] in {'httpx','litellm','pydantic','openhands'} for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        timeout=5,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ready_arm_terminal_identity_roundtrips():
    arm = sample_arm()
    ready = {
        **common(),
        "kind": "ready",
        "pid": 123,
        "pgid": 123,
        "lease_dev": 1,
        "lease_ino": 2,
    }
    terminal = {
        **common(),
        "kind": "result",
        "response": sample_response(),
        "cost_usd": 0,
    }
    error = {
        **common(),
        "kind": "error",
        "error_type": "Timeout",
        "reason": "provider_error",
        "diagnostics": diagnostics(),
    }
    identity = {key: arm[key] for key in protocol.IDENTITY_FIELDS}
    for kind, value in [
        ("ready", ready),
        ("arm", arm),
        ("terminal", terminal),
        ("terminal", error),
    ]:
        encoded = protocol.encode_frame(value, kind)
        assert struct.unpack("!I", encoded[:4])[0] == len(encoded) - 4
        assert protocol.decode_frame_payload(encoded[4:], kind, identity) == value
    identity["nonce"] = "b" * 64
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_frame_payload(
            protocol.encode_frame(arm, "arm")[4:], "arm", identity
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e10000}',
        b'{"x":9223372036854775808}',
        b"[" * 33 + b"0" + b"]" * 33,
        b'{"x":"\xff"}',
        b'{"x":"\\ud800"}',
        b"{} {}",
        b"\xef\xbb\xbf{}",
        b" " * (protocol.FRAME_LIMITS["arm"] + 1),
        b"[" + b"0," * 65536 + b"0]",
    ],
)
def test_strict_parser_rejects_before_unbounded_or_ambiguous_processing(raw):
    with pytest.raises(protocol.ProtocolError):
        protocol.strict_json_loads(raw, limit=protocol.FRAME_LIMITS["arm"])


@pytest.mark.parametrize(
    "change",
    [
        {"v": True},
        {"v": 2},
        {"nonce": "A" * 64},
        {"attempt_id": "33333333333343338333333333333333"},
        {"kind": "result"},
        {"key-like-secret-must-not-echo": "sensitive"},
        {"deadline_monotonic_ns": True},
        {"api_key": "bad\nkey"},
    ],
)
def test_envelope_rejects_unknown_or_wrong_scalar_without_echo(change):
    arm = sample_arm()
    arm.update(change)
    with pytest.raises(protocol.ProtocolError) as caught:
        protocol.encode_frame(arm, "arm")
    assert "sensitive" not in str(caught.value)
    assert "key-like" not in str(caught.value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("timeout", True),
        ("max_retries", False),
        ("num_retries", 1),
        ("stream", True),
        ("store", True),
        ("drop_params", False),
        ("use_chat_completions_api", True),
        ("api_version", "future"),
        ("extra_body", {}),
        ("client", object()),
        ("callbacks", []),
        ("seed", 7),
        ("reasoning", {"effort": "future"}),
        (
            "input",
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "https://example.invalid"}
                    ],
                }
            ],
        ),
        ("tools", [{"type": "web_search"}]),
    ],
)
def test_call_rejects_implicit_behavior_and_unsupported_shapes(name, value):
    arm = sample_arm()
    arm["call"][name] = value
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame(arm, "arm")


def test_frozen_headers_cannot_change_identity_or_authorization():
    for headers in [
        {"X-Client-Request-Id": SESSION_ID},
        {"x-client-request-id": CLIENT_ID},
        {"X-Client-Request-Id": CLIENT_ID, "Authorization": "Bearer synthetic"},
        {"X-Client-Request-Id": CLIENT_ID, "x-litellm-session-id": "not-a-uuid"},
    ]:
        arm = sample_arm()
        arm["call"]["extra_headers"] = headers
        with pytest.raises(protocol.ProtocolError):
            protocol.encode_frame(arm, "arm")


def test_actual_continuation_data_and_cache_retention_are_preserved():
    arm = sample_arm()
    arm["call"]["prompt_cache_retention"] = "24h"
    arm["call"]["tools"] = [
        {
            "type": "function",
            "name": "finish",
            "description": "Finish",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
            "strict": False,
        }
    ]
    arm["call"]["input"] += copy.deepcopy(sample_response()["output"])
    arm["call"]["input"].append(
        {"type": "function_call_output", "call_id": "call_one", "output": "done"}
    )
    wire = protocol.encode_frame(arm, "arm")
    assert protocol.decode_frame_payload(wire[4:], "arm") == arm


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.invalid/v1",
        "https://@example.invalid/v1",
        "https://example.invalid/v1?",
        "https://example.invalid/v1#",
        "https://example.invalid/\\bad",
        "https://example.invalid/v1?key=value",
        "https://example.invalid/#x",
        "https://example.invalid/ white",
        "http://public.invalid/v1",
        "http://8.8.8.8/v1",
        "https://example.invalid:bad/v1",
    ],
)
def test_endpoint_rejects_unsupported_network_semantics(url):
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_api_base(url)


@pytest.mark.parametrize("name", sorted(protocol.NETWORK_ENVIRONMENT))
def test_ambient_network_override_preflight_never_echoes_value(name):
    with pytest.raises(protocol.ProtocolError) as caught:
        protocol.assert_direct_environment({name: "DO_NOT_PRINT_PRIVATE_OVERRIDE"})
    assert "DO_NOT_PRINT" not in str(caught.value)
    protocol.assert_direct_environment({name: ""})


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        -1,
        float("nan"),
        float("inf"),
        "nan",
        "inf",
        "-1",
        "1e99",
        "secret=1",
        "1" * 65,
        {},
        [],
        1000001,
    ],
)
def test_invalid_cost_remains_unknown(value):
    assert protocol.project_cost(value) is None


@pytest.mark.parametrize(
    "value,expected",
    [(0, 0.0), ("0", 0.0), (0.125, 0.125), ("1.25e-1", 0.125), (1000000, 1000000.0)],
)
def test_only_finite_bounded_cost_is_projected(value, expected):
    assert protocol.project_cost(value) == expected


def test_missing_usage_keeps_unknown_and_incomplete_or_coercible_usage_rejects():
    response = sample_response()
    response.pop("usage")
    protocol.validate_response(response)
    response["usage"] = None
    protocol.validate_response(response)
    for usage in [
        {"input_tokens": 1},
        {"input_tokens": True, "output_tokens": 0, "total_tokens": 1},
        {"input_tokens": 1, "output_tokens": 2, "total_tokens": 4},
    ]:
        response["usage"] = usage
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_response(response)


@pytest.mark.parametrize(
    "field,value",
    [
        ("created_at", 1.0),
        ("id", []),
        ("status", []),
        ("error", {"message": "private"}),
        ("unknown", True),
        ("output", [{"type": "web_search_call"}]),
    ],
)
def test_response_has_no_pydantic_coercion_or_arbitrary_dict_escape(field, value):
    response = sample_response()
    response[field] = value
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_response(response)


def test_incomplete_or_duplicate_executable_calls_are_rejected():
    response = sample_response()
    response["output"][-1]["status"] = "incomplete"
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_response(response)
    response["output"][-1]["status"] = "completed"
    response["output"].append(copy.deepcopy(response["output"][-1]))
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_response(response)


def test_diagnostics_is_finite_typed_and_secret_checked():
    for change in [
        {"raw_message": "sensitive"},
        {"http_status": True},
        {"http_status": 600},
        {"transport_phase": "maybe"},
        {"exception_chain": ["SecretDynamicError"]},
        {"server_request_id": "prefix-secret-suffix"},
    ]:
        value = diagnostics()
        value.update(change)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_diagnostics(value, secret="secret")
    assert protocol.validate_diagnostics(diagnostics())["source"] == "helper"


def test_frame_budget_rejects_large_payload_before_serialization():
    arm = sample_arm()
    arm["call"]["instructions"] = "a" * protocol.FRAME_LIMITS["arm"]
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame(arm, "arm")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_frame_payload(json.dumps(arm).encode(), "arm")


@pytest.mark.parametrize("text", ["x" * 600000, "\x00" * 100000])
def test_cumulative_and_escaped_frame_bytes_are_rejected_before_dumps(
    monkeypatch, text
):
    arm = sample_arm()
    arm["call"]["input"][0]["content"][0]["text"] = text
    arm["call"]["instructions"] = text

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized frame must be rejected before full JSON serialization")

    monkeypatch.setattr(protocol.json, "dumps", forbidden)
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame(arm, "arm")
