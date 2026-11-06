"""Explicit opt-in live acceptance. Uses only a generated, controlled sample.

Private run evidence stays in DOONEX_HOME. Never run as an automatic unit test.
The campaign ledger reserves allowances before a run, including failed runs.
"""

import argparse
import fcntl
import json
import logging
import os
import subprocess

from doonex.api import create_app
from doonex.config import Settings
from doonex.store import Store
from doonex.worker import Worker
from fastapi.testclient import TestClient


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-model-usage", action="store_true", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=50_000)
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 10 or not 35_000 <= args.max_tokens <= 100_000:
        raise SystemExit("Invalid bounded campaign allowance")
    os.umask(0o077)
    logging.getLogger().setLevel(logging.ERROR)
    settings = Settings.load()
    if not settings.model or not settings.credential():
        raise SystemExit("Configure an actual model and private credential first")
    model = settings.model.model_copy(update={"model_id": args.model})
    settings = Settings(settings.home, settings.database_url, settings.token, model)
    lock = (settings.home / "live-acceptance.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    campaign_path = settings.home / "live-acceptance.json"
    campaign = (
        json.loads(campaign_path.read_text())
        if campaign_path.exists()
        else {"reserved_calls": 0, "reserved_tokens": 0, "runs": []}
    )
    if (
        campaign["reserved_calls"] + args.max_calls > 10
        or campaign["reserved_tokens"] + args.max_tokens > 100_000
    ):
        raise SystemExit(
            "The authorized live campaign allowance is exhausted; do not reset it to bypass the limit"
        )
    repo = settings.home / "sample-project"
    if not repo.exists():
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "DooNex acceptance fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "README.md").write_text("Controlled M1 acceptance fixture.\n")
        git(repo, "add", "calculator.py", "README.md")
        git(repo, "commit", "-m", "Controlled M1 baseline")
        (repo / "README.md").write_text("User working copy change: preserve me.\n")
        git(repo, "add", "README.md")
        (repo / "untracked.txt").write_text("User untracked file: preserve me.\n")
    original = git(repo, "status", "--porcelain=v1")
    check = "python -c \"from calculator import add; assert add(2, 3) == 5; assert add(-2, 2) == 0; assert add(0, 0) == 0; print('M1 acceptance passed')\""
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        registered = client.post("/api/project", json={"path": str(repo)})
        if registered.status_code != 200:
            raise SystemExit(
                "Could not register controlled sample; existing product project is preserved"
            )
        response = client.post(
            "/api/runs",
            json={
                "prompt": "Fix calculator.py so add(a, b) returns the sum. Inspect the file with doonex_shell, make the smallest change, and finish. Do not modify acceptance criteria. This is a controlled coding acceptance task.",
                "checks": [check],
                "base_ref": "HEAD",
                "budget": {
                    "max_calls": args.max_calls,
                    "max_tokens": args.max_tokens,
                    "max_cost_usd": None if model.billing_mode == "subscription" else 2,
                    "max_seconds": 300,
                },
            },
        )
        if response.status_code != 200:
            raise SystemExit(
                "Run preflight rejected: "
                + response.json().get("detail", "unknown error")
            )
        run_id = response.json()["id"]
        campaign["reserved_calls"] += args.max_calls
        campaign["reserved_tokens"] += args.max_tokens
        campaign["runs"].append(
            {"run_id": run_id, "model": args.model, "status": "reserved"}
        )
        campaign_path.write_text(json.dumps(campaign, indent=2))
    Worker(settings).run(once=True)
    final = Store(settings.database_url).get(run_id)
    preserved = git(repo, "status", "--porcelain=v1") == original
    summary = {
        "run_id": run_id,
        "model": args.model,
        "state": final["state"],
        "reason": final["reason"],
        "usage": final["usage"],
        "original_workspace_preserved": preserved,
        "snapshot_digest": (final.get("evidence") or {}).get("snapshot_digest"),
    }
    # Release unused allowances only after the run is terminal. Unknown in-flight
    # usage remains reserved in the persistent product ledger.
    campaign["reserved_calls"] -= args.max_calls - final["usage"]["calls"]
    campaign["reserved_tokens"] -= max(
        0,
        args.max_tokens - final["usage"]["tokens"] - final["usage"]["reserved_tokens"],
    )
    campaign["runs"][-1] = summary
    campaign_path.write_text(json.dumps(campaign, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if final["state"] != "completed_verified" or not preserved:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
