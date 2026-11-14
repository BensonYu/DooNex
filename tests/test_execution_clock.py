"""Deterministic clock tests: no store, containers, provider or real sleeps."""

import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from doonex.execution_clock import (
    ClockCapacity,
    ClockDeadline,
    ClockExpired,
    ClockInvalid,
    ClockScope,
    ExecutionClock,
)

START = datetime(2026, 11, 14, 12, tzinfo=UTC)
ID = str(UUID(int=1))
OTHER = str(UUID(int=2))
BINDING = "a" * 64


class ManualTime:
    def __init__(self, *, wall=START, monotonic=100.0, sample_cost=0.0):
        self.now, self.mono, self.sample_cost = wall, monotonic, sample_cost
        self.calls = []

    def monotonic(self):
        self.calls.append("monotonic")
        return self.mono

    def wall(self):
        self.calls.append("wall")
        self.mono += self.sample_cost
        return self.now

    def advance(self, seconds, *, wall_seconds=None):
        self.mono += seconds
        self.now += timedelta(seconds=seconds if wall_seconds is None else wall_seconds)

    def clock(self, **kwargs):
        return ExecutionClock(monotonic=self.monotonic, wall=self.wall, **kwargs)


def duration(clock, kind="run", identifier=ID, **overrides):
    return clock.duration(
        kind,
        identifier,
        **{
            "started_at": START.isoformat(),
            "seconds": 60,
            "binding": BINDING,
            **overrides,
        },
    )


def window(clock, **overrides):
    return clock.window(
        ID,
        **{
            "date": "2026-11-14",
            "window_start": (START - timedelta(hours=1)).isoformat(),
            "window_end": (START + timedelta(minutes=1)).isoformat(),
            "expires_at": (START + timedelta(minutes=1)).isoformat(),
            "binding": BINDING,
            **overrides,
        },
    )


def allocation_elapsed(clock, **overrides):
    return clock.allocation_elapsed(
        ID,
        **{
            "started_at": START.isoformat(),
            "seconds": 60,
            "binding": BINDING,
            **overrides,
        },
    )


def test_sampling_brackets_wall_with_deadline_before_and_elapsed_after():
    timer = ManualTime(sample_cost=5)
    clock = timer.clock()
    assert timer.calls == ["monotonic", "wall", "monotonic"]
    timer.mono, timer.now, timer.sample_cost = 110, START + timedelta(seconds=10), 8
    bound = duration(clock, "allocation")
    assert bound.at == 160  # Neither the 5s nor the 8s read extends admission.
    timer.mono, timer.now, timer.sample_cost = 125, START + timedelta(seconds=5), 3
    assert allocation_elapsed(clock) == 20  # 10 + (128 - 118), not 10 + 18.
    timer.sample_cost = 0
    assert allocation_elapsed(clock) == 20
    assert timer.calls == ["monotonic", "wall", "monotonic"] * 4


@pytest.mark.parametrize("kind", ["run", "batch", "allocation"])
def test_anchor_bounds_first_observation_after_a_wall_rollback(kind):
    timer = ManualTime()
    clock = timer.clock()
    timer.advance(30, wall_seconds=10)
    assert duration(clock, kind).at == 160  # Fresh now + remaining would be 180.


@pytest.mark.parametrize("kind", ["run", "batch", "allocation"])
def test_wall_forward_then_rollback_never_restores_a_shortened_deadline(kind):
    timer = ManualTime()
    clock = timer.clock()
    old = duration(clock, kind)
    assert old.at == 160
    timer.advance(10, wall_seconds=40)
    tightened = duration(clock, kind)
    assert tightened.at == 130
    timer.advance(5, wall_seconds=-10)
    assert duration(clock, kind).at == 130
    assert clock.minimum(old).at == 130  # Old snapshots also honor the cache.
    timer.mono = 130
    with pytest.raises(ClockExpired) as error:
        clock.check(old)
    assert error.value.scope == ClockScope(kind, ID)


