"""Fixed -I -S entry point.  Before ARM only the standard library is loaded.

FDs 3/4 are framed IPC, 5 is the inherited lifetime flock description, 6
is parent liveness, and 7 is the private cwd directory.  SIGALRM keeps its kernel default disposition (no Python
handler), including while a trusted native dependency holds the GIL.  This is
trusted-code lifecycle isolation, not a hostile-native-code OS sandbox.
"""

import os
import signal
import sys
import time

# Install a native default timer before importing even the protocol.  Never
# replace this with a Python handler: that would wait behind a held GIL.
_native_signal = signal.signal
_native_timer = signal.setitimer
_native_mask = signal.pthread_sigmask
_native_signal(signal.SIGALRM, signal.SIG_DFL)
_native_mask(signal.SIG_UNBLOCK, {signal.SIGALRM})
_native_timer(signal.ITIMER_REAL, 5.0)
# macOS may synthesize __CF_USER_TEXT_ENCODING even for an empty exec env.
os.environ.clear()
os.environ.update({"LANG": "C", "LC_ALL": "C"})
# Bound the remaining pre-ARM startup by the same immutable absolute deadline.
# A malformed startup vector exits before any dependency or key is available.
try:
    _startup_remaining = (int(sys.argv[4]) - time.monotonic_ns()) / 1_000_000_000
    if _startup_remaining <= 0:
        os._exit(72)
    _native_timer(signal.ITIMER_REAL, min(5.0, _startup_remaining))
except BaseException:  # noqa: BLE001 -- fixed bounded bootstrap exit
    os._exit(72)


