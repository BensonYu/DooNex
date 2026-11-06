"""Explicitly verify one authorized background batch yields to one prepared Run.

The foreground Run must already be paused, never started, and contain no stop
request. This controlled harness wraps the real Ledger.reserve only while the
first background Run executes: after its first model reservation commits, it
queues the designated foreground Run before returning that reservation. It
does not replace model transport or responses. This deterministic arrival is
an acceptance injection point, not a product scheduling interface.

Both campaign flock and worker advisory lock remain held. Existing allowances
and attempts survive failures; this entry point never recovers or retries them.
An existing draft requires --enable-authorization; enabling happens only after
the full campaign allowance is saved while both execution locks are held.
Execution explicitly selects this batch and foreground via Worker.execute;
general Worker.run queue priority has separate integration tests. SIGTERM
requests controlled cleanup; SIGKILL or host loss requires manual reconciliation
before restarting a worker, and the persisted campaign attempt forbids replay.
"""

import argparse
import fcntl
import json
import logging
import os
import signal
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

from doonex.arena import TERMINAL_RUNS, Arena, EvaluationInput
from doonex.background import Background
from doonex.budget import Ledger
from doonex.config import Settings
from doonex.models import model_for_role, run_roles
from doonex.sandbox import Sandbox
from doonex.store import dispatch_lock, now, termination_unknown
from doonex.worker import WORKER_LOCK, Worker, get_workspace, run_directory

if __package__ in {None, ""}:
    from live_arena import (
        TERMINAL,
        Refused,
        count,
        load_campaign,
        same_evaluation,
        save_campaign,
        usage_summary,
    )
else:
    from scripts.live_arena import (
        TERMINAL,
        Refused,
        count,
        load_campaign,
        same_evaluation,
        save_campaign,
        usage_summary,
    )

BLOCKING_RUNS = {"queued", "running", "verifying", "pausing", "yielded", "interrupted"}
BLOCKING_BATCHES = {"queued", "running", "cancelling", "interrupted"}
EVALUATION_FIELDS = (
    "taskset_id",
    "skill_version_ids",
    "model_profile_id",
    "repeats",
    "run_budget",
    "batch_budget",
)


def check_campaign(campaign, authorization_id, foreground_id):
    for item in campaign["runs"]:
        if same_evaluation(item.get("authorization_id"), authorization_id):
            raise Refused("authorization_already_attempted")
        if any(
            same_evaluation(item.get(key), foreground_id)
            for key in ("foreground_run_id", "run_id")
        ):
            raise Refused("foreground_already_attempted")


def check_model(settings, model, confirmed):
    if model["model_id"] != confirmed:
        raise Refused("confirmed_model_mismatch")
    if model["billing_mode"] != "subscription":
        raise Refused("subscription_billing_required")
    if not settings.credential(model):
        raise Refused("credential_unavailable")


def prepared_foreground(settings, run, model):
    request = run["request"]
    if (
        run["state"] != "paused"
        or run.get("first_started_at") is not None
        or run.get("requested_action") is not None
        or run.get("phase") != "execution"
        or request.get("arena")
        or request.get("background")
        or run_roles(request) != ("executor",)
        or run.get("scheduling")
        or run.get("evidence")
        or run.get("resume_session")
        or run.get("session_generation", 0)
        or run.get("active_assignment", "executor") != "executor"
    ):
        raise Refused("pristine_paused_foreground_required")
    if model_for_role(request) != model:
        raise Refused("foreground_model_configuration_mismatch")
    usage = usage_summary(run)
    if any(usage.values()):
        raise Refused("pristine_paused_foreground_required")
    for assignment in run.get("assignments", {}).values():
        if (
            assignment["state"] != "pending"
            or assignment.get("started_at")
            or assignment.get("resume_session")
            or assignment.get("session_generation", 0)
            or any(usage_summary(assignment).values())
        ):
            raise Refused("pristine_paused_foreground_required")
    if run_directory(settings, run["id"]).exists():
        raise Refused("foreground_execution_directory_exists")
    budget = request["budget"]
    calls = count(budget["max_calls"], positive=True)
    tokens = count(budget["max_tokens"], positive=True)
    count(budget["max_seconds"], positive=True)
    if tokens < model["max_input_tokens"] + model["max_output_tokens"]:
        raise Refused("foreground_budget_cannot_reserve_model")
    return calls, tokens