def test_expired_scope_stays_expired_after_wall_rollback():
    timer = ManualTime()
    clock = timer.clock()
    original = duration(clock)
    timer.advance(60)
    with pytest.raises(ClockExpired):
        clock.check(duration(clock))
    timer.advance(10, wall_seconds=-55)
    assert duration(clock).at == original.at
    with pytest.raises(ClockExpired):
        clock.check(duration(clock))


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding", "b" * 64),
        ("started_at", (START - timedelta(seconds=1)).isoformat()),
        ("seconds", 61),
    ],
)
def test_same_durable_id_cannot_be_rebound_even_after_expiry(field, value):
    timer = ManualTime()
    clock = timer.clock()
    original = duration(clock)
    timer.advance(70)
    with pytest.raises(ClockInvalid) as error:
        duration(clock, **{field: value})
    assert error.value.reason == "binding_changed"
    assert error.value.scope == original.scope
    with pytest.raises(ClockExpired):
        clock.check(original)


def test_roles_children_yield_and_recreated_helpers_share_the_batch_scope():
    timer = ManualTime()
    clock = timer.clock()
    batch = duration(clock, "batch", seconds=100)
    allocation = duration(clock, "allocation", seconds=120)
    child_one = duration(clock, "run", identifier=OTHER, seconds=200)
    assert clock.minimum(child_one, batch, allocation).at == 200
    timer.advance(30)
    # A second role or helper receives this same clock, not a new epoch.
    helper = {"clock": clock, "role": "reviewer", "owner": "new owner"}
    child_two = duration(
        helper["clock"],
        "run",
        identifier=str(UUID(int=3)),
        started_at=timer.now.isoformat(),
        seconds=200,
    )
    assert child_two.at == 330
    timer.advance(10, wall_seconds=-5)  # Yield and resume preserve parent start.
    assert (
        clock.minimum(
            child_two,
            duration(clock, "batch", seconds=100),
            duration(clock, "allocation", seconds=120),
        )
        == batch
    )


@pytest.mark.parametrize("source", ["run", "batch", "allocation", "window", "day"])
def test_each_business_scope_can_be_the_earliest_attributable_bound(source):
    timer = ManualTime(wall=START.replace(hour=23, minute=59, second=40))
    clock = timer.clock()
    start = timer.now.isoformat()
    values = [duration(clock, seconds=300, started_at=start)]
    if source in {"run", "batch", "allocation"}:
        values.append(
            duration(clock, source, identifier=OTHER, seconds=10, started_at=start)
        )
    elif source == "window":
        end = (timer.now + timedelta(seconds=10)).isoformat()
        values.append(window(clock, window_end=end, expires_at=end))
    else:
        values.append(clock.day("2026-11-14", binding=BINDING))
    selected = clock.minimum(*values)
    assert selected.scope.kind == source
    assert selected.at == 120 if source == "day" else selected.at == 110
    timer.mono = selected.at
    with pytest.raises(ClockExpired) as error:
        clock.check(selected)
    assert error.value.scope == selected.scope


def test_window_forward_jump_and_rollback_and_mutable_revision_are_not_new_keys():
    timer = ManualTime()
    clock = timer.clock()
    bound = window(clock)
    timer.advance(10, wall_seconds=50)
    assert window(clock).at == 120
    timer.advance(5, wall_seconds=-30)
    assert window(clock).at == 120
    assert clock.minimum(bound).at == 120
    with pytest.raises(ClockInvalid, match="binding_changed"):
        window(clock, binding="b" * 64)


def test_day_uses_frozen_midnight_boundary_and_never_the_daily_seconds_quota():
    timer = ManualTime(wall=START.replace(hour=23, minute=59))
    clock = timer.clock()
    original = clock.day("2026-11-14", binding=BINDING)
    assert original.at == 160
    timer.advance(61)
    with pytest.raises(ClockExpired):
        clock.check(clock.day("2026-11-14", binding=BINDING))
    tomorrow = clock.day("2026-11-15", binding="b" * 64)
    assert tomorrow.scope.id == "2026-11-15"
    assert tomorrow.at > original.at
    with pytest.raises(ClockInvalid, match="binding_changed"):
        clock.day("2026-11-14", binding="b" * 64)


