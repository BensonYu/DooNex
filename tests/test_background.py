"""Real PostgreSQL contracts; synthetic materials and no model transport."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from doonex.background import (
    BACKGROUND_SCHEMA,
    Background,
    BackgroundConflict,
    BackgroundInput,
    BackgroundStopped,
    BackgroundWaiting,
)
from doonex.config import Settings
from psycopg.types.json import Jsonb
from test_tasksets import taskset_source as source_fixture

taskset_source = source_fixture

pytestmark = pytest.mark.postgres


@pytest.fixture
def background(settings, store, taskset_source, tmp_path):
    with store.connect() as c:
        c.execute(BACKGROUND_SCHEMA)
    source, spec, _, _, _ = taskset_source
    spec["split"] = "tuning"
    (source / "taskset.json").write_text(json.dumps(spec))
    clock = [datetime(2031, 4, 5, 10, tzinfo=UTC)]
    background = Background(settings, store, clock=lambda: clock[0])
    taskset = background.library.import_taskset(source)
    candidates = []
    for index in range(2):
        candidate = tmp_path / f"candidate-{index}"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text(
            f"Check authorized code. Candidate {index}."
        )
        record = background.library.import_skill(candidate, {})
        background.library.review(record["version_id"], "approved", "Synthetic fixture")
        candidates.append(record["version_id"])
    body = {
        "taskset_id": taskset["id"],
        "skill_version_ids": candidates,
        "model_profile_id": "legacy",
        "repeats": 1,
        "window_start": "2031-04-05T10:00:00Z",
        "window_end": "2031-04-05T11:00:00Z",
        "run_budget": {
            "max_calls": 3,
            "max_tokens": 100000,
            "max_cost_usd": 1,
            "max_seconds": 60,
        },
        "batch_budget": {
            "max_calls": 20,
            "max_tokens": 200000,
            "max_cost_usd": 2,
            "max_seconds": 300,
        },
        "daily_budget": {
            "max_calls": 40,
            "max_tokens": 400000,
            "max_cost_usd": 4,
            "max_seconds": 600,
        },
    }
    return background, body, clock


def enabled(background, body):
    draft = background.create(body)
    return background.enable(draft["id"], draft["revision"])


def insert_batch(
    background, c, claim, request_data, *, state="queued", include_binding=True
):
    """A SQL-only Arena writer exercises the shared transaction composition."""
    identifier = str(uuid4())
    snapshot, body = claim["frozen_snapshot"], claim["evaluation_input"]
    binding = {
        key: claim[key] for key in ("authorization_id", "date", "reservation_id")
    }
    batch = {
        "id": identifier,
        "state": state,
        "split": "tuning",
        **deepcopy(body),
        "taskset_report_sha256": snapshot["taskset_report_sha256"],
        "model": snapshot["model"],
        "model_profile": snapshot["model_profile"],
        "environment": snapshot["environment"],
        "skills": {
            key: {k: v for k, v in skill.items() if k != "review_revision"}
            for key, skill in snapshot["skills"].items()
        },
        "usage": {
            "calls": 0,
            "tokens": 0,
            "reserved_tokens": 0,
            "unknown_calls": 0,
            "cost_usd": None
            if snapshot["model"]["billing_mode"] == "subscription"
            else 0.0,
            "reserved_cost_usd": None
            if snapshot["model"]["billing_mode"] == "subscription"
            else 0.0,
            "metered_cost_usd": 0.0,
            "reserved_metered_cost_usd": 0.0,
        },
    }
    if include_binding:
        batch["background"] = binding
    c.execute(
        "INSERT INTO arena_evaluations VALUES(%s,%s,%s)",
        (identifier, state, Jsonb(batch)),
    )
    request = deepcopy(request_data)
    request.update(arena={"evaluation_id": identifier}, background=binding)
    run = background.store.create_run(request, connection=c)
    batch["run_id"] = run["id"]
    c.execute(
        "UPDATE arena_evaluations SET data=%s WHERE id=%s", (Jsonb(batch), identifier)
    )
    return batch


def attached(background, body, request_data):
    grant = enabled(background, body)
    with background.store.connect() as c:
        claim = background.claim(c, grant["id"])
        batch = insert_batch(background, c, claim, request_data)
        batch = background.attach(c, claim, batch)
    return background.get(grant["id"]), batch


def finish(background, batch, *, state="complete", run_state="failed", **usage):
    batch = deepcopy(batch)
    batch["state"] = state
    batch["usage"].update(usage)
    with background.store.connect() as c:
        run = c.execute(
            "SELECT data FROM runs WHERE id=%s", (batch["run_id"],)
        ).fetchone()["data"]
        run.update(state=run_state, usage=batch["usage"])
        c.execute(
            "UPDATE runs SET state=%s,data=%s WHERE id=%s",
            (run_state, Jsonb(run), run["id"]),
        )
        c.execute(
            "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
            (state, Jsonb(batch), batch["id"]),
        )
    return batch


@pytest.mark.parametrize(
    "change",
    [
        {"window_start": "2031-04-05T10:00:00"},
        {"window_end": "2031-04-06T00:00:00Z"},
        {"window_end": "2031-04-05T10:00:00Z"},
        {"window_end": "2031-04-05T09:00:00Z"},
        {"daily_budget": {"max_calls": 0, "max_tokens": 1, "max_seconds": 1}},
        {"enabled": True},
    ],
)
def test_finite_input_refuses_ambiguous_or_unbounded_grants(background, change):
    _, body, _ = background
    with pytest.raises(ValueError):
        BackgroundInput.model_validate({**body, **change})


def test_default_draft_revision_enable_and_one_way_disable(background):
    background, body, clock = background
    draft = background.create(body)
    assert draft["state"] == "draft" and draft["evaluation_id"] is None
    assert draft["revision"] and draft["date"] == "2031-04-05"
    with background.store.connect() as c, pytest.raises(BackgroundConflict):
        background.claim(c, draft["id"])
    with pytest.raises(BackgroundConflict):
        background.enable(draft["id"], "0" * 64)
    grant = background.enable(draft["id"], draft["revision"])
    assert grant["revision"] != draft["revision"]
    with pytest.raises(BackgroundConflict):
        background.enable(grant["id"], grant["revision"])
    with pytest.raises(BackgroundConflict):
        background.disable(grant["id"], draft["revision"])
    stopped = background.disable(grant["id"], grant["revision"])
    assert stopped["state"] == "disabled"
    assert background.disable(stopped["id"], stopped["revision"]) == stopped
    with pytest.raises(BackgroundConflict):
        background.enable(stopped["id"], stopped["revision"])
    assert len(background.list()) == 1
    assert [event["event"] for event in stopped["history"]] == [
        "created",
        "enabled",
        "disabled",
    ]
    assert background.day(stopped["date"])["reserved"]["calls"] == 0
    clock[0] += timedelta(hours=2)
    with pytest.raises(BackgroundStopped):
        background.create(body)


def test_wait_before_start_and_refuse_enable_at_expiry(background):
    background, body, clock = background
    clock[0] -= timedelta(seconds=1)
    grant = enabled(background, body)
    with background.store.connect() as c, pytest.raises(BackgroundWaiting):
        background.claim(c, grant["id"])
    clock[0] = datetime(2031, 4, 5, 11, tzinfo=UTC)
    draft_body = {**body, "window_end": "2031-04-05T12:00:00Z"}
    draft = background.create(draft_body)
    clock[0] = datetime(2031, 4, 5, 12, tzinfo=UTC)
    with pytest.raises(BackgroundStopped):
        background.enable(draft["id"], draft["revision"])
    with background.store.connect() as c, pytest.raises(BackgroundStopped):
        background.claim(c, grant["id"])


def test_heldout_gate_never_opens_its_report(background, monkeypatch):
    background, body, _ = background
    with background.store.connect() as c:
        c.execute(
            "UPDATE library_versions SET data=jsonb_set(data,'{split}','\"heldout\"') WHERE kind='taskset' AND id=%s",
            (body["taskset_id"],),
        )
    monkeypatch.setattr(
        background.library, "taskset", lambda *_: pytest.fail("Opened heldout content")
    )
    with pytest.raises(BackgroundStopped, match="tuning"):
        background.create(body)
    assert background.list() == []
    with pytest.raises(KeyError):
        background.day("2031-04-05")


def test_daily_budget_first_value_frozen_across_grants_and_models(background):
    background, body, _ = background
    draft = background.create(body)
    another_settings = Settings(
        background.settings.home,
        background.settings.database_url,
        background.settings.token,
        background.settings.model.model_copy(
            update={"model_id": "synthetic-other-model"}
        ),
    )
    another = Background(another_settings, background.store, clock=background.clock)
    second = another.create(body)
    assert second["snapshot"]["model"] != draft["snapshot"]["model"]
    for key in ("max_calls", "max_tokens", "max_seconds", "max_cost_usd"):
        changed = deepcopy(body)
        changed["daily_budget"][key] += 1
        with pytest.raises(BackgroundConflict):
            another.create(changed)
    assert background.day(draft["date"])["budget"] == body["daily_budget"]
    assert len(background.list()) == 2


@pytest.mark.parametrize("fail_after", ["claim", "insert", "attach"])
def test_shared_transaction_rolls_back_entire_claim_batch_and_children(
    background, request_data, fail_after
):
    background, body, _ = background
    grant = enabled(background, body)
    with pytest.raises(RuntimeError, match="injected"), background.store.connect() as c:
        claim = background.claim(c, grant["id"])
        if fail_after != "claim":
            batch = insert_batch(background, c, claim, request_data)
            if fail_after == "attach":
                background.attach(c, claim, batch)
        raise RuntimeError("injected transaction failure")
    assert background.get(grant["id"])["state"] == "enabled"
    assert background.day(grant["date"])["reserved"]["calls"] == 0
    with background.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) AS count FROM background_allocations"
            ).fetchone()["count"]
            == 0
        )
        assert (
            c.execute("SELECT count(*) AS count FROM arena_evaluations").fetchone()[
                "count"
            ]
            == 0
        )
        assert c.execute("SELECT count(*) AS count FROM runs").fetchone()["count"] == 0
    with background.store.connect() as c:
        claim = background.claim(c, grant["id"])
        background.attach(c, claim, insert_batch(background, c, claim, request_data))
    assert background.get(grant["id"])["state"] == "consumed"


def test_one_grant_claim_race_only_commits_one_batch(background, request_data):
    background, body, _ = background
    grant = enabled(background, body)
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        try:
            with background.store.connect() as c:
                claim = background.claim(c, grant["id"])
                return background.attach(
                    c, claim, insert_batch(background, c, claim, request_data)
                )["id"]
        except BackgroundConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    restarted = Background(
        background.settings, background.store, clock=background.clock
    )
    with background.store.connect() as c, pytest.raises(BackgroundConflict):
        restarted.claim(c, grant["id"])
    assert (
        restarted.day(grant["date"])["reserved"]["calls"]
        == body["batch_budget"]["max_calls"]
    )


def test_shared_daily_capacity_race_cannot_double_spend(background, request_data):
    background, body, _ = background
    body["daily_budget"] = deepcopy(body["batch_budget"])
    grants = [enabled(background, body) for _ in range(2)]
    barrier = Barrier(2)

    def attempt(grant):
        barrier.wait(timeout=5)
        try:
            with background.store.connect() as c:
                claim = background.claim(c, grant["id"])
                return background.attach(
                    c, claim, insert_batch(background, c, claim, request_data)
                )["id"]
        except BackgroundStopped:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, grants))
    assert sum(result is not None for result in results) == 1
    assert background.day("2031-04-05")["reserved"]["tokens"] == 200000
    assert sorted(background.get(grant["id"])["state"] for grant in grants) == [
        "consumed",
        "enabled",
    ]


def test_daily_first_setting_race_freezes_one_value(background):
    background, body, _ = background
    alternatives = [deepcopy(body), deepcopy(body)]
    alternatives[1]["daily_budget"]["max_calls"] += 1
    barrier = Barrier(2)

    def attempt(spec):
        barrier.wait(timeout=5)
        try:
            return background.create(spec)
        except BackgroundConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, alternatives))
    winner = next(result for result in results if result)
    assert sum(result is not None for result in results) == 1
    assert background.day(winner["date"])["budget"] == winner["input"]["daily_budget"]


@pytest.mark.parametrize("mutation", ["split", "model", "skills", "background"])
def test_attach_refuses_batch_outside_frozen_grant(background, request_data, mutation):
    background, body, _ = background
    grant = enabled(background, body)
    with pytest.raises(BackgroundStopped), background.store.connect() as c:
        claim = background.claim(c, grant["id"])
        batch = insert_batch(background, c, claim, request_data)
        batch[mutation] = "tampered"
        c.execute(
            "UPDATE arena_evaluations SET data=%s WHERE id=%s",
            (Jsonb(batch), batch["id"]),
        )
        background.attach(c, claim, batch)
    assert background.get(grant["id"])["state"] == "enabled"
    assert background.day(grant["date"])["reserved"]["calls"] == 0


@pytest.mark.parametrize(
    "change", ["review", "reapprove", "profile", "environment", "expired", "revoked"]
)
def test_preflight_rechecks_each_dispatch_without_refunding(
    background, request_data, monkeypatch, change
):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    with background.store.connect() as c:
        assert background.preflight(c, grant["id"], batch["id"])["id"] == grant["id"]
    if change in {"review", "reapprove"}:
        background.library.review(
            body["skill_version_ids"][0],
            "rejected" if change == "review" else "approved",
            "Changed review",
        )
    elif change == "profile":
        background.settings = Settings(
            background.settings.home,
            background.settings.database_url,
            background.settings.token,
            background.settings.model.model_copy(update={"model_id": "changed"}),
        )
    elif change == "environment":
        monkeypatch.setattr("doonex.background.environment", lambda: {"changed": True})
    elif change == "expired":
        clock[0] += timedelta(hours=1)
    else:
        background.disable(grant["id"], grant["revision"])
    with pytest.raises(BackgroundStopped), background.store.connect() as c:
        background.preflight(c, grant["id"], batch["id"])
    assert background.day(grant["date"])["reserved"]["calls"] == 20


@pytest.mark.parametrize(
    "state", ["complete", "cancelled", "budget_exceeded", "failed"]
)
def test_terminal_failure_costs_settle_once_and_keep_original_allocation(
    background, request_data, state
):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    clock[0] += timedelta(seconds=20, microseconds=1)
    batch = finish(
        background,
        batch,
        state=state,
        calls=2,
        tokens=100,
        reserved_tokens=50,
        cost_usd=0.1,
        reserved_cost_usd=0.05,
        metered_cost_usd=0.1,
        reserved_metered_cost_usd=0.05,
    )
    with background.store.connect() as c:
        settled = background.settle(c, batch["id"], batch)
    assert settled["actual"]["tokens"] == 150
    assert settled["actual"]["seconds"] == 21
    assert settled["reserved"]["calls"] == 20
    ledger = background.day(grant["date"])
    assert ledger["used"]["calls"] == 2 and ledger["reserved"]["calls"] == 0
    assert ledger["used"]["cost_usd"] == pytest.approx(0.15)
    clock[0] += timedelta(hours=1)
    with background.store.connect() as c:
        assert background.settle(c, batch["id"]) == settled
    assert background.day(grant["date"])["used"] == ledger["used"]


@pytest.mark.parametrize(
    "unresolved",
    ["interrupted", "unknown_usage", "pending", "unknown_shell", "acknowledged"],
)
def test_unknown_interrupted_and_inflight_keep_whole_day_allocation(
    background, request_data, unresolved
):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    batch = finish(
        background,
        batch,
        state="interrupted" if unresolved == "interrupted" else "complete",
        calls=1,
        tokens=100,
        unknown_calls=int(unresolved == "unknown_usage"),
    )
    if unresolved in {"pending", "unknown_shell", "acknowledged"}:
        with background.store.connect() as c:
            action = background.store.action(batch["run_id"], "shell", {}, c)
            if unresolved != "pending":
                background.store.finish_action(
                    action,
                    "unknown" if unresolved == "unknown_shell" else "acknowledged",
                    {},
                    c,
                )
    clock[0] += timedelta(days=1)
    with background.store.connect() as c:
        allocation = background.settle(c, batch["id"], batch)
    assert allocation["actual"] is None and allocation["settled_at"] is None
    assert background.day(grant["date"])["reserved"]["calls"] == 20
    restarted = Background(
        background.settings, background.store, clock=background.clock
    )
    assert restarted.day(grant["date"])["reserved"]["tokens"] == 200000
    with pytest.raises(BackgroundStopped), background.store.connect() as c:
        restarted.preflight(c, grant["id"], batch["id"])


def test_late_receipts_settle_original_date_and_never_roll_old_batch_forward(
    background, request_data
):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    clock[0] += timedelta(days=1)
    with pytest.raises(BackgroundStopped), background.store.connect() as c:
        background.preflight(c, grant["id"], batch["id"])
    next_body = deepcopy(body)
    next_body.update(
        window_start="2031-04-06T10:00:00Z", window_end="2031-04-06T11:00:00Z"
    )
    next_grant = background.create(next_body)
    batch = finish(background, batch, calls=1, tokens=123)
    with background.store.connect() as c:
        background.settle(c, batch["id"], batch)
    assert background.day(grant["date"])["used"]["tokens"] == 123
    assert background.day(grant["date"])["used"]["seconds"] == 86400
    assert background.day(next_grant["date"])["used"]["calls"] == 0
    assert background.day(next_grant["date"])["reserved"]["calls"] == 0


def test_actual_overrun_is_preserved_and_blocks_further_dispatch(
    background, request_data
):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    second = enabled(background, body)
    clock[0] += timedelta(seconds=601)
    batch = finish(
        background, batch, calls=41, tokens=400001, cost_usd=4.1, metered_cost_usd=4.1
    )
    with background.store.connect() as c:
        result = background.settle(c, batch["id"], batch)
    assert result["actual"]["seconds"] == 601
    ledger = background.day(grant["date"])
    assert ledger["used"]["calls"] == 41 and ledger["used"]["tokens"] == 400001
    assert ledger["reserved"]["calls"] == 0
    with pytest.raises(BackgroundStopped), background.store.connect() as c:
        background.claim(c, second["id"])


def test_revocation_stops_new_dispatch_but_admitted_results_still_settle(
    background, request_data
):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    background.disable(grant["id"], grant["revision"])
    clock[0] += timedelta(seconds=12)
    batch = finish(background, batch, state="cancelled", calls=2, tokens=333)
    with background.store.connect() as c:
        assert background.settle(c, batch["id"], batch)["actual"]["calls"] == 2
    assert background.get(grant["id"])["state"] == "disabled"
    assert background.day(grant["date"])["used"]["tokens"] == 333


def test_subscription_unknown_money_and_shared_metered_cap(background, request_data):
    background, body, clock = background
    model = background.settings.model.model_copy(
        update={"billing_mode": "subscription"}
    )
    settings = Settings(
        background.settings.home,
        background.settings.database_url,
        background.settings.token,
        model,
    )
    subscription = Background(settings, background.store, clock=background.clock)
    grant, batch = attached(subscription, body, request_data)
    assert grant["input"]["run_budget"]["max_cost_usd"] is None
    assert grant["input"]["batch_budget"]["max_cost_usd"] is None
    assert subscription.day(grant["date"])["reserved"]["cost_usd"] is None
    clock[0] += timedelta(seconds=5)
    batch = finish(subscription, batch, calls=1, tokens=200)
    with subscription.store.connect() as c:
        subscription.settle(c, batch["id"], batch)
    assert subscription.day(grant["date"])["used"]["cost_usd"] is None
    assert subscription.day(grant["date"])["used"]["metered_cost_usd"] == 0
    metered, _ = attached(background, body, request_data)
    assert background.day(metered["date"])["used"]["cost_usd"] is None
    assert background.day(metered["date"])["reserved"]["metered_cost_usd"] == 2


def test_null_daily_money_never_admits_metered_model(background):
    background, body, _ = background
    body["daily_budget"]["max_cost_usd"] = None
    with pytest.raises(BackgroundStopped, match="费用"):
        background.create(body)
    model = background.settings.model.model_copy(
        update={"billing_mode": "subscription"}
    )
    subscription = Background(
        Settings(
            background.settings.home,
            background.settings.database_url,
            background.settings.token,
            model,
        ),
        background.store,
        clock=background.clock,
    )
    subscription.create(body)
    body["daily_budget"]["max_cost_usd"] = 4
    with pytest.raises(BackgroundConflict):
        background.create(body)


def test_concurrent_terminal_settlement_is_once_only(background, request_data):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    clock[0] += timedelta(seconds=3)
    batch = finish(background, batch, calls=2, tokens=500)
    barrier = Barrier(2)

    def settle():
        barrier.wait(timeout=5)
        with background.store.connect() as c:
            return background.settle(c, batch["id"], batch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: settle(), range(2)))
    assert results[0] == results[1]
    assert background.day(grant["date"])["used"]["calls"] == 2
    assert background.day(grant["date"])["reserved"]["calls"] == 0


def test_prepared_claim_uses_immediate_frozen_receipt_without_long_scan(
    background, request_data, monkeypatch
):
    background, body, _ = background
    grant = enabled(background, body)
    prepared = background.prepare(grant["id"])
    monkeypatch.setattr(
        background.library,
        "taskset",
        lambda *_: pytest.fail("Full scan under dispatch lock"),
    )
    monkeypatch.setattr(
        background.library,
        "skill",
        lambda *_: pytest.fail("Full scan under dispatch lock"),
    )
    with background.store.connect() as c:
        claim = background.claim(c, grant["id"], prepared=prepared)
        batch = background.attach(
            c, claim, insert_batch(background, c, claim, request_data)
        )
        assert (
            background.preflight(c, grant["id"], batch["id"])["evaluation_id"]
            == batch["id"]
        )
    assert background.day(grant["date"])["reserved"]["calls"] == 20


@pytest.mark.parametrize("change", ["age", "revision", "approval"])
def test_prepared_receipt_cannot_bypass_changes_or_be_reused_later(background, change):
    background, body, clock = background
    grant = enabled(background, body)
    prepared = background.prepare(grant["id"])
    if change == "age":
        clock[0] += timedelta(seconds=11)
    elif change == "revision":
        prepared["revision"] = "0" * 64
    else:
        background.library.review(
            body["skill_version_ids"][0], "approved", "Changed decision receipt"
        )
    with pytest.raises(BackgroundStopped), background.store.connect() as c:
        background.claim(c, grant["id"], prepared=prepared)
    assert background.day(grant["date"])["reserved"]["calls"] == 0


def test_settlement_keeps_full_allocation_on_child_accounting_discrepancy(
    background, request_data
):
    background, body, _ = background
    grant, batch = attached(background, body, request_data)
    batch = finish(background, batch, calls=3, tokens=200)
    batch["usage"]["calls"] = 2
    with (
        pytest.raises(BackgroundStopped, match="不一致"),
        background.store.connect() as c,
    ):
        background.settle(c, batch["id"], batch)
    assert background.day(grant["date"])["reserved"]["calls"] == 20


def test_no_settlement_when_clock_moves_behind_allocation(background, request_data):
    background, body, clock = background
    grant, batch = attached(background, body, request_data)
    batch = finish(background, batch)
    clock[0] -= timedelta(seconds=1)
    with (
        pytest.raises(BackgroundStopped, match="时钟"),
        background.store.connect() as c,
    ):
        background.settle(c, batch["id"], batch)
    assert background.day(grant["date"])["reserved"]["seconds"] == 300


def test_unapproved_candidate_never_creates_a_draft_or_day_budget(background):
    background, body, _ = background
    background.library.review(
        body["skill_version_ids"][0], "rejected", "Synthetic rejection"
    )
    with pytest.raises(BackgroundStopped, match="审核批准"):
        background.create(body)
    assert background.list() == []
    with pytest.raises(KeyError):
        background.day("2031-04-05")
