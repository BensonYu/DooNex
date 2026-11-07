"""Pinned SDK conversion stays in the parent; dispatch consumes one context."""

import socket
from concurrent.futures import ThreadPoolExecutor

import pytest
from doonex import responses_adapter, sdk
from test_sdk import MODEL, harness, responses_answer


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Adapter contracts must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def test_alias_requires_context_and_rejects_context_lost_in_thread():
    with pytest.raises(RuntimeError, match="unused budget context"):
        responses_adapter._dispatch()
    with (
        responses_adapter.request_context(lambda *args: None, {}, "identity", None),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with pytest.raises(RuntimeError, match="unused budget context"):
            pool.submit(responses_adapter._dispatch).result(timeout=2)
        # The owner retains its sole authorization after the foreign thread failed.
        assert responses_adapter._dispatch(tools=None) is None


def test_one_dispatch_even_when_original_fails(monkeypatch):
    calls = []

    def original(**kwargs):
        calls.append(kwargs)
        raise ValueError("synthetic")

    monkeypatch.setattr(responses_adapter, "_ORIGINAL", original)
    with responses_adapter.request_context(None, {}, "identity", None):
        with pytest.raises(ValueError, match="synthetic"):
            responses_adapter._dispatch()
        with pytest.raises(RuntimeError, match="unused budget context"):
            responses_adapter._dispatch()
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="unused budget context"):
        responses_adapter._dispatch()


def test_nested_request_context_is_rejected():
    with (
        responses_adapter.request_context(None, {}, "identity", None),
        pytest.raises(RuntimeError, match="Nested"),
        responses_adapter.request_context(None, {}, "identity", None),
    ):
        pytest.fail("Nested budget contexts cannot be installed")


def test_changed_isolated_alias_is_rejected_before_dispatch(monkeypatch):
    import openhands.sdk.llm.llm as module

    monkeypatch.setattr(module, "litellm_responses", lambda **kwargs: None)
    with (
        pytest.raises(RuntimeError, match="pinned controller alias"),
        responses_adapter.request_context(lambda *args: None, {}, "identity", None),
    ):
        pytest.fail("A replacement alias cannot bypass the controller")


def test_changed_tools_are_rejected_without_dispatch():
    calls = []
    with (
        pytest.raises(ValueError, match="authorized SDK tools"),
        responses_adapter.request_context(
            lambda *args: calls.append(args), {}, "id", []
        ),
    ):
        responses_adapter._dispatch(tools=[{"name": "other"}])
    assert calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_real_sdk_with_isolated_binding_reserves_once_and_never_direct(
    tmp_path, monkeypatch, failure
):
    calls, reservations, settlements = [], [], []

    def forbidden(**kwargs):
        pytest.fail("Isolated mode must never use the parent direct transport")

    monkeypatch.setattr(responses_adapter, "_ORIGINAL", forbidden)

    def before(*args):
        reservation = {"id": str(len(reservations))}
        reservations.append(reservation)
        return reservation

    def binding(reservation, kwargs, request_id):
        calls.append(kwargs)
        assert reservation is reservations[-1]
        assert kwargs["extra_headers"]["X-Client-Request-Id"] == request_id
        assert "HTTP-Referer" not in kwargs["extra_headers"]
        assert "X-Title" not in kwargs["extra_headers"]
        assert kwargs["api_base"] == "https://api.openai.com/v1"
        assert {tool["name"] for tool in kwargs["tools"]} == {"doonex_shell", "finish"}
        if failure:
            raise ValueError("synthetic transport failure")
        return responses_answer("finish", {"message": "done"})

    args, *_ = harness(
        tmp_path,
        model={
            **MODEL,
            "api_mode": "responses",
            "responses_transport": "isolated_direct_v1",
        },
        before_model=before,
        after_model=lambda reservation, usage: settlements.append((reservation, usage)),
        responses_transport=binding,
    )
    if failure:
        from openhands.sdk.conversation.exceptions import ConversationRunError

        with pytest.raises(ConversationRunError) as caught:
            sdk.run_session(**args)
        assert isinstance(caught.value.__cause__, sdk.ModelCallError)
    else:
        assert sdk.run_session(**args) == "finished"
    assert len(calls) == len(reservations) == len(settlements) == 1
    assert settlements[0][0] is reservations[0]
    assert (settlements[0][1] is None) == failure


def test_direct_model_rejects_isolated_binding_before_reserving(tmp_path):
    args, before, *_ = harness(tmp_path, responses_transport=lambda *args: None)
    with pytest.raises(ValueError, match="explicit isolated mode"):
        sdk.run_session(**args)
    assert not before


@pytest.mark.parametrize("swallowed_failure", [False, True])
def test_isolated_response_cannot_skip_its_helper_binding(
    tmp_path, monkeypatch, swallowed_failure
):
    from openhands.sdk.conversation.exceptions import ConversationRunError

    settlements, calls = [], []

    def skipped(self, *args, **kwargs):
        if swallowed_failure:
            try:
                responses_adapter._dispatch(
                    tools=responses_adapter._ACTIVE.get().expected_tools
                )
            except ValueError:
                pass
        return self._build_responses_result(
            responses_answer("finish", {"message": "unbound"})
        )

    monkeypatch.setattr(sdk.LLM, "responses", skipped)

    def failed_binding(*args):
        calls.append(args)
        raise ValueError("synthetic rejected helper")

    args, reservations, *_ = harness(
        tmp_path,
        model={
            **MODEL,
            "api_mode": "responses",
            "responses_transport": "isolated_direct_v1",
        },
        responses_transport=failed_binding,
        after_model=lambda reservation, usage: settlements.append(usage),
    )
    with pytest.raises(ConversationRunError) as caught:
        sdk.run_session(**args)
    assert isinstance(caught.value.__cause__, sdk.ModelCallError)
    assert len(calls) == int(swallowed_failure)
    assert len(reservations) == 1
    assert settlements == [None]
