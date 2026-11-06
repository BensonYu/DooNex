"""Classify the pinned SDK's real exception mapping without refunding usage."""

import json

import pytest
from doonex.config import Settings
from doonex.sdk import GuardedLLM
from doonex.worker import Worker, run_directory
from litellm import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    ContextWindowExceededError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from test_m3_pipeline import prepare

pytestmark = pytest.mark.docker


@pytest.mark.parametrize("api_mode", ["chat", "responses"])
@pytest.mark.parametrize(
    "error_class,sdk_type,environment_failure,safety_violation",
    [
        (Timeout, "LLMTimeoutError", True, False),
        (RateLimitError, "LLMRateLimitError", True, False),
        (APIConnectionError, "LLMServiceUnavailableError", True, False),
        (ServiceUnavailableError, "LLMServiceUnavailableError", True, False),
        (InternalServerError, "LLMServiceUnavailableError", True, False),
        (BadRequestError, "LLMBadRequestError", False, False),
        (AuthenticationError, "LLMAuthenticationError", False, False),
        (ContextWindowExceededError, "LLMContextWindowExceedError", False, False),
        (RuntimeError, "RuntimeError", False, False),
        (Timeout, "LLMTimeoutError", False, True),
    ],
)
def test_provider_failure_keeps_unknown_usage_and_missing_attempt(
    settings,
    store,
    tmp_path,
    monkeypatch,
    api_mode,
    error_class,
    sdk_type,
    environment_failure,
    safety_violation,
):
    import openhands.sdk.llm.llm as upstream

    model = settings.model.model_copy(
        update={"model_id": "gpt-4.1-mini", "api_mode": api_mode}
    )
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    arena, batch, _ = prepare(settings, store, tmp_path)
    run_id = batch["attempts"][0]["run_id"]
    calls = []
    secret = "PROVIDER_DIAGNOSTIC_MUST_NOT_PERSIST"

    def transport(*args, **kwargs):
        calls.append(1)
        if safety_violation:
            with store.locked(run_id) as (_, current):
                current["safety_violation"] = True
        if error_class is RuntimeError:
            raise RuntimeError(f"Timeout LLMTimeoutError provider unavailable {secret}")
        raise error_class(
            message=f"Synthetic provider diagnostic {secret}",
            model="gpt-4.1-mini",
            llm_provider="openai",
        )

    def forbidden(*args, **kwargs):
        pytest.fail("Failure must not trigger a different API or an extra request")

    monkeypatch.setattr(
        GuardedLLM, "_transport_call", transport if api_mode == "chat" else forbidden
    )
    monkeypatch.setattr(
        upstream,
        "litellm_responses",
        transport if api_mode == "responses" else forbidden,
    )
    Worker(settings).run(once=True)
    run = store.get(run_id)
    detail = arena.detail(batch["id"])
    attempt = next(item for item in detail["attempts"] if item["run_id"] == run_id)
    expected = "environment_failure" if environment_failure else "failed"
    assert calls == [1]
    assert run["state"] == "failed"
    if safety_violation:
        assert run["reason"] == "检测到未授权访问请求；已停止后续模型及工具动作"
    else:
        assert sdk_type in run["reason"]
    assert attempt["classification"] == expected
    if environment_failure:
        assert run["arena_outcome"]["kind"] == "provider_transport"
        assert run["arena_outcome"]["provider_error_type"] == sdk_type
    assert run["usage"]["calls"] == run["usage"]["unknown_calls"] == 1
    assert run["usage"]["tokens"] == 0
    assert run["usage"]["reserved_tokens"] > 0
    assert detail["usage"] == run["usage"]
    assert detail["metrics"]["counts"]["environment_failure"] == int(
        environment_failure
    )
    assert detail["metrics"]["counts"]["passed"] == 0
    assert detail["metrics"]["counts"]["valid"] == int(not environment_failure)
    assert detail["metrics"]["counts"]["missing_slots"] == 14 + int(environment_failure)
    assert attempt["safety_violation"] is safety_violation
    assert not detail["metrics"]["complete"]
    assert all(not values for values in detail["metrics"]["recommendations"].values())
    assert secret not in json.dumps((run, detail), default=str)
    assert secret not in "".join(
        path.read_text() for path in run_directory(settings, run_id).rglob("*.json")
    )