def test_outer_deadline_only_tightens_and_its_source_is_explicit():
    timer = ManualTime()
    clock = timer.clock()
    bound = duration(clock)
    assert clock.minimum(bound, deadline=500) == bound
    outer = clock.minimum(bound, deadline=110)
    assert outer == ClockDeadline(110, ClockScope("outer", "caller"))
    assert clock.minimum(bound, outer, deadline=120) == outer
    timer.advance(10)
    with pytest.raises(ClockExpired) as error:
        clock.check(outer)
    assert error.value.scope == outer.scope


def test_capacity_has_no_expired_entry_eviction_or_helper_reset():
    timer = ManualTime()
    clock = timer.clock(capacity=1)
    bound = duration(clock)
    timer.advance(70)
    for kind in ("batch", "allocation", "run"):
        with pytest.raises(ClockCapacity) as error:
            duration(clock, kind, identifier=OTHER)
        assert error.value.scope == ClockScope(kind, OTHER)
    with pytest.raises(ClockExpired):
        clock.check(duration(clock))
    assert clock.minimum(bound) == bound


def test_allocation_elapsed_retains_prior_observations_and_can_exceed_reservation():
    timer = ManualTime()
    clock = timer.clock()
    timer.advance(50)
    duration(clock, "allocation", seconds=10)
    assert allocation_elapsed(clock, seconds=10) == 50
    timer.advance(20, wall_seconds=-45)
    assert allocation_elapsed(clock, seconds=10) == 70
    timer.advance(10, wall_seconds=100)
    assert allocation_elapsed(clock, seconds=10) == 105
    timer.advance(5, wall_seconds=-106)  # Wall is now before original claim.
    assert allocation_elapsed(clock, seconds=10) == 110
    with pytest.raises(ClockInvalid, match="future_start"):
        duration(clock, "allocation", seconds=10)
    with pytest.raises(ClockInvalid, match="binding_changed"):
        allocation_elapsed(clock, seconds=10, binding="b" * 64)


def test_first_accounting_observation_after_expiry_is_not_a_new_allowance():
    timer = ManualTime()
    clock = timer.clock()
    timer.advance(80)
    assert allocation_elapsed(clock) == 80
    with pytest.raises(ClockExpired):
        clock.check(duration(clock, "allocation"))


def test_restart_uses_original_start_not_now_plus_full_duration():
    timer = ManualTime()
    first = timer.clock()
    assert duration(first).at == 160
    timer.advance(40)
    reconstructed = timer.clock()
    assert duration(reconstructed).at == 160
    # Arbitrary wall rollback between epochs is explicitly outside this core.
    timer.advance(1, wall_seconds=-20)
    assert duration(reconstructed).at == 160


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "2026-11-14",
        "2026-11-14T12:00:00",
        "2026-11-14 12:00:00+00:00",
        "2026-11-14T12:00:00+00:99",
        "2026-11-14T12:00:00.1234567Z",
        "2026-02-30T12:00:00Z",
        START,
    ],
)
def test_malformed_or_naive_durable_start_refuses_with_scope(value):
    clock = ManualTime().clock()
    with pytest.raises(ClockInvalid) as error:
        duration(clock, started_at=value)
    assert error.value.scope == ClockScope("run", ID)


def test_aware_offset_timestamp_is_accepted_but_original_binding_is_immutable():
    clock = ManualTime().clock()
    bound = duration(clock, started_at="2026-11-14T13:00:00+01:00")
    assert bound.at == 160
    with pytest.raises(ClockInvalid, match="binding_changed"):
        duration(clock, started_at="2026-11-14T12:00:00Z")


def test_future_start_is_refused_before_occupying_capacity():
    clock = ManualTime().clock(capacity=1)
    future = (START + timedelta(microseconds=1)).isoformat()
    with pytest.raises(ClockInvalid, match="future_start"):
        duration(clock, started_at=future)
    with pytest.raises(ClockInvalid, match="future_start"):
        allocation_elapsed(clock, started_at=future)
    assert duration(clock, identifier=OTHER).scope.id == OTHER


