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
    kwargs, *_ = harness(tmp_path / "state", model={**MODEL, "api_mode": "responses"})
    with pytest.raises(ValueError, match="chat completion"):
        run_session(**kwargs)
    assert not Path(kwargs["state_dir"]).exists()
