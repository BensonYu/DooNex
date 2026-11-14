"""Process-local upper bounds derived from original, durable UTC clocks.

One Worker owns this object for its entire ownership epoch and shares it with
its helpers. Observe all applicable scopes again at admission, combine their
snapshots, then call ``check``. This does not validate database state, grant
permission, resolve unknown work, or protect across arbitrary wall rollback
between process restarts. UTC day duration quotas remain ledger quantities.
"""

import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID


@dataclass(frozen=True)
class ClockScope:
    kind: str
    id: str


@dataclass(frozen=True)
class ClockDeadline:
    at: float
    scope: ClockScope


class ClockError(RuntimeError):
    def __init__(self, scope: ClockScope, reason: str):
        self.scope, self.reason = scope, reason
        super().__init__(f"Execution clock {scope.kind}/{scope.id}: {reason}")


class ClockInvalid(ClockError, ValueError):
    """The durable binding or clock observation cannot be trusted."""


class ClockCapacity(ClockError):
    """No scope can be evicted safely; refuse this new scope."""


class ClockExpired(ClockError, TimeoutError):
    """The earliest observed bound was reached; admit no new work."""


_EPOCH = ClockScope("clock", "epoch")
_OUTER = ClockScope("outer", "caller")
_STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


@dataclass
class _Entry:
    binding: tuple
    at: float
    elapsed: float
    observed_at: float


def _finite(value, scope, reason):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ClockInvalid(scope, reason)
    return float(value)


def _timestamp(value, scope):
    try:
        if type(value) is not str or not _STAMP.fullmatch(value):
            raise ValueError
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        raise ClockInvalid(scope, "invalid_utc_timestamp") from None


def _date(value, scope):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ClockInvalid(scope, "invalid_utc_date") from None


def _scope(kind, identifier):
    scope = ClockScope(
        kind if type(kind) is str else "invalid",
        identifier if type(identifier) is str else "invalid",
    )
    if type(kind) is not str or kind not in {
        "run",
        "batch",
        "allocation",
        "window",
        "day",
    }:
        raise ClockInvalid(scope, "invalid_scope_kind")
    if kind == "day":
        _date(identifier, scope)
    else:
        try:
            if type(identifier) is not str or str(UUID(identifier)) != identifier:
                raise ValueError
        except (ValueError, AttributeError):
            raise ClockInvalid(scope, "invalid_scope_id") from None
    return scope


def _binding(value, scope):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ClockInvalid(scope, "invalid_binding_digest")
    return value