@pytest.mark.parametrize(
    "value", [True, False, "60", None, 0, -1, math.inf, -math.inf, math.nan, 10**1000]
)
def test_nonpositive_nonfinite_or_non_numeric_duration_is_refused(value):
    with pytest.raises(ClockInvalid):
        duration(ManualTime().clock(), seconds=value)


@pytest.mark.parametrize("value", [None, "", "a", "A" * 64, "g" * 64, ["a"]])
def test_binding_is_a_finite_immutable_sha256_digest(value):
    with pytest.raises(ClockInvalid, match="invalid_binding_digest"):
        duration(ManualTime().clock(), binding=value)


@pytest.mark.parametrize(
    "kind,identifier",
    [
        (None, ID),
        ([], ID),
        ("role", ID),
        ("run", None),
        ("run", "new-owner"),
        ("run", UUID(int=1).hex),
    ],
)
def test_invalid_kind_or_noncanonical_durable_id_refuses(kind, identifier):
    with pytest.raises(ClockInvalid):
        duration(ManualTime().clock(), kind, identifier)


@pytest.mark.parametrize(
    "change",
    [
        {"date": "2026-11-15"},
        {"date": "2026-W46-6"},
        {"window_start": (START + timedelta(minutes=2)).isoformat()},
        {"window_end": (START + timedelta(days=1)).isoformat()},
        {"expires_at": (START + timedelta(seconds=59)).isoformat()},
    ],
)
def test_window_dates_order_and_expiry_must_match(change):
    with pytest.raises(ClockInvalid):
        window(ManualTime().clock(), **change)


@pytest.mark.parametrize("value", [None, "2026-W46-6", "2026-02-30", "9999-12-31"])
def test_invalid_day_or_unrepresentable_next_midnight_refuses(value):
    with pytest.raises(ClockInvalid):
        ManualTime().clock().day(value, binding=BINDING)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, None])
def test_invalid_capacity_refuses(value):
    with pytest.raises(ClockInvalid, match="invalid_capacity"):
        ManualTime().clock(capacity=value)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, True, "100"])
def test_invalid_monotonic_refuses(value):
    with pytest.raises(ClockInvalid, match="invalid_monotonic"):
        ExecutionClock(monotonic=lambda: value, wall=lambda: START)


@pytest.mark.parametrize("wall", [lambda: START.replace(tzinfo=None), lambda: None])
def test_naive_or_invalid_wall_clock_refuses(wall):
    with pytest.raises(ClockInvalid, match="invalid_wall_clock"):
        ExecutionClock(monotonic=lambda: 100, wall=wall)


def test_monotonic_regression_refuses_and_does_not_reset_existing_deadline():
    timer = ManualTime()
    clock = timer.clock()
    bound = duration(clock)
    timer.mono = 99
    with pytest.raises(ClockInvalid, match="monotonic_regression"):
        duration(clock)
    timer.mono = 101
    assert duration(clock) == bound
    decreasing = iter([100, 99])
    with pytest.raises(ClockInvalid, match="monotonic_regression"):
        ExecutionClock(monotonic=lambda: next(decreasing), wall=lambda: START)


@pytest.mark.parametrize("value", [True, math.inf, math.nan, "200"])
def test_invalid_outer_bound_refuses(value):
    with pytest.raises(ClockInvalid, match="invalid_deadline"):
        ManualTime().clock().minimum(deadline=value)


def test_unknown_or_malformed_bound_cannot_bypass_scope_registration():
    clock = ManualTime().clock()
    with pytest.raises(ClockInvalid, match="missing_deadline"):
        clock.minimum()
    with pytest.raises(ClockInvalid, match="unobserved_scope"):
        clock.check(ClockDeadline(1000, ClockScope("run", ID)))
    with pytest.raises(ClockInvalid, match="invalid_bound"):
        clock.check(1000)
