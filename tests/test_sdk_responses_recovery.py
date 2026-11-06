"""Stateless Responses replay contracts; every HTTP response is synthetic."""

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
from test_sdk import MODEL, harness, responses_answer
from test_sdk_scheduling import resume_hooks, settings


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Responses recovery contracts must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.mark.parametrize("reasoning", [False, True])
def test_stateless_http_input_is_identical_after_scheduler_resume(
    tmp_path, monkeypatch, reasoning
):
    import httpx

    bodies = {}
    active = None
    responses = responses_answer("doonex_shell", {"command": "create-once"})
    first = responses.model_dump(mode="json")
    first["output"][0]["id"] = "fc_original_item_id_0123456789"
    first["output"][0]["call_id"] = "call_original_link_0123456789"
    if reasoning:
        first["output"].insert(
            0,
            {
                "id": "rs_original_reasoning_0123456789",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Synthetic summary"}],
                "encrypted_content": "synthetic-encrypted-reasoning",
            },
        )
    first["output"].insert(
        -1,
        {
            "id": "msg_original_assistant_0123456789",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "Synthetic thought", "annotations": []}
            ],
        },
    )

    def transport(self, request, *args, **kwargs):
        assert str(request.url) == "https://responses-gateway.invalid/v1/responses"
        body = json.loads(request.content)
        bodies[active].append(body)
        assert body["store"] is False
        if len(bodies[active]) == 1:
            result = first
        else:
            result = responses_answer("finish", {"message": "done"}, sequence=2)
            result = result.model_dump(mode="json")
        return httpx.Response(200, request=request, json=copy.deepcopy(result))

    def forbidden_chat(*args, **kwargs):
        pytest.fail("Responses replay must never fall back to Chat Completions")

    monkeypatch.setattr(httpx.Client, "send", transport)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden_chat)
    for boundary in ("uninterrupted", "before_tool", "after_tool"):
        active = boundary
        bodies[boundary] = []
        receipts, commands = [], []
        flag = {"requested": False}

        def after(reservation, usage, *, flag=flag, boundary=boundary):
            if boundary == "before_tool":
                flag["requested"] = True

        def shell(command, *, flag=flag, boundary=boundary, commands=commands):
            commands.append(command)
            if boundary == "after_tool":
                flag["requested"] = True
            return {"output": "created", "exit_code": 0}

        arguments, *_ = harness(
            tmp_path / boundary,
            model={
                **MODEL,
                "api_mode": "responses",
                "model_id": "gpt-6.1-sol",
                "base_url": "https://responses-gateway.invalid/v1",
            },
            after_model=after,
            shell=shell,
            scheduling=settings(receipts, lambda flag=flag: flag["requested"]),
        )
        if boundary == "uninterrupted":
            assert sdk.run_session(**arguments) == "finished"
        else:
            assert sdk.run_session(**arguments) == "yielded"
            assert commands == ([] if boundary == "before_tool" else ["create-once"])
            flag["requested"] = False
            arguments.update(
                prompt=None,
                resume=True,
                scheduling=resume_hooks(receipts[0]),
                after_model=lambda *args: None,
            )
            assert sdk.run_session(**arguments) == "finished"
        assert commands == ["create-once"]
        assert len(bodies[boundary]) == 2
        second = bodies[boundary][1]
        calls = [item for item in second["input"] if item["type"] == "function_call"]
        outputs = [
            item for item in second["input"] if item["type"] == "function_call_output"
        ]
        assert len(calls) == len(outputs) == 1, (boundary, second["input"])
        assert calls[0]["id"] == first["output"][-1]["id"]
        assert calls[0]["call_id"] == outputs[0]["call_id"] == (
            "call_original_link_0123456789"
        )
        prior_reasoning = [item for item in second["input"] if item["type"] == "reasoning"]
        assert prior_reasoning == ([first["output"][0]] if reasoning else [])

    assert bodies["uninterrupted"] == bodies["before_tool"] == bodies["after_tool"]