def isolated(connection, foreground_id, allowed=()):
    if termination_unknown(connection):
        raise Refused("unknown_container_termination")
    rows = connection.execute("SELECT id,state FROM runs ORDER BY id").fetchall()
    permitted = {foreground_id, *allowed}
    if any(
        str(row["id"]) not in permitted and row["state"] in BLOCKING_RUNS
        for row in rows
    ):
        raise Refused("unrelated_active_run")


def pristine_action_log(connection, run_id):
    if connection.execute(
        "SELECT 1 FROM actions WHERE run_id=%s LIMIT 1", (run_id,)
    ).fetchone():
        raise Refused("foreground_already_dispatched")


def no_containers():
    # A single label query also catches task containers absent from this DB.
    if Sandbox()._docker("ps", "-aq", "--filter", "label=doonex.run_id").stdout.split():
        raise Refused("existing_task_container")


def check_scope(worker, arena, foreground_id, evaluation_id=None, run_ids=()):
    with worker.store.connect() as connection:
        dispatch_lock(connection)
        isolated(connection, foreground_id, run_ids)
    if any(
        batch["id"] != evaluation_id and batch["state"] in BLOCKING_BATCHES
        for batch in arena.list()
    ):
        raise Refused("unrelated_active_evaluation")
    no_containers()


@contextmanager
def arrival_after_reservation(
    worker, background_id, foreground, model, attempt, campaign_path, campaign
):
    original = Ledger.reserve

    def reserve(ledger, *args, **kwargs):
        reservation = original(ledger, *args, **kwargs)
        if ledger.run_id != background_id or attempt.get("arrival"):
            return reservation
        if ledger.store.database_url != worker.store.database_url:
            raise Refused("reservation_store_mismatch")
        with worker.store.locked(foreground["id"], dispatch=True) as (c, current):
            prepared_foreground(worker.settings, current, model)
            if current["request"] != foreground["request"]:
                raise Refused("foreground_changed")
            pristine_action_log(c, current["id"])
            row = c.execute(
                "SELECT kind,status FROM actions WHERE id=%s AND run_id=%s",
                (reservation["id"], background_id),
            ).fetchone()
            if not row or row != {"kind": "model", "status": "pending"}:
                raise Refused("first_model_reservation_not_pending")
            stamp = now()
            current.update(
                state="queued", reason="受控真实验收：后台首次模型预留后前台入队"
            )
            arrival = {
                "background_run_id": background_id,
                "background_action_id": reservation["id"],
                "foreground_enqueued_at": stamp,
            }
            worker.store.event(
                current["id"], "live_background_foreground_queued", arrival, c
            )
        attempt["arrival"] = arrival
        save_campaign(campaign_path, campaign)
        return reservation

    # The original method always performs real admission and accounting first.
    # No other worker can execute while this harness holds WORKER_LOCK.
    Ledger.reserve = reserve
    try:
        yield
    finally:
        Ledger.reserve = original


def complete_run(store, run_id):
    run = store.get(run_id)
    usage = usage_summary(run)
    return (
        run["state"] in TERMINAL_RUNS
        and not usage["unknown_calls"]
        and not usage["reserved_tokens"]
        and not store.pending(run_id)
    )


def timing(store, foreground, arrival):
    events = store.events(foreground["id"])["events"]
    running = next((item for item in events if item["kind"] == "running"), None)
    if running is None:
        return {}
    started = datetime.fromisoformat(running["created_at"])
    created = datetime.fromisoformat(foreground["created_at"])
    queued = datetime.fromisoformat(arrival["foreground_enqueued_at"])
    if not created <= queued <= started:
        raise Refused("invalid_foreground_timeline")
    return {
        "foreground_created_at": foreground["created_at"],
        "foreground_enqueued_at": arrival["foreground_enqueued_at"],
        "foreground_running_at": running["created_at"],
        "foreground_created_to_running_seconds": (started - created).total_seconds(),
        "foreground_queue_to_running_seconds": (started - queued).total_seconds(),
    }


