"""Explicitly execute one frozen Arena evaluation under the live campaign ledger.

This entry point never creates tasks, changes authorization, or recovers runs.
Both the campaign flock and worker advisory lock remain held through settlement.
"""

import argparse
import fcntl
import json
import logging
import os
import tempfile
from uuid import UUID, uuid4

from doonex.arena import Arena
from doonex.config import Settings
from doonex.worker import WORKER_LOCK, Worker

TERMINAL = {"complete", "cancelled", "budget_exceeded"}
STATES = TERMINAL | {"queued", "running", "interrupted", "cancelling"}


class Refused(Exception):
    """A fixed safe reason, never an exception from private configuration."""


def save_campaign(path, campaign):
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False
    ) as temporary:
        json.dump(campaign, temporary, indent=2)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = temporary.name
    os.replace(temp_path, path)


def count(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise Refused("invalid_record")
    return value


def same_evaluation(value, identifier):
    try:
        return str(UUID(str(value))) == identifier
    except (TypeError, ValueError):
        return False


def load_campaign(path, identifier):
    if not path.is_file():
        raise Refused("existing_campaign_required")
    campaign = json.loads(path.read_text())
    if campaign.get("authorization", {}).get("mode") != "subscription_uncapped":
        raise Refused("subscription_authorization_required")
    count(campaign["reserved_calls"])
    count(campaign["reserved_tokens"])
    if not isinstance(campaign["runs"], list) or not all(
        isinstance(item, dict) for item in campaign["runs"]
    ):
        raise Refused("invalid_record")
    if any(
        same_evaluation(item.get("evaluation_id"), identifier)
        for item in campaign["runs"]
    ):
        raise Refused("evaluation_already_attempted")
    return campaign


def preflight(settings, evaluation, model):
    if evaluation["state"] != "queued":
        raise Refused("queued_evaluation_required")
    frozen_model = evaluation["model"]
    if frozen_model["model_id"] != model:
        raise Refused("confirmed_model_mismatch")
    if frozen_model["billing_mode"] != "subscription":
        raise Refused("subscription_billing_required")
    if not settings.credential(frozen_model):
        raise Refused("credential_unavailable")
    budget = evaluation["batch_budget"]
    allocated_calls = count(budget["max_calls"], positive=True)
    allocated_tokens = count(budget["max_tokens"], positive=True)
    attempts = evaluation["attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise Refused("invalid_record")
    run_ids = [str(UUID(entry["run_id"])) for entry in attempts]
    if len(set(run_ids)) != len(run_ids):
        raise Refused("invalid_record")
    return allocated_calls, allocated_tokens, run_ids


def usage_summary(value):
    usage = value["usage"]
    return {
        key: count(usage[key])
        for key in ("calls", "tokens", "reserved_tokens", "unknown_calls")
    }


def execute_reserved(worker, arena, campaign_path, campaign, attempt, run_ids):
    # These totals already include the full persisted allocation. Every
    # exception path restores them, including failures during final reporting.
    full_calls = campaign["reserved_calls"]
    full_tokens = campaign["reserved_tokens"]
    summary = {
        "evaluation_id": attempt["evaluation_id"],
        "model": attempt["model"],
        "allocated_calls": attempt["allocated_calls"],
        "allocated_tokens": attempt["allocated_tokens"],
    }
    try:
        for run_id in run_ids:
            if arena.get(attempt["evaluation_id"])["state"] not in {
                "queued",
                "running",
            }:
                break
            if worker.store.get(run_id)["state"] == "queued":
                worker.execute(run_id)
        final = arena.detail(attempt["evaluation_id"])
        state = final["state"]
        if state not in STATES:
            raise Refused("invalid_record")
        usage = usage_summary(final)
        complete = final["metrics"].get("complete") is True
        if state in TERMINAL:
            campaign["reserved_calls"] += usage["calls"] - attempt["allocated_calls"]
            campaign["reserved_tokens"] += (
                usage["tokens"] + usage["reserved_tokens"] - attempt["allocated_tokens"]
            )
            status = "settled"
        else:
            status = "nonterminal_allowance_retained"
        summary.update(
            state=state, metrics_complete=complete, usage=usage, status=status
        )
        attempt.update(summary)
        save_campaign(campaign_path, campaign)
        return summary, state == "complete" and complete
    # Private provider, task and evidence errors must never escape to stderr.
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001
        campaign["reserved_calls"] = full_calls
        campaign["reserved_tokens"] = full_tokens
        summary = {
            key: summary[key]
            for key in (
                "evaluation_id",
                "model",
                "allocated_calls",
                "allocated_tokens",
            )
        }
        summary.update(
            state="unknown",
            metrics_complete=False,
            status="interrupted_allowance_retained"
            if isinstance(error, KeyboardInterrupt)
            else "exception_allowance_retained",
        )
        attempt.update(summary)
        try:
            save_campaign(campaign_path, campaign)
        except OSError:
            # The first reservation was persisted before any execution. A
            # failed status update must not trigger execution or a retry.
            summary["campaign_record_updated"] = False
        return summary, False


def run(args):
    settings = Settings.load()
    with (settings.home / "live-acceptance.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("campaign_locked") from None
        campaign_path = settings.home / "live-acceptance.json"
        campaign = load_campaign(campaign_path, args.evaluation_id)
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
            evaluation = arena.get(args.evaluation_id)
            calls, tokens, run_ids = preflight(settings, evaluation, args.model)
            attempt = {
                "attempt_id": str(uuid4()),
                "evaluation_id": args.evaluation_id,
                "model": args.model,
                "status": "reserved",
                "allocated_calls": calls,
                "allocated_tokens": tokens,
            }
            campaign["reserved_calls"] += calls
            campaign["reserved_tokens"] += tokens
            campaign["runs"].append(attempt)
            save_campaign(campaign_path, campaign)
            return execute_reserved(
                worker, arena, campaign_path, campaign, attempt, run_ids
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-model-usage", action="store_true", required=True)
    parser.add_argument("--evaluation-id", type=UUID, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    args.evaluation_id = str(args.evaluation_id)
    os.umask(0o077)
    logging.getLogger().setLevel(logging.ERROR)
    try:
        summary, success = run(args)
    except Refused as error:
        summary = {
            "evaluation_id": args.evaluation_id,
            "status": "refused",
            "reason": str(error),
        }
        success = False
    # Configuration and database errors can contain private connection details.
    except (Exception, KeyboardInterrupt):  # noqa: BLE001
        summary = {
            "evaluation_id": args.evaluation_id,
            "status": "refused",
            "reason": "preparation_failed",
        }
        success = False
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
