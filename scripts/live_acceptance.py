"""Explicit live opt-in against a generated sample, with one cumulative ledger.

Reserve the campaign before enqueueing. Hold the worker lock and execute only
this attempt. Crashes and nonterminal runs retain their full campaign allowance.
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from uuid import uuid4

from doonex.api import create_app
from doonex.config import Settings
from doonex.store import Store
from doonex.worker import WORKER_LOCK, Worker
from doonex.workspace import _git
from fastapi.testclient import TestClient

TERMINAL = {
    "completed_verified",
    "completed_unverified",
    "failed",
    "cancelled",
    "budget_exceeded",
}


def git(path, *args):
    return _git(path, *args).stdout.decode().strip()


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


def source_fingerprint(repo):
    digest = hashlib.sha256()
    for arguments in (
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "-z"),
        ("diff", "--binary", "--no-ext-diff", "--no-textconv"),
        ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"),
    ):
        digest.update(_git(repo, *arguments).stdout)
    # These two dirty fixture files are deliberately not passed into the task.
    for name in ("README.md", "untracked.txt"):
        digest.update((repo / name).read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-model-usage", action="store_true", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=50_000)
    parser.add_argument(
        "--workflow", choices=["execute", "execute_review"], default="execute"
    )
    parser.add_argument("--sample-name", default="sample-project")
    parser.add_argument(
        "--authorize-subscription-campaign",
        action="store_true",
        help="Record explicit permission for subscription testing without an aggregate cap; each run stays bounded",
    )
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 10 or not 35_000 <= args.max_tokens <= 100_000:
        raise SystemExit("Invalid bounded campaign allowance")
    if not re.fullmatch(
        r"(?:sample-project|m2-sample-[a-z0-9-]{1,40})", args.sample_name
    ):
        raise SystemExit("Use sample-project or a bounded m2-sample- name")
    os.umask(0o077)
    logging.getLogger().setLevel(logging.ERROR)
    settings = Settings.load()
    if not settings.model or not settings.credential():
        raise SystemExit("Configure an actual model and private credential first")
    model = settings.model.model_copy(update={"model_id": args.model})
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    settings.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.home / "live-acceptance.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        campaign_path = settings.home / "live-acceptance.json"
        campaign = (
            json.loads(campaign_path.read_text())
            if campaign_path.exists()
            else {"reserved_calls": 0, "reserved_tokens": 0, "runs": []}
        )
        if args.authorize_subscription_campaign:
            if model.billing_mode != "subscription":
                raise SystemExit(
                    "Uncapped campaign authorization requires confirmed subscription billing"
                )
            if campaign.get("authorization", {}).get("mode") != "subscription_uncapped":
                campaign.setdefault("authorization_history", []).append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "previous_max_calls": 10,
                        "previous_max_tokens": 100_000,
                        "recorded_calls": campaign["reserved_calls"],
                        "recorded_tokens": campaign["reserved_tokens"],
                        "authorization": "Explicit user permission for subscription testing without an aggregate cap",
                    }
                )
                campaign["authorization"] = {
                    "mode": "subscription_uncapped",
                    "max_calls": None,
                    "max_tokens": None,
                }
                save_campaign(campaign_path, campaign)
        uncapped = (
            campaign.get("authorization", {}).get("mode") == "subscription_uncapped"
        )
        if uncapped and model.billing_mode != "subscription":
            raise SystemExit(
                "Subscription campaign authorization cannot be reused for metered billing"
            )
        if not uncapped and (
            campaign["reserved_calls"] + args.max_calls > 10
            or campaign["reserved_tokens"] + args.max_tokens > 100_000
        ):
            raise SystemExit(
                "The authorized campaign allowance is exhausted; do not reset it"
            )
        worker = Worker(settings)
        worker.store.initialize()
        with worker.store.connect() as connection:
            connection.autocommit = True
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
            ).fetchone()["acquired"]
            if not acquired:
                raise SystemExit(
                    "An existing worker is active; stop it before this explicit acceptance run"
                )
            worker.lock_connection = connection
            repo = settings.home / args.sample_name
            if repo.is_symlink():
                raise SystemExit("The controlled sample cannot be a symbolic link")
            if not repo.exists():
                repo.mkdir()
                git(repo, "init", "-b", "main")
                git(repo, "config", "user.name", "DooNex acceptance fixture")
                git(repo, "config", "user.email", "fixture@example.invalid")
                (repo / "calculator.py").write_text(
                    "def add(a, b):\n    return a - b\n"
                )
                (repo / "README.md").write_text(
                    "Controlled DooNex acceptance fixture.\n"
                )
                git(repo, "add", "calculator.py", "README.md")
                git(repo, "commit", "-m", "Controlled acceptance baseline")
                (repo / "README.md").write_text(
                    "User working copy change: preserve me.\n"
                )
                git(repo, "add", "README.md")
                (repo / "untracked.txt").write_text(
                    "User untracked file: preserve me.\n"
                )
            original = source_fingerprint(repo)
            check = "python -c \"from calculator import add; assert add(2, 3) == 5; assert add(-2, 2) == 0; assert add(0, 0) == 0; print('DooNex acceptance passed')\""
            with TestClient(create_app(settings)) as client:
                client.headers["Authorization"] = "Bearer " + settings.token
                registered = client.post("/api/projects", json={"path": str(repo)})
                if registered.status_code != 200:
                    raise SystemExit("Could not register the controlled sample")
                project_id = registered.json()["id"]
                attempt = {
                    "attempt_id": str(uuid4()),
                    "model": args.model,
                    "workflow": args.workflow,
                    "status": "reserved",
                    "allocated_calls": args.max_calls,
                    "allocated_tokens": args.max_tokens,
                }
                campaign["reserved_calls"] += args.max_calls
                campaign["reserved_tokens"] += args.max_tokens
                campaign["runs"].append(attempt)
                save_campaign(campaign_path, campaign)
                response = client.post(
                    "/api/runs",
                    json={
                        "project_id": project_id,
                        "workflow": args.workflow,
                        "prompt": "Fix calculator.py so add(a, b) returns the sum. Inspect it with doonex_shell, make the smallest change, and finish. Do not modify acceptance criteria. This is a controlled coding acceptance task.",
                        "checks": [check],
                        "base_ref": "HEAD",
                        "budget": {
                            "max_calls": args.max_calls,
                            "max_tokens": args.max_tokens,
                            "max_cost_usd": None
                            if model.billing_mode == "subscription"
                            else 2,
                            "max_seconds": 300,
                        },
                    },
                )
                if response.status_code != 200:
                    attempt["status"] = "preflight_rejected"
                    save_campaign(campaign_path, campaign)
                    raise SystemExit(
                        "Run preflight rejected; conservative campaign reservation retained"
                    )
                run_id = response.json()["id"]
                attempt["run_id"] = run_id
                save_campaign(campaign_path, campaign)
            worker.execute(run_id)
            final = Store(settings.database_url).get(run_id)
            preserved = source_fingerprint(repo) == original
            summary = {
                "run_id": run_id,
                "model": args.model,
                "workflow": args.workflow,
                "state": final["state"],
                "reason": final["reason"],
                "usage": final["usage"],
                "original_workspace_preserved": preserved,
                "snapshot_digest": (final.get("evidence") or {}).get("snapshot_digest"),
                "review_status": (final.get("review") or {}).get("status"),
                "role_usage": {
                    role: item["usage"]
                    for role, item in final.get("assignments", {}).items()
                },
            }
            if final["state"] in TERMINAL:
                campaign["reserved_calls"] += final["usage"]["calls"] - args.max_calls
                campaign["reserved_tokens"] += (
                    final["usage"]["tokens"]
                    + final["usage"]["reserved_tokens"]
                    - args.max_tokens
                )
                attempt["status"] = "settled"
            else:
                attempt["status"] = "nonterminal_allowance_retained"
            attempt.update(summary)
            save_campaign(campaign_path, campaign)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if final["state"] != "completed_verified" or not preserved:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
