"""Role models are frozen inputs, with one ledger across billing modes."""

import json

import pytest
from doonex.api import create_app
from doonex.budget import Halt, Ledger
from doonex.models import ModelConfig
from doonex.store import now
from fastapi.testclient import TestClient
from test_api import repository


def switch_to_review(store, run_id):
    with store.locked(run_id) as (_, run):
        run["active_assignment"] = "reviewer"
        run["phase"] = "review"
        run["assignments"]["executor"]["state"] = "completed"
        run["assignments"]["reviewer"]["state"] = "running"


@pytest.mark.parametrize("subscription_role", ["executor", "reviewer"])
def test_mixed_roles_retain_unknown_total_and_cap_priced_subtotal(
    store, request_data, subscription_role
):
    metered = {
        **request_data["model"],
        "model_id": "priced",
        "input_price_per_million": 100,
        "output_price_per_million": 200,
    }
    subscription = {
        **metered,
        "model_id": "monthly",
        "billing_mode": "subscription",
        "input_price_per_million": None,
        "output_price_per_million": None,
        "price_source": None,
    }
    request_data.update(
        workflow="execute_review",
        role_models={
            role: subscription if role == subscription_role else metered
            for role in ("executor", "reviewer")
        },
    )
    request_data["model"] = request_data["role_models"]["executor"]
    request_data["budget"].update(max_calls=5, max_cost_usd=0.04)
    run = store.create_run(request_data)
    with store.locked(run["id"]) as (_, current):
        current.update(state="running", first_started_at=now())
    for role in ("executor", "reviewer"):
        if role == "reviewer":
            switch_to_review(store, run["id"])
        ledger = Ledger(store, run["id"])
        reservation = ledger.reserve(100, 128)
        expected_id = "monthly" if role == subscription_role else "priced"
        assert reservation["model_id"] == expected_id
        ledger.settle(reservation, {"input_tokens": 100, "output_tokens": 128})
        if role != subscription_role:
            with pytest.raises(Halt, match="费用"):
                ledger.reserve(100, 128)
    final = store.get(run["id"])
    assert final["usage"]["cost_usd"] is None
    assert final["usage"]["reserved_cost_usd"] is None
    assert final["usage"]["metered_cost_usd"] == pytest.approx(0.0356)
    assert final["usage"]["reserved_metered_cost_usd"] == 0
    assert final["usage"]["calls"] == 2 and final["usage"]["tokens"] == 456
    assert final["assignments"][subscription_role]["usage"]["cost_usd"] is None
    assert sum(
        a["usage"]["metered_cost_usd"] for a in final["assignments"].values()
    ) == pytest.approx(final["usage"]["metered_cost_usd"])


def test_review_reservation_uses_its_model_limits_and_unknown_price_reserve(
    store, request_data
):
    request_data["workflow"] = "execute_review"
    reviewer = {
        **request_data["model"],
        "model_id": "small-reviewer",
        "max_output_tokens": 128,
    }
    request_data["role_models"] = {
        "executor": request_data["model"],
        "reviewer": reviewer,
    }
    run = store.create_run(request_data)
    with store.locked(run["id"]) as (_, current):
        current.update(state="running", first_started_at=now())
    switch_to_review(store, run["id"])
    ledger = Ledger(store, run["id"])
    with pytest.raises(Halt, match="单次上限"):
        ledger.reserve(100, 129)
    reservation = ledger.reserve(100, 128)
    ledger.settle(reservation, None)
    final = store.get(run["id"])
    assert final["usage"]["reserved_metered_cost_usd"] == reservation["cost"]
    assert final["usage"]["unknown_calls"] == 1


