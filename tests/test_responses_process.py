"""Finite no-network process contracts; outer runner must retain its watchdog."""

import errno
import fcntl
import importlib.util
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from doonex import responses_process as process

_FIXTURES = Path(__file__).parent / "fixtures" / "responses_helper"


class Hooks:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.delay = None
        self.delay_deadline_ns = None
        self.invalid = None

    def callback(self, state):
        def invoke(data):
            self.calls.append((state, data))
            if self.delay == state:
                delay = (
                    0.15
                    if self.delay_deadline_ns is None
                    else max(
                        0,
                        (self.delay_deadline_ns - time.monotonic_ns()) / 1_000_000_000,
                    )
                    + 0.05
                )
                time.sleep(delay)
            if self.fail == state:
                raise RuntimeError("synthetic callback failure")
            result = {
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
            if self.invalid == state:
                result["nonce"] = "f" * 64
            return result

        return invoke

    def hooks(self):
        return process.HelperHooks(
            *(self.callback(s) for s in ("prepared", "armed", "reaped", "blocked"))
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    shutil.copyfile(
        Path(process.__file__).with_name("responses_helper_bootstrap.py"),
        runtime / "responses_helper_bootstrap.py",
    )
    shutil.copyfile(
        _FIXTURES / "protocol_fixture.py", runtime / "responses_protocol.py"
    )
    shutil.copyfile(
        _FIXTURES / "transport_fixture.py", runtime / "responses_transport.py"
    )
    spec = importlib.util.spec_from_file_location(
        "offline_protocol_fixture", runtime / "responses_protocol.py"
    )
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)
    monkeypatch.setattr(process, "_protocol", lambda: protocol)
    monkeypatch.setattr(
        process,
        "_runtime",
        lambda: process._Runtime(
            Path(sys.executable),
            runtime / "responses_helper_bootstrap.py",
            runtime / "responses_protocol.py",
            runtime / "responses_transport.py",
            runtime,
        ),
    )
    deadline = time.monotonic_ns() + 3_000_000_000
    lease = process.prepare_lease(
        process.LeaseSpec(private / "lease", allow_create=True)
    )
    attempt = process.HelperAttempt(
        str(uuid4()),
        str(uuid4()),
        uuid4().hex + uuid4().hex,
        os.getpid(),
        time.monotonic_ns(),
        "offline-test-boot",
        datetime.now(UTC).isoformat(),
        lease.expected_identity,
        deadline,
        "0" * 64,
    )
    request = process.HelperRequest(
        call={"mode": "result", "marker": str(private / "dispatch")},
        api_key="offline-synthetic-key",
        deadline_monotonic_ns=deadline,
        lease=lease,
        attempt=attempt,
    )
    return request, Hooks(), runtime


def fd_set():
    result = set()
    for fd in range(256):
        try:
            os.fstat(fd)
        except OSError:
            continue
        result.add(fd)
    return result


def run_failure(request, hooks):
    before = fd_set()
    with pytest.raises(process.HelperProcessError) as caught:
        process.run_helper(request, hooks.hooks())
    assert fd_set() == before
    return caught.value


def observe_failure_lifecycle(request, monkeypatch):
    """Observe the existing deadline/pipe; never supply another execution window."""
    observed = {"deadlines": [], "reads": [], "eof": [], "closed": []}
    original_deadline = process._deadline
    original_frame, original_read = process._read_frame, os.read
    original_close = process._Resources.close_all
    output_fd = None

    def deadline(value):
        observed["deadlines"].append(value)
        return original_deadline(value)

    def frame(resources, fd, *args):
        nonlocal output_fd
        output_fd = fd
        return original_frame(resources, fd, *args)

    def read(fd, maximum):
        value = original_read(fd, maximum)
        if fd == output_fd:
            observed["reads"].append(value)
        return value

    def close(resources):
        try:
            if resources.reap is not None and output_fd in resources.fds:
                # Independent observation after actual waitpid, before the real
                # close. Drain only this fixture's bounded output, without a wait
                # or any replacement product deadline. The Run already failed.
                for _ in range(65):
                    try:
                        tail = original_read(output_fd, 65536)
                    except BlockingIOError:
                        break
                    if not tail:
                        observed["eof"].append(dict(resources.reap))
                        break
        finally:
            original_close(resources)
            observed["closed"].append(not resources.fds and resources.selector is None)

    monkeypatch.setattr(process, "_deadline", deadline)
    monkeypatch.setattr(process, "_read_frame", frame)
    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(process._Resources, "close_all", close)
    return observed


def assert_failed_lifecycle(request, hooks, error, observed):
    assert observed["deadlines"]
    assert set(observed["deadlines"]) == {request.deadline_monotonic_ns}
    assert error.evidence["spawn_attempted"] and error.evidence["reaped"]
    assert hooks.calls[-1][0] == "reaped"
    completion = hooks.calls[-1][1]
    assert completion["reap"]["pid"] == error.evidence["child_pid"]
    assert completion["terminal_summary"]["outcome"] == "failed"
    assert observed["eof"] and all(
        receipt == completion["reap"] for receipt in observed["eof"]
    )
    assert observed["closed"] and all(observed["closed"])
    assert all(
        data["deadline_monotonic_ns"] == request.deadline_monotonic_ns
        for state, data in hooks.calls
        if state in {"prepared", "armed"}
    )
    with pytest.raises(ChildProcessError):
        os.waitpid(error.evidence["child_pid"], os.WNOHANG)
    assert not process.inspect_lease(process.LeaseSpec(request.lease.path)).locked



def test_success_requires_reap_and_persistent_completion(setup, monkeypatch):
    request, hooks, _ = setup
    monkeypatch.setenv("DOONEX_DATABASE_URL", "must-not-inherit")
    monkeypatch.setenv("SYNTHETIC_OTHER_SECRET", "must-not-inherit")
    before = fd_set()
    result = process.run_helper(request, hooks.hooks())
    assert [s for s, _ in hooks.calls] == ["prepared", "armed", "reaped"]
    assert result.reap["exit_code"] == 0
    assert result.completion["state"] == "reaped"
    assert result.envelope["payload"]["environment"] == {"LANG": "C", "LC_ALL": "C"}
    assert result.envelope["payload"]["cwd"] == str(request.lease.path.parent)
    assert result.envelope["payload"]["alarm_default"]
    assert result.envelope["payload"]["timer"] > 0
    assert "offline-synthetic-key" not in json.dumps(hooks.calls)
    assert fd_set() == before
    with pytest.raises(ChildProcessError):
        os.waitpid(result.reap["pid"], os.WNOHANG)
    inspection = process.inspect_lease(process.LeaseSpec(request.lease.path))
    assert not inspection.locked


@pytest.mark.parametrize("state", ["prepared", "armed", "reaped"])
@pytest.mark.parametrize("invalid", [False, True])
def test_hook_exception_or_wrong_ack_never_accepts_result(setup, state, invalid):
    request, hooks, _ = setup
    if invalid:
        hooks.invalid = state
    else:
        hooks.fail = state
    error = run_failure(request, hooks)
    assert error.evidence["reaped"] == (state != "prepared")
    if state in {"prepared", "armed"}:
        assert not Path(request.call["marker"]).exists()
    assert hooks.calls[-1][0] == ("reaped" if state == "armed" else "blocked")


@pytest.mark.parametrize("state", ["prepared", "armed", "reaped"])
def test_hook_that_outlasts_deadline_does_not_authorize_next_phase(setup, state):
    request, hooks, _ = setup
    hooks.delay = state
    request = replace(
        request,
        deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
    )
    hooks.delay_deadline_ns = request.deadline_monotonic_ns
    error = run_failure(request, hooks)
    assert any(which == state for which, _ in hooks.calls)
    assert error.reason == "deadline"
    if state != "reaped":
        assert not Path(request.call["marker"]).exists()


@pytest.mark.parametrize(
    "mode",
    ["extra", "nonzero", "mutate_alarm", "subprocess", "result_hang", "gil"],
)
def test_invalid_or_unfinished_terminal_is_reaped_and_unknown(setup, monkeypatch, mode):
    request, hooks, runtime = setup
    marker = request.lease.path.parent / "terminal-mode-entered.json"
    transport = runtime / "responses_transport.py"
    source = transport.read_text().replace("import ctypes\n", "import ctypes\nimport json\n")
    branch = {
        "extra": '    if mode in {"extra", "nonzero"}:\n',
        "nonzero": '    if mode in {"extra", "nonzero"}:\n',
        "mutate_alarm": '    if mode == "mutate_alarm":\n',
        "subprocess": '    if mode == "subprocess":\n',
        "result_hang": '        if mode == "result_hang":\n',
        "gil": '        if mode == "gil":\n',
    }[mode]
    indent = " " * (len(branch) - len(branch.lstrip()) + 4)
    assert source.count(branch) == 1
    source = source.replace(
        branch,
        branch
        + indent
        + f"Path({str(marker)!r}).write_text(json.dumps(dict(mode=mode, pid=os.getpid(), "
        + "deadline=arm['deadline_monotonic_ns'], entered=time.monotonic_ns())))\n",
    )
    transport.write_text(source)
    # Old .35s included interpreter startup and could pass before this branch.
    # One 2s absolute entry window now requires the named terminal fault itself;
    # it is shared by bootstrap/ARM/read/reap and never refreshed at the marker.
    request = replace(
        request,
        call={"mode": mode},
        deadline_monotonic_ns=time.monotonic_ns() + 2_000_000_000,
    )
    observed = observe_failure_lifecycle(request, monkeypatch)
    error = run_failure(request, hooks)
    assert marker.exists(), "The named terminal fault did not run"
    entered = json.loads(marker.read_text())
    assert entered["mode"] == mode and entered["pid"] == error.evidence["child_pid"]
    assert entered["deadline"] == request.deadline_monotonic_ns
    assert entered["entered"] < request.deadline_monotonic_ns
    assert time.monotonic_ns() < request.deadline_monotonic_ns + 1_500_000_000
    assert [state for state, _ in hooks.calls] == ["prepared", "armed", "reaped"]
    reasons = {
        "extra": {"extra_frame"},
        "nonzero": {"child_exit"},
        "mutate_alarm": {"early_eof"},
        "subprocess": {"early_eof"},
        "result_hang": {"deadline"},
        "gil": {"deadline", "early_eof"},
    }
    assert error.reason in reasons[mode]
    if mode in {"extra", "nonzero", "result_hang"}:
        assert hooks.calls[-1][1]["terminal_summary"]["kind"] == "result"
    if mode == "nonzero":
        assert hooks.calls[-1][1]["reap"]["exit_code"] == 23
    assert_failed_lifecycle(request, hooks, error, observed)


@pytest.mark.parametrize("at", [1, 2, 3])
def test_partial_pipe_allocation_closes_prior_descriptors(setup, monkeypatch, at):
    request, hooks, _ = setup
    original = os.pipe
    calls = 0

    def fault():
        nonlocal calls
        calls += 1
        if calls == at:
            raise OSError(errno.EMFILE, "synthetic")
        return original()

    monkeypatch.setattr(os, "pipe", fault)
    error = run_failure(request, hooks)
    assert not error.evidence["spawn_attempted"]
    assert hooks.calls[-1][0] == "blocked"


@pytest.mark.parametrize(
    "which", ["duplicate", "spawn", "selector", "select", "read", "write"]
)
def test_parent_operation_failures_share_cleanup(setup, monkeypatch, which):
    request, hooks, _ = setup

    def fault(*args, **kwargs):
        raise OSError(errno.EIO, "synthetic")

    if which == "duplicate":
        monkeypatch.setattr(process._Resources, "duplicate", fault)
    elif which == "spawn":
        monkeypatch.setattr(os, "posix_spawn", fault)
    elif which == "selector":
        monkeypatch.setattr(selectors, "DefaultSelector", fault)
    elif which == "select":
        monkeypatch.setattr(
            selectors.KqueueSelector
            if sys.platform == "darwin"
            else selectors.EpollSelector,
            "select",
            fault,
        )
    else:
        monkeypatch.setattr(os, which, fault)
    error = run_failure(request, hooks)
    assert error.evidence["reaped"] == (which not in {"duplicate", "spawn"})
    if which != "write":
        assert not Path(request.call["marker"]).exists()


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("state", ["prepared", "armed", "reaped"])
def test_control_exceptions_preserved_after_cleanup(setup, control, state):
    request, hooks, _ = setup
    original = hooks.callback

    def callback(which):
        if which != state:
            return original(which)

        def fail(_data):
            raise control()

        return fail

    hooks.callback = callback
    before = fd_set()
    with pytest.raises(control):
        process.run_helper(request, hooks.hooks())
    assert fd_set() == before
    assert hooks.calls[-1][0] == ("reaped" if state == "armed" else "blocked")


def test_echild_never_claims_reap_or_signals_reused_pid(setup, monkeypatch):
    request, hooks, _ = setup
    real_wait = os.waitpid
    kills = []

    def lost(pid, _flags):
        # Simulate a forbidden external reaper consuming the child's status.
        real_wait(pid, 0)
        raise ChildProcessError()

    monkeypatch.setattr(os, "waitpid", lost)
    monkeypatch.setattr(os, "kill", lambda *args: kills.append(args))
    error = run_failure(request, hooks)
    assert not error.evidence["reaped"]
    assert "wait_echild" in error.evidence["errors"]
    assert not kills
    assert hooks.calls[-1][0] == "blocked"


def test_block_failure_preserves_prepared_record(setup):
    request, hooks, _ = setup
    hooks.fail = "blocked"
    hooks.invalid = "prepared"
    error = run_failure(request, hooks)
    assert "block" in error.evidence["errors"]
    assert hooks.calls[0][0] == "prepared"


def test_inspection_refuses_missing_symlink_mode_and_changed_identity(setup):
    request, hooks, _ = setup
    path = request.lease.path
    missing = path.with_name("missing-lease")
    with pytest.raises(FileNotFoundError):
        process.inspect_lease(process.LeaseSpec(missing))
    assert not missing.exists()
    process.run_helper(request, hooks.hooks())
    original = process.inspect_lease(process.LeaseSpec(path)).identity
    os.chmod(path, 0o644)
    with pytest.raises(process.HelperProcessError, match="lease_invalid"):
        process.inspect_lease(process.LeaseSpec(path, original))
    os.chmod(path, 0o600)
    moved = path.with_name("old-lease")
    path.rename(moved)
    path.touch(mode=0o600)
    with pytest.raises(process.HelperProcessError, match="lease_changed"):
        process.inspect_lease(process.LeaseSpec(path, original))
    path.unlink()
    path.symlink_to(moved)
    with pytest.raises(OSError):
        process.inspect_lease(process.LeaseSpec(path, original))


def test_inherited_lease_lock_is_not_explicitly_unlocked(setup):
    request, hooks, _ = setup
    process.run_helper(request, hooks.hooks())
    fd = os.open(request.lease.path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert process.inspect_lease(process.LeaseSpec(request.lease.path)).locked
        request = replace(
            request,
            lease=process.LeaseSpec(request.lease.path, request.attempt.lease_identity),
        )
        error = run_failure(request, Hooks())
        assert error.reason == "lease_locked"
    finally:
        os.close(fd)
    assert not process.inspect_lease(process.LeaseSpec(request.lease.path)).locked


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Mac orphan observation uses kqueue, never stored-PID kill",
)
@pytest.mark.parametrize("mode", ["hang", "gil"])
def test_actual_parent_sigkill_leaves_orphan_fence_without_claiming_reap(setup, mode):
    request, _hooks, runtime = setup
    private = request.lease.path.parent
    receipt = private / "owner-receipt.json"
    owner = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            str(_FIXTURES / "orphan_owner.py"),
            str(Path(process.__file__).parents[1]),
            str(runtime),
            str(private),
            str(receipt),
            mode,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    observer = None
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                saved = json.loads(receipt.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.005)
                continue
            if saved["state"] == "armed":
                break
            time.sleep(0.005)
        else:
            pytest.fail("owner did not arm")
        child_pid = saved["data"]["child"]["pid"]
        while not (private / "dispatched").exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert (private / "dispatched").exists()
        time.sleep(0.02)  # Let the marked native GIL-holding call actually start.
        observer = selectors.select.kqueue()
        observer.control(
            [
                selectors.select.kevent(
                    child_pid,
                    filter=selectors.select.KQ_FILTER_PROC,
                    flags=selectors.select.KQ_EV_ADD | selectors.select.KQ_EV_ENABLE,
                    fflags=selectors.select.KQ_NOTE_EXIT,
                )
            ],
            0,
            0,
        )
        owner.kill()
        assert owner.wait(timeout=1) == -signal.SIGKILL
        events = observer.control([], 1, 3)
        assert events and events[0].ident == child_pid
        assert json.loads(receipt.read_text())["state"] == "armed"
        assert not process.inspect_lease(process.LeaseSpec(request.lease.path)).locked
        with pytest.raises(ChildProcessError):
            os.waitpid(child_pid, os.WNOHANG)
    finally:
        if observer is not None:
            observer.close()
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=1)