class ExecutionClock:
    def __init__(self, *, monotonic=None, wall=None, capacity=4096):
        if type(capacity) is not int or capacity < 1:
            raise ClockInvalid(_EPOCH, "invalid_capacity")
        if (
            monotonic is not None
            and not callable(monotonic)
            or wall is not None
            and not callable(wall)
        ):
            raise ClockInvalid(_EPOCH, "invalid_clock_source")
        self._monotonic = monotonic or time.monotonic
        self._wall = wall or (lambda: datetime.now(UTC))
        self._capacity = capacity
        self._entries = {}
        self._last_mono = None
        self._anchor = self._sample(_EPOCH)

    def _mono(self, scope):
        value = _finite(self._monotonic(), scope, "invalid_monotonic")
        if self._last_mono is not None and value < self._last_mono:
            raise ClockInvalid(scope, "monotonic_regression")
        self._last_mono = value
        return value

    def _sample(self, scope):
        before = self._mono(scope)
        wall = self._wall()
        if not isinstance(wall, datetime) or wall.utcoffset() is None:
            raise ClockInvalid(scope, "invalid_wall_clock")
        try:
            wall = wall.astimezone(UTC)
        except (ValueError, OverflowError):
            raise ClockInvalid(scope, "invalid_wall_clock") from None
        after = self._mono(scope)
        return before, wall, after

    def _observe(self, scope, binding, end, sample, *, start=None):
        existing = self._entries.get(scope)
        if existing is not None and existing.binding != binding:
            raise ClockInvalid(scope, "binding_changed")
        if existing is None and len(self._entries) >= self._capacity:
            raise ClockCapacity(scope, "scope_capacity_exhausted")
        before, wall, after = sample
        candidate = min(
            before + max(0.0, (end - wall).total_seconds()),
            self._anchor[0] + (end - self._anchor[1]).total_seconds(),
        )
        candidate = _finite(candidate, scope, "invalid_deadline")
        # Sampling must not count wall-read time twice. A wall observation is
        # based at mono-after for accounting, but at mono-before for admission.
        elapsed = max(0.0, (wall - start).total_seconds()) if start else 0.0
        if existing is not None:
            elapsed = max(elapsed, existing.elapsed + (after - existing.observed_at))
        elapsed = _finite(elapsed, scope, "invalid_elapsed")
        if existing is None:
            existing = _Entry(binding, candidate, elapsed, after)
            self._entries[scope] = existing
        else:
            existing.at = min(existing.at, candidate)
            existing.elapsed = elapsed
            existing.observed_at = after
        return ClockDeadline(existing.at, scope)

    def _duration(self, kind, identifier, *, started_at, seconds, binding, accounting):
        scope = _scope(kind, identifier)
        if kind not in {"run", "batch", "allocation"}:
            raise ClockInvalid(scope, "invalid_duration_kind")
        start = _timestamp(started_at, scope)
        seconds = _finite(seconds, scope, "invalid_duration")
        if seconds <= 0:
            raise ClockInvalid(scope, "invalid_duration")
        frozen = (started_at, seconds, _binding(binding, scope))
        existing = self._entries.get(scope)
        if existing is not None and existing.binding != frozen:
            raise ClockInvalid(scope, "binding_changed")
        try:
            end = start + timedelta(seconds=seconds)
        except OverflowError:
            raise ClockInvalid(scope, "invalid_duration_end") from None
        sample = self._sample(scope)
        if start > sample[1] and not (accounting and existing is not None):
            raise ClockInvalid(scope, "future_start")
        return self._observe(scope, frozen, end, sample, start=start)

    def duration(self, kind, identifier, *, started_at, seconds, binding):
        """Observe an original start and frozen duration; never start a timer."""
        return self._duration(
            kind,
            identifier,
            started_at=started_at,
            seconds=seconds,
            binding=binding,
            accounting=False,
        )

    def window(
        self, identifier, *, date, window_start, window_end, expires_at, binding
    ):
        """Bind the original window; the caller still enforces its start gate."""
        scope = _scope("window", identifier)
        day = _date(date, scope)
        start, end, expiry = (
            _timestamp(value, scope) for value in (window_start, window_end, expires_at)
        )
        if start.date() != day or end.date() != day or start >= end or expiry != end:
            raise ClockInvalid(scope, "invalid_window")
        frozen = (date, window_start, window_end, expires_at, _binding(binding, scope))
        return self._observe(scope, frozen, end, self._sample(scope))

    def day(self, date, *, binding):
        """End at the frozen date's next UTC midnight, not its seconds quota."""
        scope = _scope("day", date)
        day = _date(date, scope)
        try:
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
        except OverflowError:
            raise ClockInvalid(scope, "invalid_day_end") from None
        return self._observe(
            scope, (_binding(binding, scope),), end, self._sample(scope)
        )

    def allocation_elapsed(self, identifier, *, started_at, seconds, binding):
        """Return the observed elapsed floor even after expiry or wall rollback.

        First observation still requires a valid nonfuture start. Settlement
        must take max(valid wall elapsed, this value), without a reservation cap.
        Unknown outcomes remain a separate reason to retain the reservation.
        """
        bound = self._duration(
            "allocation",
            identifier,
            started_at=started_at,
            seconds=seconds,
            binding=binding,
            accounting=True,
        )
        return self._entries[bound.scope].elapsed

    def _bound(self, bound):
        if not isinstance(bound, ClockDeadline) or not isinstance(
            bound.scope, ClockScope
        ):
            raise ClockInvalid(_EPOCH, "invalid_bound")
        at = _finite(bound.at, bound.scope, "invalid_deadline")
        if bound.scope != _OUTER:
            _scope(bound.scope.kind, bound.scope.id)
            entry = self._entries.get(bound.scope)
            if entry is None:
                raise ClockInvalid(bound.scope, "unobserved_scope")
            at = min(at, entry.at)
        return ClockDeadline(at, bound.scope)

    def minimum(self, *bounds, deadline=None):
        values = [self._bound(bound) for bound in bounds]
        if deadline is not None:
            values.append(
                ClockDeadline(_finite(deadline, _OUTER, "invalid_deadline"), _OUTER)
            )
        if not values:
            raise ClockInvalid(_EPOCH, "missing_deadline")
        return min(values, key=lambda value: value.at)

    def check(self, bound):
        bound = self._bound(bound)
        if self._mono(bound.scope) >= bound.at:
            raise ClockExpired(bound.scope, "deadline_expired")
