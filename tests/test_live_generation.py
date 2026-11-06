"""Live-generation opt-in and campaign failures on disposable PostgreSQL only."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from doonex.generation import Generation
from doonex.sdk import GuardedLLM
from doonex.worker import WORKER_LOCK
from test_generation import setup_generation as setup_generation  # noqa: PLC0414 - pytest fixture
from test_generation_worker import transport

from scripts import live_generation


@pytest.fixture
def live_fixture(setup_generation, monkeypatch):
    original, body, _files, _taskset, _project = setup_generation
    settings = replace(
        original.settings,
        model=original.settings.model.model_copy(
            update={
                "billing_mode": "subscription",
                "input_price_per_million": None,
                "output_price_per_million": None,
                "price_source": None,
            }
        ),
    )
    service = Generation(settings, original.store)
    draft = service.create(body)
    previous = {
        "reserved_calls": 23,
        "reserved_tokens": 12345,
        "authorization": {
            "mode": "subscription_uncapped",
            "max_calls": None,
            "max_tokens": None,
        },
        "authorization_history": [{"at": "earlier", "previous_max_calls": 10}],
        "runs": [{"attempt_id": "old-failed", "status": "failed", "unknown_calls": 1}],
    }
    path = settings.home / "live-acceptance.json"
    path.write_text(json.dumps(previous))
    path.chmod(0o600)
    monkeypatch.setattr(live_generation.Settings, "load", lambda: settings)
    args = SimpleNamespace(
        generation_id=draft["id"],
        expected_revision=draft["revision"],
        model=draft["model"]["model_id"],
    )
    calls = transport(monkeypatch)
    return service, draft, path, previous, args, calls


def test_generation_campaign_records_only_new_actual_usage(live_fixture):
    service, draft, path, previous, args, calls = live_fixture
    result, success = live_generation.run(args)
    assert success and result["state"] == "completed_unverified"
    assert result["structural_valid"] and len(calls) == 1
    current = json.loads(path.read_text())
    assert current["reserved_calls"] == previous["reserved_calls"] + 1
    assert current["reserved_tokens"] == previous["reserved_tokens"] + 120
    assert current["runs"][:-1] == previous["runs"]
    assert current["authorization"] == previous["authorization"]
    assert current["authorization_history"] == previous["authorization_history"]
    assert service.get(draft["id"])["preview"] is None
    assert service.get(draft["id"])["imported"] is None
    saved = path.read_bytes()
    with pytest.raises(live_generation.Refused, match="already_attempted"):
        live_generation.run(args)
    assert path.read_bytes() == saved and len(calls) == 1


@pytest.mark.parametrize(
    "change", ["model", "revision", "credentials", "authorization", "worker_lock"]
)
def test_generation_preflight_never_starts_or_changes_campaign(
    live_fixture, monkeypatch, change
):
    service, draft, path, previous, args, calls = live_fixture
    if change == "model":
        args.model = "unconfirmed"
    elif change == "revision":
        args.expected_revision = "0" * 64
    elif change == "credentials":
        monkeypatch.delenv("DOONEX_LLM_API_KEY")
    elif change == "authorization":
        denied = copy.deepcopy(previous)
        denied["authorization"]["mode"] = "bounded"
        path.write_text(json.dumps(denied))
    before = path.read_bytes()
    with service.store.connect() as c:
        if change == "worker_lock":
            c.execute("SELECT pg_advisory_lock(%s)", (WORKER_LOCK,))
        with pytest.raises(live_generation.Refused):
            live_generation.run(args)
    assert path.read_bytes() == before and calls == []
    assert service.get(draft["id"])["run_id"] is None


def test_unknown_generation_retains_full_allowance_and_blocks_replay(
    live_fixture, monkeypatch
):
    _service, draft, path, previous, args, calls = live_fixture

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(GuardedLLM, "_transport_call", interrupt)
    result, success = live_generation.run(args)
    assert not success and result["state"] == "interrupted"
    assert result["status"] == "unknown_allowance_retained"
    assert result["usage"]["unknown_calls"] == 1
    current = json.loads(path.read_text())
    assert (
        current["reserved_calls"]
        == previous["reserved_calls"] + draft["budget"]["max_calls"]
    )
    assert (
        current["reserved_tokens"]
        == previous["reserved_tokens"] + draft["budget"]["max_tokens"]
    )
    assert current["runs"][:-1] == previous["runs"]
    with pytest.raises(live_generation.Refused, match="already_attempted"):
        live_generation.run(args)
    assert calls == []


@pytest.mark.parametrize("write", [1, 2, 3])
def test_campaign_write_failure_never_leaves_an_untracked_queued_run(
    live_fixture, monkeypatch, write
):
    service, draft, path, previous, args, calls = live_fixture
    original = live_generation.save_campaign
    count = 0

    def fail_once(*args):
        nonlocal count
        count += 1
        if count == write:
            raise OSError("PRIVATE-FAILURE-DETAIL")
        return original(*args)

    monkeypatch.setattr(live_generation, "save_campaign", fail_once)
    if write == 1:
        with pytest.raises(OSError):
            live_generation.run(args)
        assert service.get(draft["id"])["run_id"] is None
        assert json.loads(path.read_text()) == previous and calls == []
        return
    result, success = live_generation.run(args)
    assert not success and result["status"] == "exception_allowance_retained"
    assert "PRIVATE" not in json.dumps(result)
    current = json.loads(path.read_text())
    assert current["runs"][:-1] == previous["runs"]
    assert (
        current["reserved_calls"]
        == previous["reserved_calls"] + draft["budget"]["max_calls"]
    )
    assert (
        current["reserved_tokens"]
        == previous["reserved_tokens"] + draft["budget"]["max_tokens"]
    )
    run = service.store.get(service.get(draft["id"])["run_id"])
    assert run["state"] == ("cancelled" if write == 2 else "completed_unverified")
    assert len(calls) == (0 if write == 2 else 1)