@pytest.mark.parametrize(
    "mode", ["partial_terminal", "oversized", "bad_json", "wrong_identity"]
)
def test_corrupt_terminal_never_returns_response(setup, mode):
    request, hooks, _ = setup
    error = run_failure(replace(request, call={"mode": mode}), hooks)
    assert error.evidence["reaped"]
    assert hooks.calls[-1][1]["terminal_summary"]["outcome"] == "failed"


@pytest.mark.parametrize("mode", ["pid", "nonce", "partial"])
def test_bad_ready_never_authorizes_arm(setup, monkeypatch, mode):
    request, hooks, runtime = setup
    marker = request.lease.path.parent / "mutated-ready.json"
    additions = {
        "pid": "envelope = dict(envelope, pid=envelope['pid'] + 1)",
        "nonce": "envelope = dict(envelope, nonce='0' * 64)",
        "partial": "return b'\\x00\\x00\\x00\\x20{'",
    }
    with (runtime / "responses_protocol.py").open("a") as output:
        output.write(
            "\nimport os, sys, time\n_original_encode = encode_frame\ndef encode_frame(envelope, kind):\n    if kind == 'ready':\n"
            + f"        with open({str(marker)!r}, 'w') as receipt:\n"
            + "            receipt.write(json.dumps(dict(pid=os.getpid(), deadline=int(sys.argv[4]), entered=time.monotonic_ns())))\n"
            + "        "
            + additions[mode]
            + "\n    return _original_encode(envelope, kind)\n"
        )
    # Old .2s could expire before child READY. This single 1s startup window
    # requires both the mutation marker and its actual bytes in the parent pipe.
    request = replace(request, deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000)
    observed = observe_failure_lifecycle(request, monkeypatch)
    error = run_failure(request, hooks)
    assert marker.exists(), "Child never constructed the mutated READY"
    entered = json.loads(marker.read_text())
    assert entered["pid"] == error.evidence["child_pid"]
    assert entered["deadline"] == request.deadline_monotonic_ns
    assert entered["entered"] < request.deadline_monotonic_ns
    received = b"".join(observed["reads"])
    if mode == "partial":
        assert received == b"\x00\x00\x00\x20{"
        assert error.reason in {"deadline", "early_eof"}
    else:
        ready = json.loads(received[4:])
        assert int.from_bytes(received[:4], "big") == len(received) - 4
        if mode == "pid":
            assert ready["pid"] == entered["pid"] + 1
            assert error.reason == "child_identity"
        else:
            assert ready["nonce"] == "0" * 64
            assert error.reason == "ready"
    assert [state for state, _ in hooks.calls] == ["prepared", "reaped"]
    assert not Path(request.call["marker"]).exists()
    assert_failed_lifecycle(request, hooks, error, observed)


