import math

import pytest
from doonex.deadline import (
    ExecutionDeadlineExceeded,
    check_deadline,
    remaining_time,
)


def test_remaining_time_consumes_the_original_deadline(monkeypatch):
    clock = iter((10, 10.5, 11, 12))
    monkeypatch.setattr("doonex.deadline.time.monotonic", lambda: next(clock))
    assert remaining_time(12) == 1.5
    with pytest.raises(ExecutionDeadlineExceeded):
        remaining_time(12)


@pytest.mark.parametrize("deadline", [True, "30", math.inf, -math.inf, math.nan])
def test_invalid_deadline_cannot_disable_checks(deadline):
    with pytest.raises(ValueError, match="finite monotonic"):
        check_deadline(deadline)


def test_optional_deadline_preserves_existing_callers(monkeypatch):
    def forbidden_clock():
        raise AssertionError("An optional deadline must not read the clock")

    monkeypatch.setattr("doonex.deadline.time.monotonic", forbidden_clock)
    check_deadline(None)
