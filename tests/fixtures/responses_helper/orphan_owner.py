"""Finite test-only owner killed by the original pytest parent."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from doonex import responses_process as process

runtime, private, receipt, mode = map(Path, sys.argv[2:6])
process._runtime = lambda: process._Runtime(
    Path(sys.executable),
    runtime / "responses_helper_bootstrap.py",
    runtime / "responses_protocol.py",
    runtime / "responses_transport.py",
    runtime,
)
# The fixture protocol is loaded in the parent separately; no editable .pth.
import importlib.util

spec = importlib.util.spec_from_file_location(
    "fixture_protocol", runtime / "responses_protocol.py"
)
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)
process._protocol = lambda: protocol


def hook(state):
    def callback(data):
        receipt.write_text(json.dumps({"state": state, "data": data}))
        return {
            "schema": 1,
            "attempt_id": data["attempt_id"],
            "nonce": data["nonce"],
            "state": state,
            "record_sha256": {
                "prepared": "1",
                "armed": "2",
                "reaped": "3",
                "blocked": "4",
            }[state]
            * 64,
        }

    return callback


process.run_helper(
    process.HelperRequest(
        call={"mode": str(mode), "marker": str(private / "dispatched")},
        api_key="offline-synthetic-key",
        deadline_monotonic_ns=time.monotonic_ns() + 2_000_000_000,
        lease=process.LeaseSpec(private / "lease", allow_create=True),
        parent_boot_id="offline-test-boot",
    ),
    process.HelperHooks(
        hook("prepared"), hook("armed"), hook("reaped"), hook("blocked")
    ),
)