def test_extra_arm_is_rejected_before_transport_import(setup, monkeypatch):
    request, hooks, _ = setup
    protocol = process._protocol()
    encode = protocol.encode_frame
    monkeypatch.setattr(
        protocol,
        "encode_frame",
        lambda envelope, kind: encode(envelope, kind) + b"extra",
    )
    error = run_failure(request, hooks)
    assert error.evidence["reaped"]
    assert not Path(request.call["marker"]).exists()


def test_before_arm_socket_audit_denies_network(setup):
    request, hooks, runtime = setup
    with (runtime / "responses_protocol.py").open("a") as output:
        output.write(
            "\nimport socket\ntry:\n    socket.socket().connect(('127.0.0.1', 1))\nexcept RuntimeError as error:\n    assert str(error) == 'helper_prearm_network_denied'\nelse:\n    raise AssertionError('audit guard absent')\n"
        )
    result = process.run_helper(request, hooks.hooks())
    assert result.reap["exit_code"] == 0


@pytest.mark.parametrize("operation", ["open", "fsync", "flock"])
def test_lease_setup_failures_never_spawn(setup, monkeypatch, operation):
    request, hooks, _ = setup

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "synthetic")

    monkeypatch.setattr(fcntl if operation == "flock" else os, operation, fail)
    if operation == "fsync":
        before = fd_set()
        with pytest.raises(OSError):
            process.prepare_lease(
                process.LeaseSpec(
                    request.lease.path.with_name("new-lease"), allow_create=True
                )
            )
        assert fd_set() == before
        assert not hooks.calls
        return
    error = run_failure(request, hooks)
    assert not error.evidence["spawn_attempted"]
    assert not Path(request.call["marker"]).exists()


