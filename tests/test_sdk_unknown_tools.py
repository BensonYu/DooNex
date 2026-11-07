"""Unknown tool outcomes abort the pinned SDK loop without synthetic success."""

import socket

import pytest
from doonex import sdk
from doonex.budget import Halt
from doonex.store import OwnershipLost
from doonex.worker import PermissionViolation
from doonex.workspace import WorkspaceError
from test_sdk import MODEL, answer, harness, response_harness, responses_answer
from test_sdk_scheduling import settings, snapshot


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unknown-tool contracts must never contact a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def batch(api_mode):
    make = responses_answer if api_mode == "responses" else answer
    response = make("doonex_shell", {"command": "first"})
    for sequence, name, arguments in (
        (2, "doonex_shell", {"command": "second"}),
        (3, "finish", {"message": "done"}),
    ):
        following = make(name, arguments, sequence=sequence)
        if api_mode == "responses":
            response.output.extend(following.output)
        else:
            response.choices[0].message.tool_calls.extend(
                following.choices[0].message.tool_calls
            )
    return response


def arguments(tmp_path, monkeypatch, api_mode, calls, **overrides):
    def transport(*args, **kwargs):
        calls.append(1)
        assert calls == [1], "An unknown tool must prevent another model request"
        return batch(api_mode)

    if api_mode == "responses":
        return response_harness(tmp_path, monkeypatch, transport, **overrides)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    return harness(tmp_path, model={**MODEL, "api_mode": api_mode}, **overrides)


@pytest.mark.parametrize("api_mode", ["chat", "responses"])
@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("stop_after_unknown", [False, True])
def test_unknown_tool_aborts_remaining_batch_and_requires_manual_recovery(
    tmp_path, monkeypatch, api_mode, scheduled, stop_after_unknown
):
    calls, commands, finishes, receipts = [], [], [], []
    stopped = False
    cause = WorkspaceError("Synthetic checkpoint recording failure")
    signal = sdk.ToolOutcomeUnknown("Dispatched tool outcome is uncertain")

    def shell(command, identity=None):
        nonlocal stopped
        commands.append(command)
        stopped = stop_after_unknown
        raise signal from cause

    kwargs, reservations, settlements, _, _ = arguments(
        tmp_path,
        monkeypatch,
        api_mode,
        calls,
        shell=shell,
        shell_with_action=shell if scheduled else None,
        scheduling=settings(receipts) if scheduled else None,
        on_finish=finishes.append,
        should_stop=lambda: stopped,
    )
    with pytest.raises(
        sdk.SessionRecoveryRequired, match="no recorded outcome"
    ) as error:
        sdk.run_session(**kwargs)
    assert error.value.__cause__ is signal
    assert signal.__cause__ is cause
    assert calls == [1] and commands == ["first"]
    assert len(reservations) == len(settlements) == 1
    assert settlements[0][1] == {"input_tokens": 100, "output_tokens": 20}
    assert finishes == receipts == []
    state, events = snapshot(tmp_path)
    assert state["execution_status"] != "finished"
    assert [
        event["tool_name"] for event in events if event["kind"] == "ActionEvent"
    ] == [
        "doonex_shell",
        "doonex_shell",
        "finish",
    ]
    assert not {"ObservationEvent", "AgentErrorEvent", "PauseEvent"} & {
        event["kind"] for event in events
    }
    assert not sdk._CALLBACKS and not sdk._SCHEDULING

    # Removing scheduling cannot turn the unmatched action into a replay permit.
    kwargs.update(
        prompt=None,
        resume=True,
        scheduling=None,
        shell_with_action=None,
        should_stop=lambda: False,
    )
    with pytest.raises(sdk.SessionRecoveryRequired, match="no recorded outcome"):
        sdk.run_session(**kwargs)
    assert calls == [1] and commands == ["first"]
    assert len(reservations) == len(settlements) == 1
    assert finishes == receipts == []
    assert not sdk._CALLBACKS and not sdk._SCHEDULING


