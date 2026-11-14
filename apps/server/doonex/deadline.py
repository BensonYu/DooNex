"""Cooperative checks for one absolute monotonic execution deadline.

These checks bound work between healthy local operations. They cannot interrupt
a blocked filesystem syscall or replace the persisted Run and batch guards.
"""

import math
import time


class ExecutionDeadlineExceeded(TimeoutError):
    """The original execution allowance has expired; do not start more work."""


def check_deadline(deadline: float | None) -> None:
    if deadline is None:
        return
    if type(deadline) not in (float, int) or not math.isfinite(deadline):
        raise ValueError("Execution deadline must be a finite monotonic timestamp")
    if time.monotonic() >= deadline:
        raise ExecutionDeadlineExceeded("运行的共同执行期限已到，停止新增工作")


def remaining_time(deadline: float) -> float:
    check_deadline(deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExecutionDeadlineExceeded("运行的共同执行期限已到，停止新增工作")
    return remaining