@pytest.mark.parametrize(
    "operation", ["wait", "signal", "pause", "selector_close", "close", "mask"]
)
def test_cleanup_operation_failure_does_not_skip_later_cleanup(
    setup, monkeypatch, operation
):
    request, hooks, _ = setup
    marker = request.lease.path.parent / "dispatch-cleanup"
    # Old .18s could stop before ARM and never inject the intended cleanup fault.
    # One 1s absolute window must reach the real hanging transport; the fault
    # below is admitted only inside the existing finite cleanup, never startup.
    request = replace(
        request,
        call={"mode": "hang", "marker": str(marker)},
        deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
    )
    observed = observe_failure_lifecycle(request, monkeypatch)
    real_cleanup = process._Resources.cleanup
    cleanup, injected = [], []
    active = False

    def cleaning(resources):
        nonlocal active
        cleanup.append((resources.pid, marker.exists()))
        active = True
        try:
            return real_cleanup(resources)
        finally:
            active = False

    monkeypatch.setattr(process._Resources, "cleanup", cleaning)

    def once(real):
        def invoke(*args, **kwargs):
            if active and not injected:
                injected.append(operation)
                raise OSError(errno.EIO, "synthetic cleanup failure")
            return real(*args, **kwargs)

        return invoke

    if operation in {"signal", "pause"}:
        original_poll = process._Resources.poll_reap
        skipped = 0

        def skip_initial_poll(self):
            nonlocal skipped
            if active:
                skipped += 1
                if skipped <= (2 if operation == "pause" else 1):
                    return
            return original_poll(self)

        monkeypatch.setattr(process._Resources, "poll_reap", skip_initial_poll)
    if operation == "wait":
        monkeypatch.setattr(os, "waitpid", once(os.waitpid))
    elif operation == "signal":
        monkeypatch.setattr(os, "kill", once(os.kill))
    elif operation == "pause":
        monkeypatch.setattr(time, "sleep", once(time.sleep))
    elif operation == "selector_close":
        selector_type = (
            selectors.KqueueSelector
            if sys.platform == "darwin"
            else selectors.EpollSelector
        )
        real_close = selector_type.close

        def close_selector(self):
            real_close(self)
            if active and not injected:
                injected.append(operation)
                raise OSError(errno.EIO, "synthetic after close")

        monkeypatch.setattr(selector_type, "close", close_selector)
    elif operation == "close":
        real_close = os.close

        def close_fd(fd):
            real_close(fd)
            if active and not injected:
                injected.append(operation)
                raise InterruptedError()

        monkeypatch.setattr(os, "close", close_fd)
    else:
        real_mask = signal.pthread_sigmask

        def mask(how, values):
            # The outer cleanup's mask precedes _Resources.cleanup itself.
            if marker.exists() and how == signal.SIG_BLOCK and not injected:
                injected.append(operation)
                raise OSError(errno.EIO, "synthetic mask failure")
            return real_mask(how, values)

        monkeypatch.setattr(signal, "pthread_sigmask", mask)
    error = run_failure(request, hooks)
    assert marker.read_text() == "dispatched"
    assert cleanup == [(error.evidence["child_pid"], True)]
    assert injected == [operation], "The named cleanup operation was never reached"
    assert time.monotonic_ns() < request.deadline_monotonic_ns + 1_500_000_000
    code = {"pause": "cleanup_pause", "mask": "signal_mask"}.get(operation, operation)
    assert code in error.evidence["errors"]
    assert [state for state, _ in hooks.calls] == ["prepared", "armed", "reaped"]
    assert_failed_lifecycle(request, hooks, error, observed)


