"""SDK boundary contracts use the real agent loop and a fake provider transport."""

import json
import logging
from pathlib import Path

import pytest
from doonex.sdk import GuardedLLM, SessionRecoveryRequired, run_session
from litellm import ModelResponse, RateLimitError

MODEL = {
    "provider": "openai",
    "model_id": "gpt-4.1-mini",
    "api_mode": "chat",
    "max_input_tokens": 100000,
    "max_output_tokens": 1000,
    "timeout_seconds": 10,
}
SECRET = "DOONEX_FAKE_SECRET_73a9"


def answer(tool, arguments, *, sequence=1, usage=True):
    data = {
        "id": f"response-{sequence}",
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call-{sequence}",
                            "type": "function",
                            "function": {
                                "name": tool,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
    }
    if usage:
        data["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        }
    response = ModelResponse(**data)
    if not usage:
        response.usage = None
    return response


def harness(tmp_path, **overrides):
    reservations, settlements, commands, events = [], [], [], []

    def reserve(input_tokens, output_tokens):
        item = (input_tokens, output_tokens)
        reservations.append(item)
        return item

    kwargs = {
        "run_id": "sdk-contract",
        "state_dir": tmp_path,
        "prompt": "Create result.txt containing done",
        "model": MODEL,
        "api_key": SECRET,
        "before_model": reserve,
        "after_model": lambda reservation, usage: settlements.append(
            (reservation, usage)
        ),
        "shell": lambda command: (
            commands.append(command) or {"output": "done", "exit_code": 0}
        ),
        "on_event": events.append,
        "should_stop": lambda: False,
    }
    kwargs.update(overrides)
    return kwargs, reservations, settlements, commands, events


def test_real_sdk_shell_finish_and_secret_redaction(tmp_path, monkeypatch):
    responses = iter(
        [
            answer("doonex_shell", {"command": "python -c 'print(1)'"}),
            answer("finish", {"message": f"done {SECRET}"}, sequence=2),
        ]
    )
    transports = []

    def transport(self, **kwargs):
        transports.append(kwargs)
        assert self.num_retries == 0
        assert not self.caching_prompt
        assert self.fallback_strategy is None
        assert self.api_key.get_secret_value() == SECRET
        return next(responses)

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    kwargs, reservations, settlements, commands, events = harness(tmp_path)
    assert run_session(**kwargs) == "finished"
    assert len(commands) == 1
    assert len(transports) == len(reservations) == len(settlements) == 2
    assert all(0 < tokens <= MODEL["max_input_tokens"] for tokens, _ in reservations)
    assert all(output == MODEL["max_output_tokens"] for _, output in reservations)
    assert all(
        usage == {"input_tokens": 100, "output_tokens": 20} for _, usage in settlements
    )
    assert {t["function"]["name"] for t in transports[0]["tools"]} == {
        "doonex_shell",
        "finish",
    }
    all_state = "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert SECRET not in all_state
    assert SECRET not in json.dumps(events)
    assert "[REDACTED]" in all_state


def test_pause_and_resume_do_not_replay_completed_shell(tmp_path, monkeypatch):
    stopped = False
    transport_count = 0

    def transport(self, **kwargs):
        nonlocal transport_count
        transport_count += 1
        if transport_count == 1:
            return answer("doonex_shell", {"command": "create-once"})
        assert self.api_key.get_secret_value() == "replacement-secret"
        return answer("finish", {"message": "done"}, sequence=2)

    def shell(command):
        nonlocal stopped
        stopped = True
        return {"output": "created", "exit_code": 0}

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    first, *_ = harness(tmp_path, shell=shell, should_stop=lambda: stopped)
    assert run_session(**first) == "paused"
    assert transport_count == 1
    second, _, settlements, commands, events = harness(
        tmp_path,
        prompt=None,
        resume=True,
        api_key="replacement-secret",
    )
    assert run_session(**second) == "finished"
    assert transport_count == 2
    assert commands == []
    assert len(settlements) == 1
    assert events


def test_stop_between_tools_in_one_model_response(tmp_path, monkeypatch):
    stopped = False
    calls = []
    response = answer("doonex_shell", {"command": "first"})
    extra = answer("doonex_shell", {"command": "second"}, sequence=2)
    response.choices[0].message.tool_calls.extend(extra.choices[0].message.tool_calls)

    def shell(command):
        nonlocal stopped
        calls.append(command)
        stopped = True
        return {"output": "done", "exit_code": 0}

    def transport(*args, **kwargs):
        assert not calls
        return response

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    kwargs, *_ = harness(tmp_path, shell=shell, should_stop=lambda: stopped)
    assert run_session(**kwargs) == "paused"
    assert calls == ["first"]


def test_echoed_credential_is_scrubbed_before_tool_execution(tmp_path, monkeypatch):
    responses = iter(
        [
            answer("doonex_shell", {"command": f"echo {SECRET}"}),
            answer("finish", {"message": "done"}, sequence=2),
        ]
    )
    monkeypatch.setattr(GuardedLLM, "_transport_call", lambda *a, **k: next(responses))
    kwargs, _, _, commands, _ = harness(tmp_path)
    assert run_session(**kwargs) == "finished"
    assert commands == ["echo [REDACTED]"]


def test_budget_rejection_sends_no_request(tmp_path, monkeypatch):
    def reject(*args):
        raise ValueError("budget exhausted")

    def transport(*args, **kwargs):
        pytest.fail("request must not be dispatched")

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    kwargs, _, settlements, commands, _ = harness(tmp_path, before_model=reject)
    with pytest.raises(Exception, match="budget exhausted"):
        run_session(**kwargs)
    assert settlements == commands == []


def test_unknown_usage_is_not_free(tmp_path, monkeypatch):
    monkeypatch.setattr(
        GuardedLLM,
        "_transport_call",
        lambda *a, **k: answer("finish", {"message": "done"}, usage=False),
    )
    kwargs, _, settlements, _, _ = harness(tmp_path)
    assert run_session(**kwargs) == "finished"
    assert settlements[0][1] is None


def test_provider_failure_is_not_retried_and_is_redacted(tmp_path, monkeypatch, caplog):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        raise RateLimitError(
            message=f"provider echoed credential {SECRET}",
            model="gpt-4.1-mini",
            llm_provider="openai",
        )

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    kwargs, _, settlements, _, events = harness(tmp_path)
    with caplog.at_level(logging.ERROR), pytest.raises(Exception) as error:
        run_session(**kwargs)
    assert calls == [1]
    assert settlements[0][1] is None
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(events)
    assert SECRET not in "".join(path.read_text() for path in tmp_path.rglob("*.json"))


def test_no_ambient_discovery_or_host_tools(tmp_path, monkeypatch):
    import openhands.sdk.agent.base as agent_base
    import openhands.sdk.conversation.impl.local_conversation as local
    from openhands.sdk.llm.llm_profile_store import LLMProfileStore

    def forbidden(*args, **kwargs):
        pytest.fail("ambient host discovery must be disabled")

    for name in [
        "load_available_plugins",
        "register_file_agents",
        "load_available_skills",
        "load_memory",
        "load_marketplace_standalone_skills",
    ]:
        monkeypatch.setattr(local, name, forbidden)
    monkeypatch.setattr(agent_base, "has_vision_profile_available", forbidden)
    monkeypatch.setattr(agent_base, "_load_soul_md", forbidden)
    monkeypatch.setattr(LLMProfileStore, "load", forbidden)
    monkeypatch.setattr(
        GuardedLLM,
        "_transport_call",
        lambda *a, **k: answer("finish", {"message": "done"}),
    )
    kwargs, *_ = harness(tmp_path)
    assert run_session(**kwargs) == "finished"


def test_input_bound_refuses_oversized_prompt_before_reservation(tmp_path, monkeypatch):
    def transport(*args, **kwargs):
        pytest.fail("oversized request must not reach transport")

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    kwargs, reservations, settlements, _, _ = harness(
        tmp_path,
        model={**MODEL, "max_input_tokens": 16384},
        prompt="x" * 20000,
    )
    with pytest.raises(Exception, match="input token bound"):
        run_session(**kwargs)
    assert reservations == settlements == []


def test_resume_refuses_orphaned_action(tmp_path, monkeypatch):
    monkeypatch.setattr(
        GuardedLLM,
        "_transport_call",
        lambda *a, **k: answer("doonex_shell", {"command": "possibly-executed"}),
    )

    class SimulatedCrash(BaseException):
        pass

    def crash(*args):
        raise SimulatedCrash()

    kwargs, *_ = harness(tmp_path, shell=crash)
    with pytest.raises(SimulatedCrash):
        run_session(**kwargs)
    resumed, *_ = harness(tmp_path, prompt=None, resume=True)
    with pytest.raises(SessionRecoveryRequired, match="no recorded outcome"):
        run_session(**resumed)


def test_resume_and_control_directory_fail_closed(tmp_path):
    kwargs, *_ = harness(tmp_path, resume=True)
    with pytest.raises(ValueError, match="resume flag"):
        run_session(**kwargs)
    (tmp_path / "control" / "AGENTS.md").write_text("untrusted instructions")
    kwargs["resume"] = False
    with pytest.raises(ValueError, match="remain empty"):
        run_session(**kwargs)


def test_unsupported_api_mode_is_rejected_before_state_creation(tmp_path):
    kwargs, *_ = harness(tmp_path / "state", model={**MODEL, "api_mode": "auto"})
    with pytest.raises(ValueError, match="explicit chat or responses"):
        run_session(**kwargs)
    assert not Path(kwargs["state_dir"]).exists()


@pytest.mark.parametrize(
    "provider", ["litellm_proxy", "ollama", "huggingface", "openrouter"]
)
def test_unsupported_provider_is_rejected_before_network_or_state(
    tmp_path, monkeypatch, provider
):
    import httpx

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported providers must not perform discovery or inference")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(GuardedLLM, "_transport_call", forbidden)
    kwargs, reservations, settlements, _, _ = harness(
        tmp_path / "state", model={**MODEL, "provider": provider}
    )
    with pytest.raises(ValueError, match="openai and anthropic"):
        run_session(**kwargs)
    assert reservations == settlements == []
    assert not Path(kwargs["state_dir"]).exists()


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [
        ("openai", "gpt-4.1-mini"),
        ("openai", "prod/ollama/opaque-gateway-alias"),
        ("anthropic", "claude-sonnet-4-5"),
    ],
)
def test_supported_gateway_uses_no_constructor_or_runtime_metadata_requests(
    tmp_path, monkeypatch, provider, model_id
):
    import asyncio

    import httpx
    import openhands.sdk.llm.llm as upstream

    def forbidden(*args, **kwargs):
        pytest.fail("provider metadata discovery must remain offline")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(upstream, "get_litellm_model_info", forbidden)
    monkeypatch.setattr(upstream, "resolve_provider_metadata_sync", forbidden)
    monkeypatch.setattr(upstream, "aresolve_provider_metadata", forbidden)

    def transport(self, **kwargs):
        assert self.model == f"{provider}/{model_id}"
        assert self.base_url == "https://private-gateway.invalid/custom/v1"
        assert self.resolve_runtime_metadata(force=True) is None
        assert asyncio.run(self.aresolve_runtime_metadata(force=True)) is None
        assert self.effective_max_input_tokens == MODEL["max_input_tokens"]
        assert self.effective_max_output_tokens == MODEL["max_output_tokens"]
        if model_id == "gpt-4.1-mini":
            assert self.model_info["supports_function_calling"] is True
        return answer("finish", {"message": "done"})

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    kwargs, reservations, _, _, _ = harness(
        tmp_path,
        model={
            **MODEL,
            "provider": provider,
            "model_id": model_id,
            "base_url": "https://private-gateway.invalid/custom/v1",
        },
    )
    assert run_session(**kwargs) == "finished"
    assert len(reservations) == 1


def test_constructor_diagnostics_are_redacted(tmp_path, monkeypatch, caplog):
    refresh = GuardedLLM._refresh_litellm_metadata

    def diagnostic_refresh(self):
        try:
            raise RuntimeError(f"construction diagnostic echoed {SECRET}")
        except RuntimeError:
            logging.getLogger("openhands.sdk.llm.llm").exception(
                "constructor diagnostic: %s", SECRET
            )
        refresh(self)

    monkeypatch.setattr(GuardedLLM, "_refresh_litellm_metadata", diagnostic_refresh)
    monkeypatch.setattr(
        GuardedLLM,
        "_transport_call",
        lambda *a, **k: answer("finish", {"message": "done"}),
    )
    kwargs, *_ = harness(tmp_path)
    with caplog.at_level(logging.ERROR):
        assert run_session(**kwargs) == "finished"
    assert "constructor diagnostic" in caplog.text
    assert "[REDACTED]" in caplog.text
    assert SECRET not in caplog.text


def test_fresh_worker_disables_inherited_tracing_and_dotenv(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    (tmp_path / ".env").write_text(
        "LMNR_PROJECT_API_KEY=FAKE_DOTENV_TRACING_KEY\n"
        "OTEL_ENDPOINT=https://dotenv-tracing.invalid\n"
        "DOONEX_DOTENV_MUST_NOT_LOAD=loaded\n"
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "apps/server"),
            "LITELLM_MODE": "DEV",
            "LITELLM_LOCAL_MODEL_COST_MAP": "False",
            "OTEL_SDK_DISABLED": "false",
            "LMNR_PROJECT_API_KEY": "FAKE_INHERITED_TRACING_KEY",
            "OTEL_ENDPOINT": "https://inherited-tracing.invalid",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://traces.invalid",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://exporter.invalid",
        }
    )
    script = textwrap.dedent(
        """
        import os
        import socket
        import sys
        from pathlib import Path

        attempts = []
        def forbidden_connection(*args, **kwargs):
            attempts.append("network")
            raise AssertionError("worker import/SDK fixture must stay offline")
        socket.socket.connect = forbidden_connection

        from doonex.sdk import GuardedLLM, run_session
        from litellm import ModelResponse
        from openhands.sdk.observability.laminar import should_enable_observability

        for name in (
            "LMNR_PROJECT_API_KEY", "OTEL_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT",
            "DOONEX_DOTENV_MUST_NOT_LOAD",
        ):
            assert name not in os.environ, name
        assert os.environ["LITELLM_MODE"] == "PRODUCTION"
        assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
        assert os.environ["OTEL_SDK_DISABLED"] == "true"
        assert not should_enable_observability()
        assert "lmnr" not in sys.modules

        def fake_transport(*args, **kwargs):
            return ModelResponse(
                id="offline-finish", model="gateway-alias",
                choices=[{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "finish-1", "type": "function", "function": {
                            "name": "finish", "arguments": '{"message": "done"}'
                        }
                    }]
                }}],
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            )
        GuardedLLM._transport_call = fake_transport
        assert run_session(
            run_id="offline-privacy", state_dir=Path("state"), prompt="Finish",
            model={
                "provider": "openai", "model_id": "gateway-alias", "api_mode": "chat",
                "base_url": "https://authorized-gateway.invalid/v1",
                "max_input_tokens": 100000, "max_output_tokens": 1000,
                "timeout_seconds": 1,
            },
            api_key="FAKE_MODEL_KEY", before_model=lambda *args: object(),
            after_model=lambda *args: None, shell=lambda *args: {},
            on_event=lambda *args: None, should_stop=lambda: False,
        ) == "finished"
        assert not should_enable_observability()
        assert "lmnr" not in sys.modules
        assert attempts == [], attempts
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("state", ["tracing", "callbacks", "otel"])
def test_reusing_instrumented_process_is_rejected(tmp_path, monkeypatch, state):
    import litellm
    import opentelemetry.trace
    from openhands.sdk.observability import laminar

    if state == "tracing":
        monkeypatch.setattr(laminar, "_observability_enabled", True)
    elif state == "callbacks":
        monkeypatch.setattr(litellm, "success_callback", [lambda *args: None])
    else:
        monkeypatch.setattr(opentelemetry.trace, "_TRACER_PROVIDER", object())
    kwargs, reservations, settlements, _, _ = harness(tmp_path / "state")
    with pytest.raises(RuntimeError, match="fresh DooNex worker"):
        run_session(**kwargs)
    assert reservations == settlements == []
    assert not Path(kwargs["state_dir"]).exists()


def responses_answer(tool, arguments, *, sequence=1, usage=True):
    from litellm.types.llms.openai import ResponsesAPIResponse

    return ResponsesAPIResponse(
        id=f"resp_{sequence}",
        created_at=1,
        model="gateway-model",
        object="response",
        status="completed",
        output=[
            {
                "id": f"fc_{sequence}",
                "call_id": f"call_{sequence}",
                "type": "function_call",
                "name": tool,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        ],
        usage=(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 80},
            }
            if usage
            else None
        ),
    )


def response_harness(tmp_path, monkeypatch, transport, **overrides):
    import openhands.sdk.llm.llm as upstream

    def forbidden(*args, **kwargs):
        pytest.fail("Responses must never fall back to chat")

    monkeypatch.setattr(upstream, "litellm_responses", transport)
    monkeypatch.setattr(GuardedLLM, "_transport_call", forbidden)
    return harness(tmp_path, model={**MODEL, "api_mode": "responses"}, **overrides)


def test_real_sdk_responses_shell_finish_redacts_and_counts_full_input(
    tmp_path, monkeypatch
):
    responses = iter(
        [
            responses_answer("doonex_shell", {"command": f"echo {SECRET}"}),
            responses_answer("finish", {"message": f"done {SECRET}"}, sequence=2),
        ]
    )
    transports = []

    def transport(**kwargs):
        transports.append(kwargs)
        assert kwargs["api_key"] == SECRET
        assert kwargs["max_retries"] == kwargs["num_retries"] == 0
        assert kwargs["max_output_tokens"] == MODEL["max_output_tokens"]
        assert kwargs["custom_llm_provider"] == "openai"
        assert kwargs["store"] is False
        assert kwargs["stream"] is False
        assert kwargs["use_chat_completions_api"] is False
        assert {tool["name"] for tool in kwargs["tools"]} == {
            "doonex_shell",
            "finish",
        }
        assert all(tool["type"] == "function" for tool in kwargs["tools"])
        return next(responses)

    kwargs, reservations, settlements, commands, events = response_harness(
        tmp_path, monkeypatch, transport
    )
    assert run_session(**kwargs) == "finished"
    assert commands == ["echo [REDACTED]"]
    assert len(transports) == len(reservations) == len(settlements) == 2
    assert all(
        usage == {"input_tokens": 100, "output_tokens": 20} for _, usage in settlements
    )
    assert any(
        item["type"] == "function_call_output" for item in transports[1]["input"]
    )
    assert SECRET not in json.dumps(events)
    assert SECRET not in "".join(path.read_text() for path in tmp_path.rglob("*.json"))


def test_responses_budget_rejection_sends_no_request(tmp_path, monkeypatch):
    def reject(*args):
        raise ValueError("budget exhausted")

    def forbidden(*args, **kwargs):
        pytest.fail("budget rejection must prevent Responses transport")

    kwargs, _, settlements, commands, _ = response_harness(
        tmp_path, monkeypatch, forbidden, before_model=reject
    )
    with pytest.raises(Exception, match="budget exhausted"):
        run_session(**kwargs)
    assert settlements == commands == []


def test_responses_unknown_usage_preserves_reservation(tmp_path, monkeypatch):
    kwargs, reservations, settlements, _, _ = response_harness(
        tmp_path,
        monkeypatch,
        lambda **kwargs: responses_answer("finish", {"message": "done"}, usage=False),
    )
    assert run_session(**kwargs) == "finished"
    assert len(reservations) == len(settlements) == 1
    assert settlements[0][1] is None


def test_responses_provider_error_is_not_retried_and_is_redacted(
    tmp_path, monkeypatch, caplog
):
    calls = []

    def transport(**kwargs):
        calls.append(1)
        raise RateLimitError(
            message=f"Responses diagnostic echoed {SECRET}",
            model="gateway-model",
            llm_provider="openai",
        )

    kwargs, _, settlements, _, events = response_harness(
        tmp_path, monkeypatch, transport
    )
    with caplog.at_level(logging.ERROR), pytest.raises(Exception) as error:
        run_session(**kwargs)
    assert calls == [1]
    assert settlements[0][1] is None
    assert SECRET not in str(error.value)
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(events)
    assert SECRET not in "".join(path.read_text() for path in tmp_path.rglob("*.json"))


def test_responses_pause_resume_does_not_replay_shell(tmp_path, monkeypatch):
    stopped = False
    calls = []

    def transport(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            return responses_answer("doonex_shell", {"command": "create-once"})
        assert kwargs["api_key"] == "replacement-secret"
        return responses_answer("finish", {"message": "done"}, sequence=2)

    def shell(command):
        nonlocal stopped
        stopped = True
        return {"output": "created", "exit_code": 0}

    kwargs, *_ = response_harness(
        tmp_path, monkeypatch, transport, shell=shell, should_stop=lambda: stopped
    )
    assert run_session(**kwargs) == "paused"
    resumed, _, _, commands, _ = response_harness(
        tmp_path,
        monkeypatch,
        transport,
        prompt=None,
        resume=True,
        api_key="replacement-secret",
    )
    assert run_session(**resumed) == "finished"
    assert len(calls) == 2
    assert commands == []


def test_anthropic_responses_is_rejected_before_state_creation(tmp_path):
    kwargs, *_ = harness(
        tmp_path / "state",
        model={**MODEL, "provider": "anthropic", "api_mode": "responses"},
    )
    with pytest.raises(ValueError, match="Responses only through the openai"):
        run_session(**kwargs)
    assert not Path(kwargs["state_dir"]).exists()


@pytest.mark.parametrize("failure", [False, True])
def test_responses_real_litellm_http_boundary_has_no_fallback_or_retry(
    tmp_path, monkeypatch, caplog, failure
):
    import httpx

    calls = []

    def transport(self, request, *args, **kwargs):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://responses-gateway.invalid/v1/responses"
        body = json.loads(request.content)
        assert body["max_output_tokens"] == MODEL["max_output_tokens"]
        assert body["store"] is False
        if failure:
            return httpx.Response(
                429,
                request=request,
                json={
                    "error": {
                        "message": f"rate limited {SECRET}",
                        "type": "rate_limit_error",
                    }
                },
            )
        tool = "doonex_shell" if len(calls) == 1 else "finish"
        arguments = (
            {"command": "create-once"} if len(calls) == 1 else {"message": "done"}
        )
        return httpx.Response(
            200,
            request=request,
            json=responses_answer(tool, arguments, sequence=len(calls)).model_dump(
                mode="json"
            ),
        )

    def forbidden_chat(*args, **kwargs):
        pytest.fail("native Responses must not fall back to chat")

    monkeypatch.setattr(httpx.Client, "send", transport)
    monkeypatch.setattr(GuardedLLM, "_transport_call", forbidden_chat)
    kwargs, reservations, settlements, commands, events = harness(
        tmp_path,
        model={
            **MODEL,
            "api_mode": "responses",
            "model_id": "gateway-model",
            "base_url": "https://responses-gateway.invalid/v1",
        },
    )
    with caplog.at_level(logging.ERROR):
        if failure:
            with pytest.raises(Exception, match="Model request failed"):
                run_session(**kwargs)
        else:
            assert run_session(**kwargs) == "finished"
    assert len(calls) == len(reservations) == len(settlements) == (1 if failure else 2)
    assert commands == ([] if failure else ["create-once"])
    assert settlements[0][1] == (
        None if failure else {"input_tokens": 100, "output_tokens": 20}
    )
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(events)