@pytest.mark.parametrize("api_mode", ["chat", "responses"])
@pytest.mark.parametrize("scheduled", [False, True])
def test_known_tool_batch_still_finishes(tmp_path, monkeypatch, api_mode, scheduled):
    calls, commands, finishes, receipts = [], [], [], []

    def shell(command, identity=None):
        commands.append(command)
        return {"output": command, "exit_code": 0}

    kwargs, reservations, settlements, _, _ = arguments(
        tmp_path,
        monkeypatch,
        api_mode,
        calls,
        shell=shell,
        shell_with_action=shell if scheduled else None,
        scheduling=settings(receipts) if scheduled else None,
        on_finish=finishes.append,
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert calls == [1] and commands == ["first", "second"]
    assert len(reservations) == len(settlements) == 1
    assert finishes == ["done"] and receipts == []
    state, events = snapshot(tmp_path)
    assert state["execution_status"] == "finished"
    assert sum(event["kind"] == "ObservationEvent" for event in events) == 3
    assert not sdk._CALLBACKS and not sdk._SCHEDULING


@pytest.mark.parametrize(
    "signal_type", [KeyboardInterrupt, SystemExit, sdk.SchedulerYield]
)
def test_other_control_signals_are_not_reclassified(tmp_path, monkeypatch, signal_type):
    calls, commands, finishes = [], [], []
    signal = signal_type("Synthetic control interruption")

    def shell(command):
        commands.append(command)
        raise signal

    kwargs, *_ = arguments(
        tmp_path, monkeypatch, "chat", calls, shell=shell, on_finish=finishes.append
    )
    with pytest.raises(signal_type) as error:
        sdk.run_session(**kwargs)
    assert error.value is signal
    assert calls == [1] and commands == ["first"] and finishes == []
    assert not sdk._CALLBACKS and not sdk._SCHEDULING


@pytest.mark.parametrize("api_mode", ["chat", "responses"])
@pytest.mark.parametrize(
    "error_type", [Halt, PermissionViolation, OwnershipLost, ValueError, RuntimeError]
)
def test_controller_callback_errors_keep_their_original_classification(
    tmp_path, monkeypatch, api_mode, error_type
):
    calls, commands, finishes, receipts = [], [], [], []
    stopped = False
    error = error_type("Synthetic controller failure")
    cause = RuntimeError("Synthetic original cause")

    def shell(command, identity):
        nonlocal stopped
        commands.append(command)
        stopped = True
        raise error from cause

    kwargs, *_ = arguments(
        tmp_path,
        monkeypatch,
        api_mode,
        calls,
        shell_with_action=shell,
        scheduling=settings(receipts),
        should_stop=lambda: stopped,
        on_finish=finishes.append,
    )
    with pytest.raises(error_type) as received:
        sdk.run_session(**kwargs)
    assert received.value is error and received.value.__cause__ is cause
    assert calls == [1] and commands == ["first"]
    assert receipts == finishes == []
    assert not {"ObservationEvent", "PauseEvent"} & {
        event["kind"] for event in snapshot(tmp_path)[1]
    }
    assert not sdk._CALLBACKS and not sdk._SCHEDULING


def test_shell_stop_check_failure_aborts_before_any_callback(tmp_path, monkeypatch):
    calls, commands, finishes = [], [], []
    error = OwnershipLost("Synthetic stop-check database failure")

    def stop():
        if calls:
            raise error
        return False

    kwargs, *_ = arguments(
        tmp_path,
        monkeypatch,
        "chat",
        calls,
        should_stop=stop,
        shell=lambda command: commands.append(command),
        on_finish=finishes.append,
    )
    with pytest.raises(OwnershipLost) as received:
        sdk.run_session(**kwargs)
    assert received.value is error
    assert calls == [1] and commands == finishes == []
    assert not sdk._CALLBACKS and not sdk._SCHEDULING


def test_missing_sdk_action_identity_requires_reconciliation(tmp_path, monkeypatch):
    calls, commands, finishes, receipts = [], [], [], []
    monkeypatch.setattr(sdk, "_action_identity", lambda *args: None)
    kwargs, *_ = arguments(
        tmp_path,
        monkeypatch,
        "chat",
        calls,
        shell_with_action=lambda *args: commands.append(args),
        scheduling=settings(receipts),
        on_finish=finishes.append,
    )
    with pytest.raises(sdk.SessionRecoveryRequired, match="action identity is missing"):
        sdk.run_session(**kwargs)
    assert calls == [1] and commands == finishes == receipts == []
    assert not sdk._CALLBACKS and not sdk._SCHEDULING


@pytest.mark.parametrize("failure", ["json", "observation"])
def test_observation_conversion_failure_never_executes_the_next_tool(
    tmp_path, monkeypatch, failure
):
    calls, completed, finishes = [], [], []
    error = RuntimeError("Synthetic SDK observation persistence failure")
    if failure == "observation":

        def reject(*args, **kwargs):
            raise error

        monkeypatch.setattr(sdk.ShellObservation, "from_text", reject)

    def shell(command):
        completed.append(command)
        return {"exit_code": 0, "output": {"not JSON"} if failure == "json" else "done"}

    kwargs, *_ = arguments(
        tmp_path, monkeypatch, "chat", calls, shell=shell, on_finish=finishes.append
    )
    with pytest.raises(TypeError if failure == "json" else RuntimeError) as received:
        sdk.run_session(**kwargs)
    if failure == "observation":
        assert received.value is error
    assert calls == [1] and completed == ["first"] and finishes == []
    assert not {"ObservationEvent", "AgentErrorEvent"} & {
        event["kind"] for event in snapshot(tmp_path)[1]
    }
    assert not sdk._CALLBACKS and not sdk._SCHEDULING


def test_nonzero_command_result_remains_a_normal_observation(tmp_path, monkeypatch):
    calls, commands, finishes = [], [], []

    def shell(command):
        commands.append(command)
        return {"exit_code": 1, "output": "Synthetic command failure"}

    kwargs, *_ = arguments(
        tmp_path, monkeypatch, "chat", calls, shell=shell, on_finish=finishes.append
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert calls == [1] and commands == ["first", "second"] and finishes == ["done"]
    observations = [
        event["observation"]
        for event in snapshot(tmp_path)[1]
        if event["kind"] == "ObservationEvent" and event["tool_name"] == "doonex_shell"
    ]
    assert len(observations) == 2
    assert all(item["is_error"] and item["exit_code"] == 1 for item in observations)
