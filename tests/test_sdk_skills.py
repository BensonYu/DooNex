"""Real pinned SDK initialization, event persistence and resume; fake model only."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from doonex import sdk
from litellm import ModelResponse
from openhands.sdk.context import Skill

MODEL = {
    "provider": "openai",
    "model_id": "gpt-4.1-mini",
    "api_mode": "chat",
    "max_input_tokens": 100000,
    "max_output_tokens": 1000,
    "timeout_seconds": 10,
}
SECRET = "DOONEX_FAKE_SKILL_SECRET_912a"
VERSION = "a" * 64


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must replace provider transport before any model request")

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)


def response(tool="finish", arguments=None, sequence=1):
    return ModelResponse(
        id=f"skill-response-{sequence}",
        model=MODEL["model_id"],
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"skill-call-{sequence}",
                            "type": "function",
                            "function": {
                                "name": tool,
                                "arguments": json.dumps(
                                    arguments or {"message": "done"}
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def harness(state_dir, **changes):
    reservations, settlements, commands, events = [], [], [], []
    kwargs = {
        "run_id": "sdk-skills-contract",
        "state_dir": state_dir,
        "prompt": "Complete the authorized task",
        "model": MODEL,
        "api_key": SECRET,
        "before_model": lambda inputs, outputs: reservations.append((inputs, outputs)),
        "after_model": lambda ticket, usage: settlements.append(usage),
        "shell": lambda command: (
            commands.append(command) or {"output": "container result", "exit_code": 0}
        ),
        "on_event": events.append,
        "should_stop": lambda: False,
    }
    kwargs.update(changes)
    return kwargs, reservations, settlements, commands, events


@pytest.fixture
def selected():
    return {
        "version_id": VERSION,
        "instructions": "Opaque Skill sentinel. Preserve ${HOME} and {{ 7 * 7 }}.",
        "assets": ["assets/reference.txt", "scripts/check.py"],
    }


def test_native_skill_is_opaque_and_does_not_read_container_assets_on_host(
    tmp_path, monkeypatch, selected
):
    import openhands.sdk.skills.skill as native

    marker = tmp_path / "must-not-exist"
    selected["instructions"] = (
        "---\nname: external\nshell: /bin/sh\n"
        "allowed-tools: terminal file_editor invoke_skill\n"
        f"mcp_tools: {{host: {{command: touch, args: ['{marker}']}}}}\n"
        f"hooks: {{session_start: 'touch {marker}'}}\n---\n"
        f"Original inline syntax !`touch {marker}` must remain opaque.\n"
        + selected["instructions"]
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Skill text must not execute, parse frontmatter or load host files")

    original_open = Path.open

    def bounded_open(path, *args, **kwargs):
        assert not str(path).startswith("/skills/")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", bounded_open)
    monkeypatch.setattr(Skill, "load", forbidden)
    monkeypatch.setattr(native.frontmatter, "load", forbidden)
    monkeypatch.setattr(native, "render_content_with_commands", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(sdk.os, "system", forbidden)
    runtime = sdk._runtime_skill(selected)
    assert isinstance(runtime, Skill)
    assert runtime.render_content(working_dir=tmp_path) == runtime.content
    assert selected["instructions"] in runtime.content
    assert runtime.source == "/skills/SKILL.md"
    assert "/skills/assets/reference.txt" in runtime.content
    assert runtime.mcp_tools is runtime.resources is runtime.trigger is None
    requests = []

    def transport(self, **kwargs):
        requests.append(kwargs)
        text = json.dumps(kwargs["messages"], ensure_ascii=False)
        assert selected["instructions"] in "\n".join(
            item["text"]
            for message in kwargs["messages"]
            for item in message["content"]
            if isinstance(item, dict) and item.get("type") == "text"
        )
        assert "${HOME}" in text and "{{ 7 * 7 }}" in text
        assert {item["function"]["name"] for item in kwargs["tools"]} == {
            "doonex_shell",
            "finish",
        }
        return response()

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, reservations, settlements, commands, _ = harness(
        tmp_path / "state", skill=selected
    )
    assert sdk.run_session(**kwargs) == "finished"
    assert len(requests) == len(reservations) == len(settlements) == 1
    assert commands == []
    assert not marker.exists()


def test_real_sdk_pause_persist_and_resume_keeps_opaque_skill_and_tool_boundary(
    tmp_path, monkeypatch, selected
):
    import openhands.sdk.agent.base as agent_base
    import openhands.sdk.conversation.impl.local_conversation as local
    from openhands.sdk.llm.llm_profile_store import LLMProfileStore

    def forbidden(*args, **kwargs):
        pytest.fail("Skill sessions must not discover host plugins, tools or skills")

    for name in (
        "load_available_plugins",
        "register_file_agents",
        "load_available_skills",
        "load_memory",
        "load_marketplace_standalone_skills",
    ):
        monkeypatch.setattr(local, name, forbidden)
    monkeypatch.setattr(agent_base, "has_vision_profile_available", forbidden)
    monkeypatch.setattr(agent_base, "_load_soul_md", forbidden)
    monkeypatch.setattr(LLMProfileStore, "load", forbidden)
    monkeypatch.setattr(Skill, "render_content", forbidden)
    observed = []

    class ObservedConversation(sdk._M1Conversation):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            skill = self.agent.agent_context.skills[0]
            assert isinstance(skill, sdk._OpaqueSkill)
            assert skill.render_content() == skill.content
            observed.append(skill)

    monkeypatch.setattr(sdk, "_M1Conversation", ObservedConversation)
    stopped = False
    requests = []

    def shell(command):
        nonlocal stopped
        stopped = True
        return {"output": "read only container asset", "exit_code": 0}

    def transport(self, **kwargs):
        requests.append(kwargs)
        assert "Opaque Skill sentinel" in str(kwargs["messages"])
        assert {item["function"]["name"] for item in kwargs["tools"]} == {
            "doonex_shell",
            "finish",
        }
        return (
            response("doonex_shell", {"command": "cat /skills/assets/reference.txt"})
            if len(requests) == 1
            else response(sequence=2)
        )

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, *_ = harness(
        tmp_path, skill=selected, shell=shell, should_stop=lambda: stopped
    )
    assert sdk.run_session(**kwargs) == "paused"
    saved_path = next(tmp_path.rglob("base_state.json"))
    persisted = json.loads(saved_path.read_text())
    saved_skill = persisted["agent"]["agent_context"]["skills"][0]
    assert selected["instructions"] in saved_skill["content"]
    assert saved_skill["metadata"]["doonex_version_id"] == VERSION
    assert len(saved_skill["metadata"]["doonex_payload_sha256"]) == 64
    resumed, reservations, settlements, commands, _ = harness(
        tmp_path,
        skill=selected,
        prompt=None,
        resume=True,
        api_key="replacement-fake-key",
    )
    assert sdk.run_session(**resumed) == "finished"
    assert len(observed) == len(requests) == 2
    assert observed[0].model_dump() == observed[1].model_dump()
    assert len(reservations) == len(settlements) == 1
    assert commands == []  # The completed container action was not replayed.


@pytest.mark.parametrize("change", ["version_id", "instructions", "assets", "remove"])
def test_resume_rejects_changed_or_removed_frozen_skill_before_model_call(
    tmp_path, monkeypatch, selected, change
):
    first, *_ = harness(tmp_path, skill=selected, should_stop=lambda: True)
    assert sdk.run_session(**first) == "paused"
    saved_path = next(tmp_path.rglob("base_state.json"))
    original = saved_path.read_bytes()
    changed = deepcopy(selected)
    if change == "remove":
        changed = None
    else:
        changed[change] = {
            "version_id": "b" * 64,
            "instructions": "Different instructions",
            "assets": ["different.txt"],
        }[change]
    resumed, reservations, settlements, commands, _ = harness(
        tmp_path, skill=changed, resume=True, prompt=None
    )
    with pytest.raises(ValueError, match="frozen context"):
        sdk.run_session(**resumed)
    assert saved_path.read_bytes() == original
    assert reservations == settlements == commands == []


def test_resume_cannot_add_skill_to_existing_baseline(tmp_path, selected):
    first, *_ = harness(tmp_path, should_stop=lambda: True)
    assert sdk.run_session(**first) == "paused"
    resumed, *_ = harness(tmp_path, skill=selected, resume=True, prompt=None)
    with pytest.raises(ValueError, match="frozen context"):
        sdk.run_session(**resumed)


@pytest.mark.parametrize("change", ["mcp_tools", "load_user_skills", "marketplace"])
def test_resume_rejects_persisted_context_tampering_before_sdk_deserialization(
    tmp_path, selected, change
):
    first, *_ = harness(tmp_path, skill=selected, should_stop=lambda: True)
    assert sdk.run_session(**first) == "paused"
    saved_path = next(tmp_path.rglob("base_state.json"))
    state = json.loads(saved_path.read_text())
    context = state["agent"]["agent_context"]
    if change == "mcp_tools":
        context["skills"][0]["mcp_tools"] = {"host": {"command": "sh"}}
    elif change == "marketplace":
        context["registered_marketplaces"] = [{"source": "https://invalid.example"}]
    else:
        context[change] = True
    saved_path.write_text(json.dumps(state))
    before = saved_path.read_bytes()
    resumed, reservations, settlements, commands, _ = harness(
        tmp_path, skill=selected, resume=True, prompt=None
    )
    with pytest.raises(ValueError, match="frozen context"):
        sdk.run_session(**resumed)
    assert saved_path.read_bytes() == before
    assert reservations == settlements == commands == []


def test_reviewer_rejects_skill_before_creating_state(tmp_path, selected):
    state = tmp_path / "review"
    kwargs, *_ = harness(state, role="reviewer", skill=selected)
    with pytest.raises(ValueError, match="only for the executor"):
        sdk.run_session(**kwargs)
    assert not state.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"version_id": "../host"},
        {"instructions": ""},
        {"instructions": "x\x00y"},
        {"instructions": "x" * (2 * 1024 * 1024 + 1)},
        {"assets": ["/etc/passwd"]},
        {"assets": ["../host"]},
        {"assets": ["a/./b"]},
        {"assets": ["a//b"]},
        {"assets": ["C:\\secret"]},
        {"assets": ["a\nb"]},
        {"assets": ["same", "same"]},
        {"path": "/host/path/not-accepted"},
        {"mcp_tools": {"host": {"command": "sh"}}},
    ],
)
def test_invalid_payloads_fail_without_state_or_secret_echo(tmp_path, selected, change):
    selected.update(change)
    state = tmp_path / "invalid"
    kwargs, reservations, settlements, commands, _ = harness(state, skill=selected)
    with pytest.raises(ValueError, match="Selected Skill payload is invalid") as error:
        sdk.run_session(**kwargs)
    assert SECRET not in str(error.value)
    assert not state.exists()
    assert reservations == settlements == commands == []


def test_skill_context_counts_toward_input_budget_before_reservation(
    tmp_path, selected, monkeypatch
):
    selected["instructions"] = "x" * 20000
    kwargs, reservations, settlements, commands, _ = harness(
        tmp_path, skill=selected, model={**MODEL, "max_input_tokens": 16384}
    )
    with pytest.raises(Exception, match="input token bound"):
        sdk.run_session(**kwargs)
    assert reservations == settlements == commands == []


def test_echoed_key_in_skill_does_not_enter_sdk_state_or_model_context(
    tmp_path, selected, monkeypatch
):
    selected["instructions"] += " Credential echo: " + SECRET

    def transport(self, **kwargs):
        assert SECRET not in str(kwargs["messages"])
        assert "[REDACTED]" in str(kwargs["messages"])
        return response()

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, *_ = harness(tmp_path, skill=selected)
    assert sdk.run_session(**kwargs) == "finished"
    assert SECRET not in "".join(path.read_text() for path in tmp_path.rglob("*.json"))