def _main():
    import hashlib
    import importlib.util
    import resource
    import stat
    import struct
    import threading
    from pathlib import Path

    # Do not load site, .pth files, ambient cwd/PYTHONPATH or parent secrets.
    keep = {3, 4, 5, 6, 7}
    maximum = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if maximum == resource.RLIM_INFINITY:
        maximum = 1_048_576
    for low, high in ((0, 3), (8, maximum)):
        os.closerange(low, high)
    # Import/SDK stdout and stderr cannot enter the protocol pipe.
    null_fd = os.open(os.devnull, os.O_RDWR)
    for target in (0, 1, 2):
        os.dup2(null_fd, target)
    if null_fd not in keep and null_fd > 2:
        os.close(null_fd)
    for fd in keep:
        os.set_inheritable(fd, False)
    if len(sys.argv) != 8:
        os._exit(72)
    (
        attempt_id,
        client_request_id,
        nonce,
        deadline_text,
        packages,
        protocol_hash,
        transport_hash,
    ) = sys.argv[1:]
    deadline = int(deadline_text)
    directory = Path(__file__).resolve().parent
    # The inherited directory FD was validated by the parent.  No project cwd,
    # ambient path or user-selected import path is consulted.
    os.fchdir(7)
    os.close(7)
    cwd_info = os.stat(".")
    if stat.S_IMODE(cwd_info.st_mode) != 0o700 or cwd_info.st_uid != os.geteuid():
        os._exit(72)
    lease = os.fstat(5)
    if (
        not stat.S_ISREG(lease.st_mode)
        or stat.S_IMODE(lease.st_mode) != 0o600
        or lease.st_uid != os.geteuid()
        or lease.st_nlink != 1
    ):
        os._exit(72)
    armed = False

    def audit(event, _args):
        if event in {
            "os.fork",
            "os.forkpty",
            "os.posix_spawn",
            "os.exec",
            "subprocess.Popen",
        }:
            raise RuntimeError("helper_subprocess_denied")
        if not armed and event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.sendto",
        }:
            raise RuntimeError("helper_prearm_network_denied")

    sys.addaudithook(audit)

    def parent_liveness():
        try:
            # Original parent is the only writer.  Any byte or EOF is terminal.
            os.read(6, 1)
        finally:
            os._exit(73)

    threading.Thread(target=parent_liveness, daemon=True).start()

    def load(name, path, expected):
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise RuntimeError("helper_source_changed")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        # Execute the exact bytes just hashed; do not allow a timestamp-based
        # .pyc or a second path read to choose different source.
        exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102 -- fixed hashed trusted source
        return module

    import types

    package = types.ModuleType("doonex")
    package.__path__ = [str(directory)]
    sys.modules["doonex"] = package
    protocol = load(
        "doonex.responses_protocol", directory / "responses_protocol.py", protocol_hash
    )
    sys.modules["responses_protocol"] = protocol
    identity = {
        "attempt_id": attempt_id,
        "client_request_id": client_request_id,
        "nonce": nonce,
    }

    def write_all(frame):
        view = memoryview(frame)
        while view:
            try:
                written = os.write(4, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise RuntimeError("helper_write")
            view = view[written:]

    def read_exact(length):
        result = bytearray()
        while len(result) < length:
            try:
                part = os.read(3, length - len(result))
            except InterruptedError:
                continue
            if not part:
                raise RuntimeError("helper_eof")
            result.extend(part)
        return bytes(result)

    ready = {
        "v": 1,
        "transport": "isolated_direct_v1",
        **identity,
        "kind": "ready",
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "lease_dev": lease.st_dev,
        "lease_ino": lease.st_ino,
    }
    write_all(protocol.encode_frame(ready, "ready"))
    length = struct.unpack("!I", read_exact(4))[0]
    if not 0 < length <= protocol.FRAME_LIMITS["arm"]:
        raise RuntimeError("helper_frame_size")
    arm = protocol.decode_frame_payload(read_exact(length), "arm", identity)
    # Parent sends one ARM then closes.  Extra input is rejected before any
    # network dependency is imported; liveness uses the separate pipe.
    if os.read(3, 1) != b"":
        raise RuntimeError("helper_extra_arm")
    if arm["deadline_monotonic_ns"] != deadline:
        raise RuntimeError("helper_deadline_identity")
    remaining = (deadline - time.monotonic_ns()) / 1_000_000_000
    if remaining <= 0:
        raise RuntimeError("helper_deadline")
    _native_timer(signal.ITIMER_REAL, remaining)
    armed = True

    def guarded_signal(number, handler):
        if number == signal.SIGALRM:
            raise RuntimeError("helper_alarm_mutation")
        return _native_signal(number, handler)

    def guarded_timer(which, seconds, interval=0.0):
        if which == signal.ITIMER_REAL:
            raise RuntimeError("helper_alarm_mutation")
        return _native_timer(which, seconds, interval)

    def guarded_mask(how, mask):
        if how in {signal.SIG_BLOCK, signal.SIG_SETMASK} and signal.SIGALRM in mask:
            raise RuntimeError("helper_alarm_mutation")
        return _native_mask(how, mask)

    def guarded_alarm(_seconds):
        raise RuntimeError("helper_alarm_mutation")

    signal.signal = guarded_signal
    signal.setitimer = guarded_timer
    signal.pthread_sigmask = guarded_mask
    signal.alarm = guarded_alarm

    def before_request():
        current_mask = _native_mask(signal.SIG_BLOCK, set())
        if (
            time.monotonic_ns() >= deadline
            or signal.getsignal(signal.SIGALRM) != signal.SIG_DFL
            or signal.getitimer(signal.ITIMER_REAL)[0] <= 0
            or signal.SIGALRM in current_mask
        ):
            raise RuntimeError("helper_alarm_invalid")

    # No site.main() or .pth.  Protocol was fixed stdlib source; network-capable
    # dependencies and the trusted transport are loaded only after valid ARM.
    package_path = Path(packages)
    if not package_path.is_absolute() or not package_path.is_dir():
        raise RuntimeError("helper_runtime")
    sys.path.append(str(package_path))
    sys.path.append(str(directory))
    before_request()
    transport = load(
        "doonex.responses_transport",
        directory / "responses_transport.py",
        transport_hash,
    )
    terminal = transport.run_transport(arm, before_request=before_request)
    frame = protocol.encode_frame(terminal, "terminal")
    before_request()
    write_all(frame)
    os.close(4)
    # os._exit avoids unbounded dependency atexit hooks/background thread joins.
    os._exit(0)


if __name__ == "__main__":
    try:
        _main()
    except BaseException:  # noqa: BLE001 -- terminal exit, never raw diagnostics
        # Never print exceptions, provider bodies, keys or module import output.
        os._exit(72)
