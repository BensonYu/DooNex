"""An old yielded snapshot cannot borrow changed execution authority."""

from copy import deepcopy

import pytest
from doonex.sandbox import Sandbox
from doonex.scheduling import ledger_checkpoint
from doonex.store import OwnershipLost
from test_yielded_deadline import admitted, taskset_source  # noqa: F401
from test_yielded_deadline import yielded as yielded_fixture

yielded = yielded_fixture

pytestmark = [pytest.mark.postgres, pytest.mark.docker]


@pytest.mark.parametrize("field", ["owner", "request"])
@pytest.mark.parametrize("phase", ["before_probe", "after_probe"])
def test_yielded_identity_change_cannot_resume(
    yielded,
    monkeypatch,
    field,
    phase,
):
    context = yielded
    original = context.store.get(context.run_id)
    changed = []
    probes = []

    def mutate():
        with context.store.locked(context.run_id) as (_, current):
            if field == "owner":
                current["owner"] = "replacement-owner"
            else:
                current["request"] = deepcopy(current["request"])
                current["request"]["prompt"] += " changed"
        changed.append(context.store.get(context.run_id))

    actual = Sandbox.containers

    def probe(self, *args, **kwargs):
        value = actual(self, *args, **kwargs)
        probes.append(value)
        if phase == "after_probe":
            mutate()
        return value

    monkeypatch.setattr(Sandbox, "containers", probe)
    if phase == "before_probe":
        mutate()
    events = context.store.events(context.run_id)
    with pytest.raises(OwnershipLost, match="归属或冻结请求"):
        context.scheduler.resume_yielded(original, context.workspace)
    assert context.scheduler.cleanup_blocked
    after = context.store.get(context.run_id)
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in changed[0].items() if k != "updated_at"
    }
    assert ledger_checkpoint(after) == ledger_checkpoint(original)
    assert context.store.events(context.run_id) == events
    assert len(probes) == (0 if phase == "before_probe" else 1)