def test_unresolved_reap_stays_blocked_with_finite_cleanup(setup, monkeypatch):
    request, hooks, _ = setup
    request = replace(
        request,
        call={"mode": "result"},
        deadline_monotonic_ns=time.monotonic_ns() + 120_000_000,
    )
    real_wait = os.waitpid

    def cannot_wait(*args):
        raise OSError(errno.EIO, "synthetic wait failure")

    monkeypatch.setattr(os, "waitpid", cannot_wait)
    start = time.monotonic()
    error = run_failure(request, hooks)
    assert time.monotonic() - start < 1.6
    assert not error.evidence["reaped"]
    assert hooks.calls[-1][0] == "blocked"
    assert not any(state == "reaped" for state, _ in hooks.calls)
    # Test harness retains original parent authority; only it clears the fixture
    # zombie after confirming product did not invent a successful wait receipt.
    waited, _status = real_wait(error.evidence["child_pid"], 0)
    assert waited == error.evidence["child_pid"]


def test_valid_error_terminal_preserves_diagnostics_after_reap_receipt(setup):
    request, hooks, _ = setup
    result = process.run_helper(replace(request, call={"mode": "error"}), hooks.hooks())
    assert result.envelope["kind"] == "error"
    assert result.reap["exit_code"] == 0
    assert result.completion["state"] == "reaped"
    assert hooks.calls[-1][1]["terminal_summary"] == {
        "kind": "error",
        "outcome": "failed",
        "reason": "helper_error",
    }