def test_profiles_are_frozen_at_run_creation_and_do_not_dispatch_metadata(
    settings, tmp_path, monkeypatch
):
    from doonex.model_registry import MetadataClient

    def no_network(*args):
        raise AssertionError("Creating runs must not query provider metadata")

    monkeypatch.setattr(MetadataClient, "discover", no_network)
    monkeypatch.setattr(MetadataClient, "usage", no_network)
    repo, _ = repository(tmp_path)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        client.post("/api/project", json={"path": str(repo)}).raise_for_status()
        body = {
            "name": "Fast",
            "model": {**settings.model.model_dump(), "model_id": "fast-original"},
            "usage_adapter": "none",
        }
        saved = client.put("/api/model-profiles/fast", json=body)
        assert saved.status_code == 200, saved.text
        profile = saved.json()
        response = client.post(
            "/api/runs",
            json={
                "prompt": "freeze settings",
                "workflow": "execute_review",
                "model_profile_id": "legacy",
                "reviewer_model_profile_id": "fast",
            },
        )
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["request"]["role_models"]["reviewer"]["model_id"] == "fast-original"
        assert (
            run["request"]["model_profiles"]["reviewer"]["revision"]
            == profile["revision"]
        )
        changed = {
            **body,
            "model": {**body["model"], "model_id": "fast-new"},
            "expected_revision": profile["revision"],
        }
        assert client.put("/api/model-profiles/fast", json=changed).status_code == 200
        old = client.get("/api/runs/" + run["id"]).json()
        assert old["request"] == run["request"]
        assert client.put("/api/model-profiles/fast", json=changed).status_code == 400
        assert client.put("/api/model-profiles/legacy", json=body).status_code == 400
        assert (
            client.post(
                "/api/runs",
                json={"prompt": "wrong workflow", "reviewer_model_profile_id": "fast"},
            ).status_code
            == 400
        )


@pytest.mark.docker
def test_worker_selects_frozen_role_model_and_credential(
    settings, store, tmp_path, monkeypatch
):
    from doonex.model_registry import ModelRegistry
    from doonex.sdk import GuardedLLM
    from doonex.worker import Worker
    from test_worker import response

    monkeypatch.setenv("DOONEX_REVIEW_KEY", "synthetic-review-key")
    registry = ModelRegistry(settings)
    executor = ModelConfig(
        **{**settings.model.model_dump(), "model_id": "gpt-4.1-mini"}
    )
    reviewer = ModelConfig(
        **{
            **settings.model.model_dump(),
            "model_id": "gpt-4.1",
            "credential_env": "DOONEX_REVIEW_KEY",
            "billing_mode": "subscription",
        }
    )
    registry.save("executor", "Execution", executor)
    registry.save("reviewer", "Review", reviewer)
    repo, _ = repository(tmp_path)
    calls = []
    responses = [
        response("doonex_shell", {"command": "printf 'answer = 42\\n' > hello.py"}, 1),
        response("finish", {"message": "Changed sample"}, 2),
        response(
            "finish",
            {
                "message": json.dumps(
                    {
                        "status": "approved",
                        "summary": "The exact fix is correct",
                        "findings": [],
                    }
                )
            },
            3,
        ),
    ]

    def transport(self, *args, **kwargs):
        calls.append((self.model, self.api_key.get_secret_value()))
        return responses.pop(0)

    monkeypatch.setattr(GuardedLLM, "_transport_call", transport)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        client.post("/api/project", json={"path": str(repo)}).raise_for_status()
        response_run = client.post(
            "/api/runs",
            json={
                "prompt": "Set answer to 42",
                "checks": ["python -c 'from hello import answer; assert answer == 42'"],
                "workflow": "execute_review",
                "model_profile_id": "executor",
                "reviewer_model_profile_id": "reviewer",
            },
        )
        assert response_run.status_code == 200, response_run.text
        run = response_run.json()
    Worker(settings).run(once=True)
    final = store.get(run["id"])
    assert final["state"] == "completed_verified", final["reason"]
    assert [m for m, _ in calls] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1",
    ]
    assert calls[-1][1] == "synthetic-review-key"
    assert all(key != "synthetic-review-key" for _, key in calls[:-1])
    assert final["usage"]["calls"] == 3 and final["usage"]["cost_usd"] is None
    assert final["usage"]["metered_cost_usd"] > 0
    assert final["assignments"]["reviewer"]["usage"]["cost_usd"] is None
