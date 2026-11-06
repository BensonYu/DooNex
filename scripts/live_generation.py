"""Explicitly start one frozen generation draft under the existing campaign.

No fixture creation, automatic retry, import, approval or Arena dispatch occurs.
An interrupted attempt retains its allowance and cannot be replayed by this
script. After killing the process, reconcile the recorded generation/Run before
restarting an ordinary worker; the two stores cannot commit atomically.
"""

import argparse
import fcntl
import json
import logging
import os
from uuid import UUID, uuid4

from doonex.config import Settings
from doonex.generation import Generation, StartInput
from doonex.worker import WORKER_LOCK, Worker, run_directory

try:
    from scripts.live_arena import (
        Refused,
        count,
        load_campaign,
        save_campaign,
        usage_summary,
    )
except ModuleNotFoundError:
    from live_arena import Refused, count, load_campaign, save_campaign, usage_summary


def preflight(settings, draft, args):
    if draft["run_id"] is not None or draft["state"] != "draft":
        raise Refused("unstarted_generation_required")
    if draft["revision"] != args.expected_revision:
        raise Refused("confirmed_revision_mismatch")
    model = draft["model"]
    if model["model_id"] != args.model:
        raise Refused("confirmed_model_mismatch")
    if model["billing_mode"] != "subscription":
        raise Refused("subscription_billing_required")
    if not settings.credential(model):
        raise Refused("credential_unavailable")
    budget = draft["budget"]
    count(budget["max_seconds"], positive=True)
    if budget["max_cost_usd"] is not None:
        raise Refused("invalid_subscription_budget")
    return count(budget["max_calls"], positive=True), count(
        budget["max_tokens"], positive=True
    )


def execute_reserved(worker, service, path, campaign, attempt, draft):
    full_calls, full_tokens = campaign["reserved_calls"], campaign["reserved_tokens"]
    try:
        started = service.start(
            draft["id"],
            StartInput(
                expected_revision=draft["revision"],
                confirm_model_usage=True,
                confirm_derivative_rights=True,
            ),
        )
        run_id = started["run_id"]
        attempt["run_id"] = run_id
        save_campaign(path, campaign)
        worker.execute(run_id)
        final = worker.store.get(run_id)
        usage = usage_summary(final)
        valid = False
        if final["state"] == "completed_unverified":
            service.validate_run(
                final, run_directory(worker.settings, run_id), require_workspace=True
            )
            binding, _report = service._final(
                final, run_directory(worker.settings, run_id)
            )
            valid = (
                binding == final.get("generation_candidate")
                and binding["valid"] is True
            )
        known = (
            final["state"]
            in {"completed_unverified", "failed", "cancelled", "budget_exceeded"}
            and not usage["unknown_calls"]
            and not usage["reserved_tokens"]
        )
        if known:
            campaign["reserved_calls"] += usage["calls"] - attempt["allocated_calls"]
            campaign["reserved_tokens"] += usage["tokens"] - attempt["allocated_tokens"]
        attempt.update(
            state=final["state"],
            usage=usage,
            structural_valid=valid,
            status="settled" if known else "unknown_allowance_retained",
        )
        save_campaign(path, campaign)
        return attempt.copy(), known and valid
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - keep private failures out of output
        campaign["reserved_calls"], campaign["reserved_tokens"] = (
            full_calls,
            full_tokens,
        )
        attempt.update(
            state="unknown",
            structural_valid=False,
            status="interrupted_allowance_retained"
            if isinstance(error, KeyboardInterrupt)
            else "exception_allowance_retained",
        )
        # A failed status write after start must not leave a queued attempt for
        # an ordinary worker to execute later without campaign reconciliation.
        try:
            run_id = service.get(draft["id"])["run_id"]
            if run_id:
                attempt["run_id"] = run_id
                with worker.store.locked(run_id) as (c, current):
                    if current["state"] == "queued":
                        current.update(
                            state="cancelled", reason="生成验收准备中断；原额度保留"
                        )
                        worker.store.event(
                            run_id,
                            "cancelled",
                            {"source": "live_generation_preparation"},
                            c,
                        )
        except Exception:  # noqa: BLE001
            attempt["requires_run_reconciliation"] = True
        try:
            save_campaign(path, campaign)
        except OSError:
            attempt["campaign_record_updated"] = False
        return attempt.copy(), False


def run(args):
    settings = Settings.load()
    with (settings.home / "live-acceptance.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("campaign_locked") from None
        path = settings.home / "live-acceptance.json"
        campaign = load_campaign(path, args.generation_id)
        if any(
            item.get("generation_id") == args.generation_id for item in campaign["runs"]
        ):
            raise Refused("generation_already_attempted")
        worker = Worker(settings)
        # The source must already have its schema installed. Do not migrate or
        # reconcile unrelated records as part of a single live model attempt.
        with worker.store.connect() as connection:
            connection.autocommit = True
            if not connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
            ).fetchone()["acquired"]:
                raise Refused("worker_locked")
            worker.lock_connection = connection
            service = Generation(settings, worker.store)
            draft = service.detail(args.generation_id)
            calls, tokens = preflight(settings, draft, args)
            attempt = {
                "attempt_id": str(uuid4()),
                "generation_id": args.generation_id,
                "revision": draft["revision"],
                "model": args.model,
                "status": "reserved",
                "allocated_calls": calls,
                "allocated_tokens": tokens,
                "max_seconds": draft["budget"]["max_seconds"],
            }
            campaign["reserved_calls"] += calls
            campaign["reserved_tokens"] += tokens
            campaign["runs"].append(attempt)
            save_campaign(path, campaign)
            return execute_reserved(worker, service, path, campaign, attempt, draft)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-model-usage", action="store_true", required=True)
    parser.add_argument(
        "--confirm-derivative-rights", action="store_true", required=True
    )
    parser.add_argument("--generation-id", type=UUID, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    args.generation_id = str(args.generation_id)
    os.umask(0o077)
    logging.getLogger().setLevel(logging.ERROR)
    try:
        summary, success = run(args)
    except Refused as error:
        summary, success = {"status": "refused", "reason": str(error)}, False
    except (Exception, KeyboardInterrupt):  # noqa: BLE001
        summary, success = {"status": "refused", "reason": "preparation_failed"}, False
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