def test_ambiguous_spawn_never_invents_pid_or_reap(setup, monkeypatch):
    request, hooks, _ = setup
    real_spawn = os.posix_spawn
    spawned = []

    def ambiguous(*args, **kwargs):
        spawned.append(real_spawn(*args, **kwargs))
        raise OSError(errno.EIO, "synthetic failure after actual spawn")

    monkeypatch.setattr(os, "posix_spawn", ambiguous)
    error = run_failure(request, hooks)
    assert error.evidence["spawn_attempted"]
    assert error.evidence["child_pid"] is None
    assert not error.evidence["reaped"]
    assert hooks.calls[-1][0] == "blocked"
    assert not Path(request.call["marker"]).exists()
    # Independent finite harness still has the real direct-child PID.  The
    # product deliberately did not use the test-only hidden return value.
    end = time.monotonic() + 2
    while time.monotonic() < end:
        waited, _ = os.waitpid(spawned[0], os.WNOHANG)
        if waited:
            break
        time.sleep(0.01)
    else:
        pytest.fail("unarmed ambiguous-spawn fixture did not self-exit")


def test_partial_duplication_failure_closes_every_prior_duplicate(setup, monkeypatch):
    request, hooks, _ = setup
    real_duplicate = process._Resources.duplicate
    count = 0

    def fail_third(self, fd):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError(errno.EMFILE, "synthetic third dup failure")
        return real_duplicate(self, fd)

    monkeypatch.setattr(process._Resources, "duplicate", fail_third)
    error = run_failure(request, hooks)
    assert not error.evidence["spawn_attempted"]


