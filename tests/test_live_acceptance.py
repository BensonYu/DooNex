"""The opt-in harness must not dispatch another run or erase unknown allowance."""

import json
import sys

import pytest
from doonex.config import Settings
from doonex.worker import WORKER_LOCK, Worker

from scripts import live_acceptance


def configure(monkeypatch, settings):
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_acceptance.py",
            "--confirm-model-usage",
            "--model",
            "offline-fixture",
            "--max-calls",
            "2",
            "--max-tokens",
            "35000",
        ],
    )


def test_existing_worker_rejects_before_queue_or_campaign_reservation(
    settings, store, monkeypatch
):
    configure(monkeypatch, settings)
    with store.connect() as connection:
        connection.autocommit = True
        connection.execute("SELECT pg_advisory_lock(%s)", (WORKER_LOCK,))
        with pytest.raises(SystemExit, match="existing worker"):
            live_acceptance.main()
    assert store.list_runs() == []
    assert store.list_projects() == []
    assert not (settings.home / "live-acceptance.json").exists()


def test_only_own_run_executes_and_nonterminal_allowance_is_retained(
    settings, store, request_data, monkeypatch
):
    configure(monkeypatch, settings)
    other = store.create_run(request_data)
    calls = []

    def pause(self, run_id):
        calls.append(run_id)
        self.store.transition(run_id, "paused", "controlled pause")

    monkeypatch.setattr(Worker, "execute", pause)
    with pytest.raises(SystemExit) as error:
        live_acceptance.main()
    assert error.value.code == 1
    assert len(calls) == 1 and calls[0] != other["id"]
    assert store.get(other["id"])["state"] == "queued"
    campaign = json.loads((settings.home / "live-acceptance.json").read_text())
    assert campaign["reserved_calls"] == 2
    assert campaign["reserved_tokens"] == 35000
    assert campaign["runs"][0]["status"] == "nonterminal_allowance_retained"


def test_crash_before_queue_response_keeps_persisted_reservation(
    settings, store, monkeypatch
):
    configure(monkeypatch, settings)
    original = live_acceptance.TestClient.post

    def crash(self, path, **kwargs):
        if path == "/api/runs":
            campaign = json.loads((settings.home / "live-acceptance.json").read_text())
            assert campaign["reserved_calls"] == 2
            assert campaign["reserved_tokens"] == 35000
            raise RuntimeError("controlled transport interruption")
        return original(self, path, **kwargs)

    monkeypatch.setattr(live_acceptance.TestClient, "post", crash)
    with pytest.raises(RuntimeError, match="controlled transport"):
        live_acceptance.main()
    assert store.list_runs() == []
    campaign = json.loads((settings.home / "live-acceptance.json").read_text())
    assert campaign["runs"][0]["status"] == "reserved"
    assert campaign["reserved_calls"] == 2


def test_terminal_settlement_preserves_previous_unknown_allowances(
    settings, monkeypatch
):
    configure(monkeypatch, settings)
    settings.home.mkdir(parents=True)
    original = {
        "reserved_calls": 5,
        "reserved_tokens": 15140,
        "runs": [{"run_id": "prior-record"}],
    }
    (settings.home / "live-acceptance.json").write_text(json.dumps(original))

    def finish(self, run_id):
        with self.store.locked(run_id) as (_, run):
            run["state"] = "failed"
            run["usage"].update(
                calls=1, tokens=120, reserved_tokens=228, unknown_calls=1
            )

    monkeypatch.setattr(Worker, "execute", finish)
    with pytest.raises(SystemExit):
        live_acceptance.main()
    campaign = json.loads((settings.home / "live-acceptance.json").read_text())
    assert campaign["reserved_calls"] == 6
    assert campaign["reserved_tokens"] == 15140 + 120 + 228
    assert campaign["runs"][0] == original["runs"][0]


def test_explicit_subscription_authorization_retains_history_and_run_bounds(
    settings, store, monkeypatch
):
    settings = Settings(
        settings.home,
        settings.database_url,
        settings.token,
        settings.model.model_copy(update={"billing_mode": "subscription"}),
    )
    configure(monkeypatch, settings)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--authorize-subscription-campaign"])
    settings.home.mkdir(parents=True)
    previous = {
        "reserved_calls": 10,
        "reserved_tokens": 20535,
        "runs": [{"run_id": "preserve-previous"}],
    }
    (settings.home / "live-acceptance.json").write_text(json.dumps(previous))

    def finish(self, run_id):
        run = self.store.get(run_id)
        assert run["request"]["budget"]["max_calls"] == 2
        assert run["request"]["budget"]["max_seconds"] == 300
        self.store.transition(run_id, "failed", "fixture")

    monkeypatch.setattr(Worker, "execute", finish)
    with pytest.raises(SystemExit):
        live_acceptance.main()
    campaign = json.loads((settings.home / "live-acceptance.json").read_text())
    assert campaign["reserved_calls"] == previous["reserved_calls"]
    assert campaign["reserved_tokens"] == previous["reserved_tokens"]
    assert campaign["runs"][0] == previous["runs"][0]
    assert campaign["authorization"]["mode"] == "subscription_uncapped"
    assert campaign["authorization_history"][0]["recorded_calls"] == 10


def test_metered_model_cannot_use_subscription_campaign_permission(
    settings, monkeypatch
):
    configure(monkeypatch, settings)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--authorize-subscription-campaign"])
    with pytest.raises(SystemExit, match="confirmed subscription"):
        live_acceptance.main()
    assert not (settings.home / "live-acceptance.json").exists()
