"""No provider or socket operations; exercise the real fixed bootstrap."""

import ctypes
import os
import signal
import struct
import time
from pathlib import Path

from .responses_protocol import encode_frame


def run_transport(arm, *, before_request):
    before_request()
    call = arm["call"]
    marker = call.get("marker")
    if marker:
        Path(marker).write_text("dispatched")
    mode = call.get("mode", "result")
    result = {
        key: arm[key]
        for key in ("v", "transport", "attempt_id", "client_request_id", "nonce")
    }
    leaked_fds = []
    for fd in range(8, 128):
        try:
            os.fstat(fd)
        except OSError:
            continue
        leaked_fds.append(fd)
    result.update(
        kind="error" if mode == "error" else "result",
        payload={
            "environment": dict(os.environ),
            "leaked_fds": leaked_fds,
            "lease_inheritable": os.get_inheritable(5),
            "cwd": os.getcwd(),
            "alarm_default": signal.getsignal(signal.SIGALRM) == signal.SIG_DFL,
            "timer": signal.getitimer(signal.ITIMER_REAL)[0],
        },
    )
    if mode == "partial_terminal":
        os.write(4, struct.pack("!I", 50) + b"{")
        os._exit(0)
    if mode == "oversized":
        os.write(4, struct.pack("!I", 2097153))
        os._exit(0)
    if mode == "bad_json":
        os.write(4, struct.pack("!I", 4) + b"oops")
        os._exit(0)
    if mode == "wrong_identity":
        result["nonce"] = "0" * 64
    if mode == "mutate_alarm":
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
    if mode == "subprocess":
        os.posix_spawn("/usr/bin/true", ["true"], {})
    if mode in {"gil", "hang", "result_hang"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if mode == "result_hang":
            os.write(4, encode_frame(result, "terminal"))
        if mode == "gil":
            # PyDLL deliberately holds the GIL, so the liveness thread cannot run.
            ctypes.PyDLL(None).sleep(10)
        else:
            time.sleep(10)
    if mode in {"extra", "nonzero"}:
        os.write(4, encode_frame(result, "terminal"))
        if mode == "extra":
            os.write(4, encode_frame(result, "terminal"))
            os._exit(0)
        os._exit(23)
    return result