def test_cleanup_control_exception_is_not_swallowed(setup, monkeypatch):
    request, hooks, _ = setup
    real_close = os.close
    count = 0

    def interrupted_after_close(fd):
        nonlocal count
        real_close(fd)
        count += 1
        if count == 1:
            raise KeyboardInterrupt()

    monkeypatch.setattr(os, "close", interrupted_after_close)
    before = fd_set()
    with pytest.raises(KeyboardInterrupt):
        process.run_helper(request, hooks.hooks())
    assert fd_set() == before
    assert hooks.calls[-1][0] == "reaped"


def test_valid_error_with_late_completion_is_rejected(setup, monkeypatch):
    request, hooks, _ = setup
    # Keep the existing 1s execution contract; use one exact absolute value for
    # the helper and delayed completion so no second clock grants extra time.
    request = replace(
        request,
        call={"mode": "error"},
        deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
    )
    hooks.delay = "reaped"
    hooks.delay_deadline_ns = request.deadline_monotonic_ns
    original_callback = hooks.callback
    completion = []

    def callback(state):
        original = original_callback(state)
        if state != "reaped":
            return original

        def delayed(data):
            record = {"entered": time.monotonic_ns(), "data": data}
            completion.append(record)
            try:
                return original(data)
            finally:
                record["returned"] = time.monotonic_ns()

        return delayed

    hooks.callback = callback
    observed = observe_failure_lifecycle(request, monkeypatch)
    error = run_failure(request, hooks)
    assert len(completion) == 1
    entered = completion[0]
    assert entered["entered"] < request.deadline_monotonic_ns < entered["returned"]
    assert entered["data"]["terminal_summary"] == {
        "kind": "error",
        "outcome": "failed",
        "reason": "helper_error",
    }
    assert entered["data"]["reap"]["exit_code"] == 0
    assert [state for state, _ in hooks.calls] == ["prepared", "armed", "reaped"]
    assert error.reason == "deadline"
    assert_failed_lifecycle(request, hooks, error, observed)