def stop_attempt(worker, arena, foreground_id, evaluation_id):
    result = {}
    if evaluation_id:
        try:
            batch = arena.get(evaluation_id)
            children = [worker.store.get(item["run_id"]) for item in batch["attempts"]]
            uncertain = any(
                usage_summary(run)["unknown_calls"]
                or usage_summary(run)["reserved_tokens"]
                or worker.store.pending(run["id"])
                or run.get("recovery", {}).get("remaining")
                or run.get("recovery", {}).get("errors")
                for run in children
            )
            try:
                no_containers()
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - unknown is not stopped
                uncertain = True
            arena.stop(
                evaluation_id,
                "interrupted" if uncertain else "cancelling",
                "受控真实验收停止；保留已用和未知动作，禁止自动重试",
            )
            result["batch_stop_requested"] = (
                "interrupted" if uncertain else "cancelling"
            )
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - never erase the original reservation
            result["batch_cleanup"] = "unknown"
    try:
        with worker.store.locked(foreground_id, dispatch=True) as (c, current):
            if (
                current["state"] in {"queued", "paused"}
                and current.get("first_started_at") is None
            ):
                pristine_action_log(c, foreground_id)
                current.update(
                    state="cancelled",
                    ended_at=now(),
                    reason="受控真实验收已停止，取消尚未执行的前台样例",
                )
                worker.store.event(
                    foreground_id, "live_background_fixture_cancelled", {}, c
                )
                result["unstarted_foreground_cancelled"] = True
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - pending actions must not become cancelled
        result["foreground_cleanup"] = "unknown"
    return result


