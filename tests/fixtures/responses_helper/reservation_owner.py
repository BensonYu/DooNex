"""Disposable-PG owner stopped at an actual reserve transaction boundary."""

import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from doonex.budget import Ledger
from doonex.config import Settings
from doonex.models import ModelConfig
from doonex.responses_binding import ResponsesBinding
from doonex.responses_registry import WORKER_LOCK
from doonex.store import Store

payload = json.loads(Path(sys.argv[2]).read_text())
receipt = Path(sys.argv[3])
store = Store(payload["database_url"])
settings = Settings(
    Path(payload["home"]),
    payload["database_url"],
    "offline-owner-token",
    ModelConfig.model_validate(payload["model"]),
)


def hold(action_id):
    receipt.write_text(json.dumps({"action_id": action_id, "phase": payload["phase"]}))
    receipt.chmod(0o600)
    while True:
        signal.pause()


with store.connect() as owner_connection:
    owner_connection.autocommit = True
    assert owner_connection.execute(
        "SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)
    ).fetchone()["acquired"]

    def check_owner():
        owner_connection.execute("SELECT 1")

    binding = ResponsesBinding(
        settings,
        store,
        run_id=payload["run_id"],
        owner=payload["owner"],
        check_owner=check_owner,
        check_dispatch=check_owner,
        model=payload["model"],
        api_key="offline-synthetic-owner-key",
    )
    intent = binding.prepare_intent(payload["request_id"])
    if payload["phase"] == "before_commit":
        original_event = store.event

        def before_commit(run_id, kind, body, connection=None):
            original_event(run_id, kind, body, connection)
            if kind == "model_reserved":
                hold(body["action_id"])

        store.event = before_commit
    reservation = Ledger(store, payload["run_id"], payload["owner"]).reserve(
        100, 50, helper_intent=intent
    )
    hold(reservation["id"])
