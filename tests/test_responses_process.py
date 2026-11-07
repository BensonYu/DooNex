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
from pathlib import Path

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
    request = process.HelperRequest(
        call={"mode": "result", "marker": str(private / "dispatch")},
        api_key="offline-synthetic-key",
        deadline_monotonic_ns=time.monotonic_ns() + 3_000_000_000,
        lease=process.LeaseSpec(private / "lease", allow_create=True),
        parent_boot_id="offline-test-boot",
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
def test_invalid_or_unfinished_terminal_is_reaped_and_unknown(setup, mode):
    request, hooks, _ = setup
    request = replace(
        request,
        call={"mode": mode},
        deadline_monotonic_ns=time.monotonic_ns() + 350_000_000,
    )
    start = time.monotonic()
    error = run_failure(request, hooks)
    assert time.monotonic() - start < 1.6
    assert error.evidence["reaped"]
    assert hooks.calls[-1][0] == "reaped"
    assert hooks.calls[-1][1]["terminal_summary"]["outcome"] == "failed"


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
    with pytest.raises(FileNotFoundError):
        process.inspect_lease(request.lease)
    assert not path.exists()
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
        request = replace(request, lease=process.LeaseSpec(request.lease.path))
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
def test_bad_ready_never_authorizes_arm(setup, mode):
    request, hooks, runtime = setup
    additions = {
        "pid": "envelope = dict(envelope, pid=envelope['pid'] + 1)",
        "nonce": "envelope = dict(envelope, nonce='0' * 64)",
        "partial": "return b'\\x00\\x00\\x00\\x20{'",
    }
    with (runtime / "responses_protocol.py").open("a") as output:
        output.write(
            "\n_original_encode = encode_frame\ndef encode_frame(envelope, kind):\n    if kind == 'ready':\n        "
            + additions[mode]
            + "\n    return _original_encode(envelope, kind)\n"
        )
    error = run_failure(
        replace(request, deadline_monotonic_ns=time.monotonic_ns() + 200_000_000), hooks
    )
    assert error.evidence["reaped"]
    assert all(state != "armed" for state, _ in hooks.calls)
    assert not Path(request.call["marker"]).exists()


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
    request = replace(
        request,
        call={"mode": "hang"},
        deadline_monotonic_ns=time.monotonic_ns() + 180_000_000,
    )
    real_wait, real_kill, real_sleep = os.waitpid, os.kill, time.sleep
    count = 0

    def once(real):
        def invoke(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise OSError(errno.EIO, "synthetic cleanup failure")
            return real(*args, **kwargs)

        return invoke

    if operation in {"signal", "pause"}:
        original_poll = process._Resources.poll_reap
        skipped = 0

        def skip_initial_poll(self):
            nonlocal skipped
            skipped += 1
            if skipped <= (2 if operation == "pause" else 1):
                return
            return original_poll(self)

        monkeypatch.setattr(process._Resources, "poll_reap", skip_initial_poll)
    if operation == "wait":
        monkeypatch.setattr(os, "waitpid", once(real_wait))
    elif operation == "signal":
        monkeypatch.setattr(os, "kill", once(real_kill))
    elif operation == "pause":
        monkeypatch.setattr(time, "sleep", once(real_sleep))
    elif operation == "selector_close":
        selector_type = (
            selectors.KqueueSelector
            if sys.platform == "darwin"
            else selectors.EpollSelector
        )
        real_close = selector_type.close

        def close_selector(self):
            real_close(self)
            raise OSError(errno.EIO, "synthetic after close")

        monkeypatch.setattr(selector_type, "close", close_selector)
    elif operation == "close":
        real_close = os.close

        def close_fd(fd):
            nonlocal count
            real_close(fd)
            count += 1
            if count == 1:
                raise InterruptedError()

        monkeypatch.setattr(os, "close", close_fd)
    else:
        real_mask = signal.pthread_sigmask

        def mask(how, values):
            # Fail only when outer finite cleanup begins, after ARM dispatch.
            if Path(request.lease.path.parent / "dispatch-cleanup").exists():
                raise OSError(errno.EIO, "synthetic mask failure")
            return real_mask(how, values)

        request = replace(
            request,
            call={
                "mode": "hang",
                "marker": str(request.lease.path.parent / "dispatch-cleanup"),
            },
        )
        monkeypatch.setattr(signal, "pthread_sigmask", mask)
    start = time.monotonic()
    error = run_failure(request, hooks)
    assert time.monotonic() - start < 1.6
    assert error.evidence["reaped"]
    assert error.evidence["errors"]
    assert hooks.calls[-1][0] == "reaped"


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


def test_valid_error_with_late_completion_is_rejected(setup):
    request, hooks, _ = setup
    hooks.delay = "reaped"
    hooks.delay_deadline_ns = time.monotonic_ns() + 1_000_000_000
    error = run_failure(
        replace(
            request,
            call={"mode": "error"},
            deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        ),
        hooks,
    )
    assert error.reason == "deadline"
    assert error.evidence["reaped"]


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