def execute_reserved(worker, arena, background, path, campaign, attempt, foreground):
    full_calls, full_tokens = campaign["reserved_calls"], campaign["reserved_tokens"]
    identifier = attempt.get("evaluation_id")
    summary = {
        key: attempt[key]
        for key in (
            "attempt_id",
            "authorization_id",
            "foreground_run_id",
            "model",
            "allocated_calls",
            "allocated_tokens",
        )
    }
    try:
        # Creation joins claim, child enqueue and attach in one DB transaction.
        # Its full campaign allowance was already fsynced before this point.
        grant = background.get(attempt["authorization_id"])
        if attempt.get("enable_authorization"):
            grant = background.enable(grant["id"], attempt["authorization_revision"])
            attempt["authorization_enabled_at"] = grant["enabled_at"]
            summary["authorization_enabled_at"] = grant["enabled_at"]
            save_campaign(path, campaign)
        prepared = background.prepare(grant["id"])
        body = EvaluationInput(
            **{key: grant["input"][key] for key in EVALUATION_FIELDS}
        )
        arena_prepared = arena.prepare(body)
        with worker.store.locked(foreground["id"], dispatch=True) as (c, current):
            isolated(c, foreground["id"])
            prepared_foreground(worker.settings, current, grant["snapshot"]["model"])
            if current["request"] != foreground["request"]:
                raise Refused("foreground_changed")
            pristine_action_log(c, current["id"])
            claim = background.claim(c, grant["id"], prepared=prepared)
            binding = {
                key: claim[key]
                for key in ("authorization_id", "date", "reservation_id")
            }
            evaluation = arena.create(
                body, connection=c, background=binding, prepared=arena_prepared
            )
            background.attach(c, claim, evaluation)
        identifier = evaluation["id"]
        attempt["evaluation_id"] = summary["evaluation_id"] = identifier
        run_ids = [str(UUID(item["run_id"])) for item in evaluation["attempts"]]
        if not run_ids or len(run_ids) != len(set(run_ids)):
            raise Refused("invalid_record")
        save_campaign(path, campaign)
        check_scope(worker, arena, foreground["id"], identifier, run_ids)
        first = run_ids[0]
        with arrival_after_reservation(
            worker,
            first,
            foreground,
            grant["snapshot"]["model"],
            attempt,
            path,
            campaign,
        ):
            worker.execute(first)
        if not attempt.get("arrival") or worker.store.get(first)["state"] != "yielded":
            raise Refused("background_did_not_yield")
        with worker.store.connect() as c:
            actions = c.execute(
                "SELECT id,kind,status FROM actions WHERE run_id=ANY(%s::uuid[])",
                (run_ids,),
            ).fetchall()
        if len(actions) != 1 or actions[0] != {
            "id": UUID(attempt["arrival"]["background_action_id"]),
            "kind": "model",
            "status": "completed",
        }:
            raise Refused("extra_or_unsettled_background_dispatch_before_foreground")
        summary["yielded_run_id"] = first
        summary["arrival"] = attempt["arrival"]
        check_scope(worker, arena, foreground["id"], identifier, run_ids)
        worker.execute(foreground["id"])
        foreground_final = worker.store.get(foreground["id"])
        summary["foreground_state"] = foreground_final["state"]
        summary.update(timing(worker.store, foreground, attempt["arrival"]))
        if foreground_final["state"] != "completed_verified" or not complete_run(
            worker.store, foreground["id"]
        ):
            raise Refused("foreground_not_verified_or_usage_unknown")
        for run_id in run_ids:
            check_scope(worker, arena, foreground["id"], identifier, run_ids)
            with worker.store.connect() as c:
                dispatch_lock(c)
                background.preflight(c, grant["id"], identifier)
            run = worker.store.get(run_id)
            if run["state"] == "yielded":
                current_usage = usage_summary(run)
                if (
                    current_usage["unknown_calls"]
                    or current_usage["reserved_tokens"]
                    or worker.store.pending(run_id)
                ):
                    raise Refused("yielded_background_usage_unknown")
                workspace = get_workspace(run, run_directory(worker.settings, run_id))
                if not worker.scheduler.resume_yielded(run, workspace):
                    raise Refused("background_resume_refused")
                arena.sync(identifier)
                run = worker.store.get(run_id)
            if run["state"] == "queued":
                worker.execute(run_id)
            if not complete_run(worker.store, run_id):
                raise Refused("background_nonterminal_or_usage_unknown")
        final = arena.detail(identifier)
        foreground_final = worker.store.get(foreground["id"])
        batch_usage = usage_summary(final)
        foreground_usage = usage_summary(foreground_final)
        known = (
            final["state"] in TERMINAL
            and not batch_usage["unknown_calls"]
            and not batch_usage["reserved_tokens"]
            and all(complete_run(worker.store, item) for item in run_ids)
            and complete_run(worker.store, foreground["id"])
        )
        if not known:
            raise Refused("final_usage_not_fully_settled")
        if known:
            with worker.store.connect() as c:
                row = c.execute(
                    "SELECT data FROM arena_evaluations WHERE id=%s FOR UPDATE",
                    (identifier,),
                ).fetchone()
                allocation = background.settle(c, identifier, row["data"])
            if not allocation.get("settled_at"):
                raise Refused("background_day_allowance_retained")
            campaign["reserved_calls"] += (
                batch_usage["calls"]
                + foreground_usage["calls"]
                - attempt["allocated_calls"]
            )
            campaign["reserved_tokens"] += (
                batch_usage["tokens"]
                + foreground_usage["tokens"]
                - attempt["allocated_tokens"]
            )
        summary.update(
            state=final["state"],
            metrics_complete=final["metrics"].get("complete") is True,
            background_usage=batch_usage,
            foreground_usage=foreground_usage,
            status="settled" if known else "nonterminal_allowance_retained",
        )
        attempt.update(summary)
        save_campaign(path, campaign)
        return summary, (
            known
            and final["state"] == "complete"
            and summary["metrics_complete"]
            and final["integrity"]["valid"] is True
        )
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - never print private provider errors
        campaign["reserved_calls"], campaign["reserved_tokens"] = (
            full_calls,
            full_tokens,
        )
        summary.update(
            state="unknown",
            metrics_complete=False,
            status="interrupted_allowance_retained"
            if isinstance(error, KeyboardInterrupt)
            else "exception_allowance_retained",
            reason=str(error) if isinstance(error, Refused) else "execution_failed",
        )
        # A signal can arrive after a database commit but before its returned
        # value is assigned. Re-read durable effects instead of local flags.
        try:
            current = background.get(attempt["authorization_id"])
            if current.get("evaluation_id"):
                recovered_id = str(UUID(current["evaluation_id"]))
                if identifier is not None and identifier != recovered_id:
                    raise Refused("authorization_evaluation_changed")
                identifier = recovered_id
            elif current["state"] == "enabled":
                background.disable(current["id"], current["revision"])
                summary["unconsumed_authorization_disabled"] = True
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - preserve allowance and safe output
            summary["authorization_cleanup"] = "unknown"
        if identifier:
            summary["evaluation_id"] = identifier
        summary.update(stop_attempt(worker, arena, foreground["id"], identifier))
        attempt.update(summary)
        try:
            save_campaign(path, campaign)
        except OSError:
            summary["campaign_record_updated"] = False
        return summary, False