@pytest.mark.parametrize("boundary", ["before_tool", "after_first"])
def test_responses_pending_batch_reloads_in_fresh_process(tmp_path, boundary):
    """No in-process view or provider state survives this scheduler checkpoint."""
    worker = tmp_path / "synthetic_worker.py"
    worker.write_text(
        textwrap.dedent(
            """
            import json
            import socket
            import sys
            from pathlib import Path

            import httpx
            from doonex import sdk
            from test_sdk import MODEL, harness, responses_answer
            from test_sdk_scheduling import resume_hooks, settings

            root, phase, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
            requested = False
            requests, commands, receipts = [], [], []

            def forbidden(*args, **kwargs):
                raise AssertionError("Synthetic recovery must never use the network")

            socket.socket.connect = forbidden
            sdk.GuardedLLM._transport_call = forbidden

            def transport(self, request, *args, **kwargs):
                requests.append(json.loads(request.content))
                if phase == "checkpoint":
                    result = responses_answer("doonex_shell", {"command": "first"})
                    result = result.model_dump(mode="json")
                    second = responses_answer(
                        "doonex_shell", {"command": "second"}, sequence=2
                    ).model_dump(mode="json")["output"][0]
                    result["output"].append(second)
                    result["output"].insert(0, {
                        "id": "rs_synthetic_original",
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "synthetic-encrypted",
                    })
                else:
                    result = responses_answer(
                        "finish", {"message": "done"}, sequence=3
                    ).model_dump(mode="json")
                return httpx.Response(200, request=request, json=result)

            httpx.Client.send = transport

            def after(reservation, usage):
                global requested
                if phase == "checkpoint" and boundary == "before_tool":
                    requested = True

            def shell(command):
                global requested
                commands.append(command)
                if phase == "checkpoint" and boundary == "after_first":
                    requested = True
                return {"output": "created", "exit_code": 0}

            arguments, *_ = harness(
                root / "session",
                model={**MODEL, "api_mode": "responses",
                       "model_id": "gpt-6.1-sol",
                       "base_url": "https://responses-gateway.invalid/v1"},
                shell=shell,
                after_model=after,
                scheduling=settings(receipts, lambda: requested),
            )
            if phase == "resume":
                arguments.update(
                    prompt=None, resume=True,
                    scheduling=resume_hooks(json.loads((root / "receipt.json").read_text())),
                )
            result = sdk.run_session(**arguments)
            if phase == "checkpoint":
                (root / "receipt.json").write_text(json.dumps(receipts[0]))
            (root / (phase + ".json")).write_text(json.dumps({
                "result": result, "requests": requests, "commands": commands,
            }))
            """
        )
    )
    repo = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repo / "apps/server"), str(repo / "tests"))
    )
    for phase in ("checkpoint", "resume"):
        completed = subprocess.run(
            [sys.executable, str(worker), str(tmp_path), phase, boundary],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    resumed = json.loads((tmp_path / "resume.json").read_text())
    assert checkpoint["result"] == "yielded"
    assert resumed["result"] == "finished"
    assert checkpoint["commands"] + resumed["commands"] == ["first", "second"]
    assert len(checkpoint["requests"]) == len(resumed["requests"]) == 1
    body = resumed["requests"][0]
    calls = [item for item in body["input"] if item["type"] == "function_call"]
    outputs = [item for item in body["input"] if item["type"] == "function_call_output"]
    assert [item["id"] for item in calls] == ["fc_1", "fc_2"]
    assert [item["call_id"] for item in calls] == ["call_1", "call_2"]
    assert [item["call_id"] for item in outputs] == ["call_1", "call_2"]
    assert next(item for item in body["input"] if item["type"] == "reasoning") == {
        "id": "rs_synthetic_original",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "synthetic-encrypted",
    }
