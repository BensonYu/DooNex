"""Pinned SDK scheduling contracts: real loop/persistence, synthetic transport."""

import copy
import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from doonex import sdk
from test_sdk import SECRET, answer, harness


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("SDK scheduling contracts must never connect to a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def two_tools(*, first="first", second="second"):
    response = answer("doonex_shell", {"command": first})
    response.choices[0].message.tool_calls.extend(
        answer("doonex_shell", {"command": second}, sequence=2)
        .choices[0]
        .message.tool_calls
    )
    return response


def snapshot(root):
    saved = next(root.glob("*/base_state.json"))
    state = json.loads(saved.read_text())
    events = [
        json.loads(path.read_text())
        for path in sorted((saved.parent / "events").glob("*.json"))
    ]
    return state, events


def settings(receipts, should_yield=lambda: False, **overrides):
    arguments = {
        "generation": 0,
        "should_yield": should_yield,
        "on_yield": receipts.append,
    }
    arguments.update(overrides)
    return sdk.SessionScheduling(**arguments)


def resume_hooks(receipt, **overrides):
    arguments = {
        "resume_receipt": receipt,
        "authorize_resume": lambda received: received == receipt,
    }
    arguments.update(overrides)
    return settings([], **arguments)


@pytest.mark.parametrize("boundary", ["after_model", "after_first", "before_second"])
def test_yield_keeps_known_pending_and_resumes_each_tool_once(
    tmp_path, monkeypatch, boundary
):
    calls, reservations, settled, commands, identities, receipts = (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    requested = False

    def transport(*args, **kwargs):
        calls.append(1)
        return (
            two_tools()
            if len(calls) == 1
            else answer("finish", {"message": "done"}, sequence=3)
        )

    def after(reservation, usage):
        nonlocal requested
        settled.append(usage)
        if boundary == "after_model":
            requested = True

    def shell(command, identity):
        nonlocal requested
        identities.append(identity)
        if boundary == "before_second" and command == "second" and len(calls) == 1:
            raise sdk.SchedulerYield()
        commands.append(command)
        if boundary == "after_first":
            requested = True
        return {"output": "done", "exit_code": 0}

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, *_ = harness(
        tmp_path,
        before_model=lambda *args: reservations.append(1),
        after_model=after,
        shell_with_action=shell,
        scheduling=settings(receipts, lambda: requested),
    )
    assert sdk.run_session(**kwargs) == "yielded"
    assert len(calls) == len(reservations) == len(settled) == 1
    expected_done = [] if boundary == "after_model" else ["first"]
    assert commands == expected_done
    before_state, before_events = snapshot(tmp_path)
    assert before_state["execution_status"] == "paused"
    kinds = [item["kind"] for item in before_events]
    assert "PauseEvent" in kinds
    assert not {"AgentErrorEvent", "ConversationErrorEvent"} & set(kinds)
    assert sum(item["kind"] == "ObservationEvent" for item in before_events) == len(
        expected_done
    )
    receipt = receipts[0]
    assert receipt["session_id"] == before_state["id"]
    assert receipt["leaf_event_id"] == before_state["leaf_event_id"]
    assert len(receipt["pending_actions"]) == 2 - len(expected_done)
    assert receipt["settled_responses"] == [
        {"response_id": "response-1", "input_tokens": 100, "output_tokens": 20}
    ]
    requested = False

    def remaining(command, identity):
        identities.append(identity)
        commands.append(command)
        return {"output": "done", "exit_code": 0}

    kwargs.update(
        prompt=None,
        resume=True,
        scheduling=resume_hooks(receipt),
        shell_with_action=remaining,
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert commands == ["first", "second"]
    assert len(calls) == len(settled) == len(reservations) == 2
    after_state, _ = snapshot(tmp_path)
    assert after_state["id"] == before_state["id"]
    for identity in identities:
        assert identity["session_id"] == receipt["session_id"]
        assert identity["session_generation"] == 0
        assert identity["tool_name"] == "doonex_shell"
        assert len(identity["action_sha256"]) == len(identity["event_sha256"]) == 64
    if boundary == "before_second":
        assert identities[1] == identities[2]
    assert not sdk._SCHEDULING and not sdk._CALLBACKS


def test_before_model_transaction_signal_yields_without_reserving_or_unknown_usage(
    tmp_path, monkeypatch
):
    receipts, settlements, commands = [], [], []

    def before(*args):
        raise sdk.SchedulerYield()

    def transport(*args, **kwargs):
        pytest.fail("scheduler admission rejected this request before dispatch")

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, reservations, *_ = harness(
        tmp_path,
        before_model=before,
        after_model=lambda *args: settlements.append(args),
        shell=lambda command: commands.append(command),
        scheduling=settings(receipts),
    )
    assert sdk.run_session(**kwargs) == "yielded"
    assert not reservations and not settlements and not commands
    assert receipts[0]["settled_responses"] == receipts[0]["pending_actions"] == []
    assert not {"ConversationErrorEvent", "AgentErrorEvent"} & {
        event["kind"] for event in snapshot(tmp_path)[1]
    }
    monkeypatch.setattr(
        sdk.GuardedLLM,
        "_transport_call",
        lambda *args, **kwargs: answer("finish", {"message": "done"}),
    )
    kwargs.update(
        prompt=None,
        resume=True,
        before_model=lambda *args: object(),
        scheduling=resume_hooks(receipts[0]),
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert len(settlements) == 1


def test_signal_at_next_model_admission_preserves_completed_tool(tmp_path, monkeypatch):
    calls, commands, receipts, settled = [], [], [], []

    def transport(*args, **kwargs):
        calls.append(1)
        return answer("doonex_shell", {"command": "once"})

    def before(*args):
        if settled:
            raise sdk.SchedulerYield()
        return object()

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, *_ = harness(
        tmp_path,
        before_model=before,
        after_model=lambda *args: settled.append(args),
        shell=lambda command: commands.append(command) or {"exit_code": 0},
        scheduling=settings(receipts),
    )
    assert sdk.run_session(**kwargs) == "yielded"
    assert calls == [1] and commands == ["once"] and len(settled) == 1
    assert receipts[0]["pending_actions"] == []
    assert len(receipts[0]["settled_responses"]) == 1
    assert not {"AgentErrorEvent", "ConversationErrorEvent"} & {
        item["kind"] for item in snapshot(tmp_path)[1]
    }


def test_yield_after_last_tool_stops_before_reservation_and_finish_has_priority(
    tmp_path, monkeypatch
):
    requested = False
    receipts, calls, commands = [], [], []

    def shell(command):
        nonlocal requested
        commands.append(command)
        requested = True
        return {"output": "done", "exit_code": 0}

    def transport(*args, **kwargs):
        calls.append(1)
        return (
            answer("doonex_shell", {"command": "once"})
            if len(calls) == 1
            else answer("finish", {"message": "done"}, sequence=2)
        )

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, reservations, *_ = harness(
        tmp_path, shell=shell, scheduling=settings(receipts, lambda: requested)
    )
    assert sdk.run_session(**kwargs) == "yielded"
    assert commands == ["once"] and len(calls) == len(reservations) == 1
    assert receipts[0]["pending_actions"] == []
    requested = False

    def event(item):
        nonlocal requested
        if item["kind"] == "ObservationEvent" and item["tool_name"] == "finish":
            requested = True

    kwargs.update(
        prompt=None,
        resume=True,
        on_event=event,
        scheduling=resume_hooks(receipts[0], should_yield=lambda: requested),
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert len(receipts) == 1
    assert len(calls) == 2 and commands == ["once"]


def paused_before_tools(tmp_path, monkeypatch):
    receipts = []
    requested = False
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", lambda *a, **k: two_tools())

    def after(*args):
        nonlocal requested
        requested = True

    kwargs, *_ = harness(
        tmp_path,
        after_model=after,
        scheduling=settings(receipts, lambda: requested),
    )
    assert sdk.run_session(**kwargs) == "yielded"
    return kwargs, receipts[0]


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "schema_bool",
        "session",
        "generation",
        "head",
        "branch",
        "action_id",
        "action_hash",
        "event_hash",
        "tool_call_id",
        "pending",
        "usage_missing",
        "usage_unknown",
        "extra",
        "denied",
        "truthy",
        "no_authorizer",
        "new_prompt",
    ],
)
def test_resume_receipt_and_product_authorization_fail_closed(
    tmp_path, monkeypatch, change
):
    kwargs, receipt = paused_before_tools(tmp_path, monkeypatch)
    invalid = copy.deepcopy(receipt)
    overrides = {}
    prompt = None
    if change == "missing":
        invalid = None
    elif change == "schema_bool":
        invalid["schema"] = True
    elif change == "session":
        invalid["session_id"] = "different-session"
    elif change == "generation":
        invalid["session_generation"] = 1
    elif change == "head":
        invalid["leaf_event_id"] = "changed-head"
    elif change == "branch":
        invalid["branch_sha256"] = "0" * 64
    elif change in {"action_id", "action_hash", "event_hash", "tool_call_id"}:
        key = {"action_hash": "action_sha256", "event_hash": "event_sha256"}.get(
            change, change
        )
        invalid["pending_actions"][0][key] = "changed"
    elif change == "pending":
        invalid["pending_actions"].pop()
    elif change == "usage_missing":
        invalid["settled_responses"] = []
    elif change == "usage_unknown":
        invalid["settled_responses"][0]["input_tokens"] = None
    elif change == "extra":
        invalid["trusted"] = True
    elif change == "denied":
        overrides["authorize_resume"] = lambda received: False
    elif change == "truthy":
        overrides["authorize_resume"] = lambda received: 1
    elif change == "no_authorizer":
        overrides["authorize_resume"] = None
    elif change == "new_prompt":
        prompt = "alter the task"
    kwargs.update(
        prompt=prompt,
        resume=True,
        scheduling=resume_hooks(invalid, **overrides),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("invalid checkpoint must not reserve or dispatch")

    kwargs["before_model"] = kwargs["shell"] = forbidden
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)
    with pytest.raises((sdk.SessionRecoveryRequired, ValueError)):
        sdk.run_session(**kwargs)


@pytest.mark.parametrize("target", ["action", "observation", "leaf"])
def test_changed_persisted_state_cannot_use_original_receipt(
    tmp_path, monkeypatch, target
):
    requested = False
    receipts = []

    def shell(command):
        nonlocal requested
        requested = True
        return {"output": "first outcome", "exit_code": 0}

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", lambda *a, **k: two_tools())
    kwargs, *_ = harness(
        tmp_path,
        shell=shell,
        scheduling=settings(receipts, lambda: requested),
    )
    assert sdk.run_session(**kwargs) == "yielded"
    saved = next(tmp_path.glob("*/base_state.json"))
    if target == "leaf":
        value = json.loads(saved.read_text())
        paths = sorted((saved.parent / "events").glob("*.json"))
        value["leaf_event_id"] = json.loads(paths[-2].read_text())["id"]
        saved.write_text(json.dumps(value))
    else:
        kind = "ActionEvent" if target == "action" else "ObservationEvent"
        path = next(
            item
            for item in (saved.parent / "events").glob("*.json")
            if json.loads(item.read_text())["kind"] == kind
        )
        value = json.loads(path.read_text())
        if target == "action":
            value["action"]["command"] = "different-command"
        else:
            value["observation"]["content"][0]["text"] = "different-outcome"
        path.write_text(json.dumps(value))
    kwargs.update(prompt=None, resume=True, scheduling=resume_hooks(receipts[0]))
    with pytest.raises(sdk.SessionRecoveryRequired):
        sdk.run_session(**kwargs)


def test_plain_resume_still_refuses_scheduler_pending_without_receipt(
    tmp_path, monkeypatch
):
    kwargs, _ = paused_before_tools(tmp_path, monkeypatch)
    kwargs.update(prompt=None, resume=True, scheduling=None)
    with pytest.raises(sdk.SessionRecoveryRequired, match="no recorded outcome"):
        sdk.run_session(**kwargs)


@pytest.mark.parametrize("failure", ["crash", "keyboard", "illegal_signal"])
def test_unknown_tool_outcome_never_becomes_scheduler_yield(
    tmp_path, monkeypatch, failure
):
    receipts, effects = [], []

    class Crash(BaseException):
        pass

    error = {
        "crash": Crash,
        "keyboard": KeyboardInterrupt,
        "illegal_signal": sdk.SchedulerYield,
    }[failure]

    def shell(command):
        effects.append(command)
        raise error()

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", lambda *a, **k: two_tools())
    kwargs, *_ = harness(tmp_path, shell=shell, scheduling=settings(receipts))
    with pytest.raises(error):
        sdk.run_session(**kwargs)
    assert effects == ["first"] and receipts == []
    kwargs.update(prompt=None, resume=True)
    with pytest.raises(sdk.SessionRecoveryRequired):
        sdk.run_session(**kwargs)
    assert effects == ["first"]


def test_first_observation_is_on_disk_before_second_tool_can_crash(
    tmp_path, monkeypatch
):
    effects, receipts = [], []

    class Crash(BaseException):
        pass

    def shell(command, identity):
        effects.append(command)
        if command == "second":
            _, events = snapshot(tmp_path)
            actions = {
                item["id"]: item["action"]["command"]
                for item in events
                if item["kind"] == "ActionEvent"
            }
            completed = [
                actions[item["action_id"]]
                for item in events
                if item["kind"] == "ObservationEvent"
            ]
            assert completed == ["first"]
            raise Crash()
        return {"output": "first is durable", "exit_code": 0}

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", lambda *a, **k: two_tools())
    kwargs, *_ = harness(
        tmp_path,
        shell_with_action=shell,
        scheduling=settings(receipts),
    )
    with pytest.raises(Crash):
        sdk.run_session(**kwargs)
    assert effects == ["first", "second"] and receipts == []
    kwargs.update(prompt=None, resume=True, scheduling=None, shell_with_action=None)
    with pytest.raises(sdk.SessionRecoveryRequired, match="no recorded outcome"):
        sdk.run_session(**kwargs)
    assert effects == ["first", "second"]


@pytest.mark.parametrize("point", ["after_model", "event"])
def test_signal_from_unapproved_callback_does_not_make_receipt(
    tmp_path, monkeypatch, point
):
    receipts = []

    def signal(*args):
        raise sdk.SchedulerYield()

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", lambda *a, **k: two_tools())
    kwargs, *_ = harness(
        tmp_path,
        scheduling=settings(receipts),
        **({"after_model": signal} if point == "after_model" else {"on_event": signal}),
    )
    with pytest.raises(sdk.SchedulerYield):
        sdk.run_session(**kwargs)
    assert receipts == []


def test_signal_during_pause_callback_is_not_accepted_by_previous_dispatch_signal(
    tmp_path, monkeypatch
):
    receipts = []

    def shell(command, identity):
        raise sdk.SchedulerYield()

    def event(item):
        if item["kind"] == "PauseEvent":
            raise sdk.SchedulerYield()

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", lambda *a, **k: two_tools())
    kwargs, *_ = harness(
        tmp_path,
        on_event=event,
        shell_with_action=shell,
        scheduling=settings(receipts),
    )
    with pytest.raises(sdk.SchedulerYield):
        sdk.run_session(**kwargs)
    assert receipts == []


def test_ordinary_stop_keeps_its_existing_status_without_scheduler_receipt(
    tmp_path, monkeypatch
):
    receipts = []
    stopped = False

    def shell(command):
        nonlocal stopped
        stopped = True
        return {"output": "done", "exit_code": 0}

    monkeypatch.setattr(
        sdk.GuardedLLM,
        "_transport_call",
        lambda *a, **k: answer("doonex_shell", {"command": "once"}),
    )
    kwargs, *_ = harness(
        tmp_path,
        shell=shell,
        should_stop=lambda: stopped,
        scheduling=settings(receipts),
    )
    assert sdk.run_session(**kwargs) == "paused"
    assert receipts == []


def test_repeated_yield_updates_receipt_without_sampling_or_replaying(
    tmp_path, monkeypatch
):
    kwargs, receipt = paused_before_tools(tmp_path, monkeypatch)
    receipts = []
    kwargs.update(
        prompt=None,
        resume=True,
        scheduling=resume_hooks(
            receipt,
            should_yield=lambda: True,
            on_yield=receipts.append,
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("immediate re-yield must not sample or execute")

    kwargs["before_model"] = kwargs["shell"] = forbidden
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)
    assert sdk.run_session(**kwargs) == "yielded"
    assert receipts[0]["leaf_event_id"] != receipt["leaf_event_id"]
    assert receipts[0]["branch_sha256"] != receipt["branch_sha256"]
    assert receipts[0]["pending_actions"] == receipt["pending_actions"]
    kwargs["scheduling"] = resume_hooks(receipt)
    with pytest.raises(sdk.SessionRecoveryRequired):
        sdk.run_session(**kwargs)


def test_responses_yield_preserves_native_pending_tool_identity(tmp_path, monkeypatch):
    from test_sdk import response_harness, responses_answer

    requested = False
    receipts, calls, commands = [], [], []

    def transport(**kwargs):
        calls.append(1)
        return (
            responses_answer("doonex_shell", {"command": "once"})
            if len(calls) == 1
            else responses_answer("finish", {"message": "done"}, sequence=2)
        )

    def after(*args):
        nonlocal requested
        requested = True

    kwargs, *_ = response_harness(
        tmp_path,
        monkeypatch,
        transport,
        after_model=after,
        scheduling=settings(receipts, lambda: requested),
    )
    assert sdk.run_session(**kwargs) == "yielded"
    assert receipts[0]["pending_actions"][0]["llm_response_id"] == "resp_1"
    assert receipts[0]["settled_responses"] == [
        {"response_id": "resp_1", "input_tokens": 100, "output_tokens": 20}
    ]
    kwargs.update(
        prompt=None,
        resume=True,
        after_model=lambda *args: None,
        shell_with_action=lambda command, identity: (
            commands.append((command, identity)) or {"exit_code": 0}
        ),
        scheduling=resume_hooks(receipts[0]),
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert len(calls) == 2
    assert len(commands) == 1 and commands[0][0] == "once"
    assert commands[0][1] == receipts[0]["pending_actions"][0]


def test_unknown_model_usage_refuses_checkpoint_instead_of_zeroing_it(
    tmp_path, monkeypatch
):
    receipts, settlements = [], []
    requested = False

    def after(reservation, usage):
        nonlocal requested
        settlements.append(usage)
        requested = True

    monkeypatch.setattr(
        sdk.GuardedLLM,
        "_transport_call",
        lambda *a, **k: answer("doonex_shell", {"command": "first"}, usage=False),
    )
    kwargs, *_ = harness(
        tmp_path,
        after_model=after,
        scheduling=settings(receipts, lambda: requested),
    )
    with pytest.raises(sdk.SessionRecoveryRequired, match="unknown model response"):
        sdk.run_session(**kwargs)
    assert settlements == [None] and receipts == []


def test_receipt_persistence_failure_does_not_report_success(tmp_path, monkeypatch):
    requested = False

    def after(*args):
        nonlocal requested
        requested = True

    def failed_receipt(receipt):
        raise RuntimeError("product ledger rejected unresolved tool/budget")

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", lambda *a, **k: two_tools())
    kwargs, *_ = harness(
        tmp_path,
        after_model=after,
        scheduling=settings([], lambda: requested, on_yield=failed_receipt),
    )
    with pytest.raises(RuntimeError, match="ledger rejected"):
        sdk.run_session(**kwargs)
    assert snapshot(tmp_path)[0]["execution_status"] == "paused"
    kwargs.update(prompt=None, resume=True, scheduling=resume_hooks(None))
    with pytest.raises(sdk.SessionRecoveryRequired):
        sdk.run_session(**kwargs)


def test_action_identity_uses_sdk_ids_for_identical_commands_and_redacts_secret(
    tmp_path, monkeypatch
):
    requested = False
    receipts, identities, calls = [], [], []

    def after(*args):
        nonlocal requested
        requested = True

    def transport(*args, **kwargs):
        calls.append(1)
        return (
            two_tools(first=f"same {SECRET}", second=f"same {SECRET}")
            if len(calls) == 1
            else answer("finish", {"message": "done"}, sequence=3)
        )

    def shell(command, identity):
        assert SECRET not in command
        identities.append(identity)
        return {"output": "done", "exit_code": 0}

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, *_ = harness(
        tmp_path,
        after_model=after,
        shell_with_action=shell,
        scheduling=settings(receipts, lambda: requested),
    )
    assert sdk.run_session(**kwargs) == "yielded"
    requested = False
    kwargs.update(
        prompt=None,
        resume=True,
        after_model=lambda *args: None,
        scheduling=resume_hooks(receipts[0]),
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert len(identities) == 2
    assert identities[0]["action_id"] != identities[1]["action_id"]
    assert identities[0]["tool_call_id"] != identities[1]["tool_call_id"]
    assert identities[0]["action_sha256"] == identities[1]["action_sha256"]
    assert SECRET not in json.dumps(receipts)
    assert SECRET not in "".join(path.read_text() for path in tmp_path.rglob("*.json"))


def test_scheduler_checkpoint_resumes_same_session_in_fresh_process(tmp_path):
    script = textwrap.dedent("""
        import json
        import socket
        import sys
        from pathlib import Path
        def forbidden(*args, **kwargs):
            raise AssertionError("offline scheduling contract")
        socket.socket.connect = forbidden
        from doonex import sdk
        from test_sdk import answer, harness
        from test_sdk_scheduling import two_tools

        stage, target = sys.argv[1:]
        root = Path(target)
        root.mkdir(exist_ok=True)
        receipt_path = root / "product-receipt.json"
        counts_path = root / "effects.json"
        requested = False
        calls = 0
        def transport(*args, **kwargs):
            global calls
            calls += 1
            return two_tools() if stage == "create" else answer("finish", {"message":"done"}, sequence=3)
        sdk.GuardedLLM._transport_call = transport
        def shell(command, identity):
            global requested
            counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
            counts[command] = counts.get(command, 0) + 1
            counts_path.write_text(json.dumps(counts))
            if stage == "create":
                requested = True
            return {"output":"done", "exit_code":0}
        receipt = json.loads(receipt_path.read_text()) if stage == "resume" else None
        def save(received):
            receipt_path.write_text(json.dumps(received))
        schedule = sdk.SessionScheduling(
            generation=7, should_yield=lambda:requested, on_yield=save,
            resume_receipt=receipt,
            authorize_resume=lambda received: received == json.loads(receipt_path.read_text()),
        )
        kwargs, *_ = harness(
            root / "session", run_id="cross-process-scheduling",
            prompt="Synthetic task" if stage == "create" else None,
            resume=stage == "resume", scheduling=schedule, shell_with_action=shell,
        )
        status = sdk.run_session(**kwargs)
        saved = next((root / "session").glob("*/base_state.json"))
        print(json.dumps({"status":status, "session_id":json.loads(saved.read_text())["id"],
            "calls":calls, "effects":json.loads(counts_path.read_text())}))
    """)
    environment = dict(os.environ)
    checkout = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(checkout / "apps/server"), str(checkout / "tests")]
    )
    summaries = []
    for stage in ("create", "resume"):
        result = subprocess.run(
            [sys.executable, "-c", script, stage, str(tmp_path)],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        summaries.append(json.loads(result.stdout.strip().splitlines()[-1]))
    assert summaries[0]["status"] == "yielded"
    assert summaries[1]["status"] == "finished"
    assert summaries[0]["session_id"] == summaries[1]["session_id"]
    assert summaries[0]["effects"] == {"first": 1}
    assert summaries[1]["effects"] == {"first": 1, "second": 1}
    assert [item["calls"] for item in summaries] == [1, 1]


def test_receipt_branch_digest_has_canonical_event_binding(tmp_path, monkeypatch):
    _, receipt = paused_before_tools(tmp_path, monkeypatch)
    _, events = snapshot(tmp_path)
    # The SDK serializes defaults into the in-memory model; disk JSON omits
    # defaults, so verify the exact normalized SDK representation, not raw JSON.
    from openhands.sdk.event import Event

    normalized = [Event.model_validate(item).model_dump(mode="json") for item in events]
    assert receipt["branch_sha256"] == sdk._canonical_sha256(normalized)