def run(args):
    if not args.confirm_model_usage:
        raise Refused("model_usage_confirmation_required")
    settings = Settings.load()
    with (settings.home / "live-acceptance.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("campaign_locked") from None
        path = settings.home / "live-acceptance.json"
        campaign = load_campaign(path, args.authorization_id)
        check_campaign(campaign, args.authorization_id, args.foreground_run_id)
        worker = Worker(settings)
        worker.store.initialize()
        with worker.store.connect() as connection:
            connection.autocommit = True
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
            ).fetchone()["acquired"]
            if not acquired:
                raise Refused("worker_locked")
            worker.lock_connection = connection
            arena = Arena(settings, worker.store)
            background = Background(settings, worker.store)
            grant = background.get(args.authorization_id)
            expected_state = "draft" if args.enable_authorization else "enabled"
            if grant["state"] != expected_state or grant.get("evaluation_id"):
                raise Refused("unconsumed_enabled_authorization_required")
            background.prepare(grant["id"])
            model = grant["snapshot"]["model"]
            check_model(settings, model, args.model)
            foreground = worker.store.get(args.foreground_run_id)
            calls, tokens = prepared_foreground(settings, foreground, model)
            with worker.store.connect() as c:
                dispatch_lock(c)
                isolated(c, foreground["id"])
                pristine_action_log(c, foreground["id"])
            check_scope(worker, arena, foreground["id"])
            budget = grant["input"]["batch_budget"]
            count(budget["max_seconds"], positive=True)
            attempt = {
                "attempt_id": str(uuid4()),
                "authorization_id": grant["id"],
                "foreground_run_id": foreground["id"],
                "enable_authorization": args.enable_authorization,
                "authorization_revision": grant["revision"],
                "model": args.model,
                "status": "reserved",
                "allocated_calls": calls + count(budget["max_calls"], positive=True),
                "allocated_tokens": tokens + count(budget["max_tokens"], positive=True),
            }
            campaign["reserved_calls"] += attempt["allocated_calls"]
            campaign["reserved_tokens"] += attempt["allocated_tokens"]
            campaign["runs"].append(attempt)
            save_campaign(path, campaign)
            return execute_reserved(
                worker, arena, background, path, campaign, attempt, foreground
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-model-usage", action="store_true", required=True)
    parser.add_argument("--authorization-id", type=UUID, required=True)
    parser.add_argument("--foreground-run-id", type=UUID, required=True)
    parser.add_argument(
        "--enable-authorization",
        action="store_true",
        help="Explicitly enable an existing draft after reserving its full allowance",
    )
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    args.authorization_id, args.foreground_run_id = (
        str(args.authorization_id),
        str(args.foreground_run_id),
    )
    os.umask(0o077)
    logging.getLogger().setLevel(logging.ERROR)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise KeyboardInterrupt("Controlled acceptance termination")

    signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            summary, success = run(args)
        except Refused as error:
            summary = {"status": "refused", "reason": str(error)}
            success = False
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - configuration/DB errors can contain secrets
            summary = {"status": "refused", "reason": "preparation_failed"}
            success = False
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    summary.update(
        authorization_id=args.authorization_id, foreground_run_id=args.foreground_run_id
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