def test_bootstrap_closes_ambient_inheritable_descriptors(setup):
    request, hooks, _ = setup
    fd = os.open(
        request.lease.path.parent / "ambient-file", os.O_CREAT | os.O_RDWR, 0o600
    )
    inherited = fcntl.fcntl(fd, fcntl.F_DUPFD, 64)
    os.set_inheritable(inherited, True)
    try:
        result = process.run_helper(request, hooks.hooks())
        assert result.envelope["payload"]["leaked_fds"] == []
        assert result.envelope["payload"]["lease_inheritable"] is False
    finally:
        os.close(inherited)
        os.close(fd)


@pytest.mark.parametrize("source", ["protocol", "transport"])
def test_exact_source_hash_mismatch_never_dispatches(setup, monkeypatch, source):
    request, hooks, runtime = setup
    original = process._hash
    bad_source = runtime / f"responses_{source}.py"
    monkeypatch.setattr(
        process,
        "_hash",
        lambda path: "0" * 64 if path == bad_source else original(path),
    )
    error = run_failure(request, hooks)
    assert error.evidence["reaped"]
    assert not Path(request.call["marker"]).exists()
    assert any(state == "armed" for state, _ in hooks.calls) == (source == "transport")


def test_bootstrap_does_not_execute_site_pth_files(setup):
    request, hooks, runtime = setup
    marker = request.lease.path.parent / "pth-executed"
    (runtime / "unexpected.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n"
    )
    result = process.run_helper(request, hooks.hooks())
    assert result.reap["exit_code"] == 0
    assert not marker.exists()


def test_repeated_sigint_waits_for_actual_child_reap_and_receipt(setup):
    request, _hooks, runtime = setup
    private = request.lease.path.parent
    receipt = private / "signal-owner-receipt.json"
    owner = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            str(_FIXTURES / "orphan_owner.py"),
            str(Path(process.__file__).parents[1]),
            str(runtime),
            str(private),
            str(receipt),
            "gil",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if (private / "dispatched").exists():
                break
            time.sleep(0.005)
        else:
            pytest.fail("signal fixture did not dispatch")
        saved = json.loads(receipt.read_text())
        assert saved["state"] == "armed"
        child = saved["data"]["child"]["pid"]
        time.sleep(0.02)
        owner.send_signal(signal.SIGINT)
        time.sleep(0.02)
        owner.send_signal(signal.SIGINT)
        assert owner.wait(timeout=3) != 0
        final = json.loads(receipt.read_text())
        assert final["state"] == "reaped"
        assert final["data"]["reap"]["pid"] == child
        assert final["data"]["reap"]["exit_code"] == -signal.SIGKILL
        assert not process.inspect_lease(process.LeaseSpec(request.lease.path)).locked
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=1)
