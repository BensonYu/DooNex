import dataclasses
import fcntl
import hashlib
import json
import os
import time

import pytest
from doonex import sdk  # Configure the dedicated worker privacy boundary first.
from doonex.session_store import (
    LIMITS,
    SessionFileStore,
    SessionStorageError,
    canonical_sha256,
    check_source,
)


def test_round_trip_without_cache_and_exact_canonical_bytes(tmp_path):
    store = SessionFileStore(tmp_path)
    store.write("base_state.json", "abc")
    store.write("events/event-00000-12345678.json", "{}")
    assert store.read("base_state.json") == "abc"
    assert store.list("events") == ["events/event-00000-12345678.json"]
    with store.lock("events/.eventlog.lock"):
        store.write("events/.eventlog-len-1.marker", "")
    assert store.exists("events/.eventlog.lock")
    store.delete("events/.eventlog-len-1.marker")
    store.delete("events/.eventlog-len-1.marker")
    store.close()
    again = SessionFileStore(tmp_path)
    assert again.read("base_state.json") == "abc"
    value = [{"key": "非 ASCII\n\x00", "nested": [False, None, 1, 2.5]}]
    assert (
        canonical_sha256(value)
        == hashlib.sha256(
            json.dumps(
                value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    again.close()


@pytest.mark.parametrize(
    "kind",
    [
        "base_bytes",
        "event_bytes",
        "metadata_bytes",
        "total_bytes",
        "peak_bytes",
        "entries",
        "events",
        "path_bytes",
        "depth",
    ],
)
def test_each_cap_and_latched_failure(tmp_path, kind):
    values = {
        "base_bytes": 4,
        "event_bytes": 4,
        "metadata_bytes": 4,
        "total_bytes": 4,
        "peak_bytes": 4,
        "entries": 1,
        "events": 1,
        "path_bytes": 4,
        "depth": 1,
    }
    limits = dataclasses.replace(LIMITS, **{kind: values[kind]})
    store = SessionFileStore(tmp_path, limits=limits)
    if kind in {"base_bytes", "total_bytes", "peak_bytes"}:
        store.write("base_state.json", "1234")
        attempt = lambda: store.write("base_state.json", "12345")
    elif kind == "event_bytes":
        store.write("events/event-00000-12345678.json", "1234")
        attempt = lambda: store.write("events/event-00001-12345679.json", "12345")
    elif kind == "metadata_bytes":
        store.write("meta", "1234")
        attempt = lambda: store.write("meta", "12345")
    elif kind == "entries":
        store.write("one", "1")
        attempt = lambda: store.write("two", "2")
    elif kind == "events":
        store.write("events/event-00000-12345678.json", "{}")
        attempt = lambda: store.write("events/event-00001-12345679.json", "{}")
    elif kind == "path_bytes":
        store.write("four", "1")
        attempt = lambda: store.write("five!", "1")
    else:
        store.write("one", "1")
        attempt = lambda: store.write("one/two", "1")
    with pytest.raises(SessionStorageError):
        attempt()
    with pytest.raises(SessionStorageError):
        store.write("other", "")
    store.close()


@pytest.mark.parametrize(
    "kind", ["symlink", "fifo", "hardlink", "oversized", "temporary", "invalid_utf8"]
)
def test_inventory_rejects_all_entries_without_modification(tmp_path, kind):
    item = tmp_path / (".doonex-write-stale" if kind == "temporary" else "item")
    if kind == "symlink":
        item.symlink_to("outside")
    elif kind == "fifo":
        os.mkfifo(item)
    elif kind == "hardlink":
        (tmp_path / "source").write_text("content")
        os.link(tmp_path / "source", item)
    else:
        item.write_bytes(
            b"\xff"
            if kind == "invalid_utf8"
            else b"x" * (16385 if kind == "oversized" else 1)
        )
    before = sorted(entry.name for entry in tmp_path.iterdir())
    with pytest.raises(SessionStorageError):
        SessionFileStore(tmp_path)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "change", ["replace", "delete", "add", "directory_delete", "root_replace"]
)
def test_external_change_latches_instead_of_empty_history(tmp_path, change):
    root = tmp_path / "session"
    store = SessionFileStore(root)
    store.write("events/event-00000-12345678.json", "{}")
    if change == "replace":
        other = root / "events/new"
        other.write_text("{}")
        other.replace(root / "events/event-00000-12345678.json")
    elif change == "delete":
        (root / "events/event-00000-12345678.json").unlink()
    elif change == "add":
        (root / "events/unknown").write_text("x")
    elif change == "directory_delete":
        (root / "events/event-00000-12345678.json").unlink()
        (root / "events").rmdir()
    else:
        root.rename(tmp_path / "old")
        root.mkdir()
    with pytest.raises(SessionStorageError):
        store.list("events")
    with pytest.raises(SessionStorageError):
        store.write("base_state.json", "replacement")
    store.close()


def test_partial_write_leaves_evidence_and_blocks_future_writes(tmp_path, monkeypatch):
    store = SessionFileStore(tmp_path)
    store.write("base_state.json", "original")
    original = os.write

    def partial(fd, data):
        original(fd, data[:2])
        raise OSError("synthetic failure")

    monkeypatch.setattr(os, "write", partial)
    with pytest.raises(SessionStorageError):
        store.write("base_state.json", "replacement")
    assert (tmp_path / "base_state.json").read_text() == "original"
    assert len(list(tmp_path.glob(".doonex-write-*"))) == 1
    with pytest.raises(SessionStorageError):
        store.read("base_state.json")
    store.close()
    with pytest.raises(SessionStorageError):
        SessionFileStore(tmp_path)


def test_lock_uses_remaining_deadline_and_keeps_inode(tmp_path, monkeypatch):
    deadline = time.monotonic() + 3
    store = SessionFileStore(tmp_path, deadline=deadline)
    store.write("events/.eventlog.lock", "")
    path = tmp_path / "events/.eventlog.lock"
    inode = path.stat().st_ino
    with path.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = fcntl.flock
        blocked = []

        def observe(descriptor, operation):
            try:
                return original(descriptor, operation)
            except BlockingIOError:
                blocked.append(time.monotonic())
                raise

        monkeypatch.setattr(fcntl, "flock", observe)
        with (
            pytest.raises(SessionStorageError),
            store.lock("events/.eventlog.lock", timeout=30),
        ):
            pytest.fail("held lock admitted")
        assert blocked and blocked[0] < deadline
        assert deadline <= time.monotonic() < deadline + 1
        assert store.deadline == deadline
    assert path.stat().st_ino == inode
    store.close()


def test_source_bounds_work_before_json_serialization():
    check_source({"x": "1234"}, max_bytes=10)
    with pytest.raises(SessionStorageError):
        check_source({"x": "12345678901"}, max_bytes=10)
    with pytest.raises(SessionStorageError):
        check_source([None] * 4097)
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(SessionStorageError):
        check_source(cyclic)
    check_source(sdk.ShellObservation.from_text("valid", exit_code=0))


@pytest.mark.parametrize("payload", [b"a", b"\x00", b"\xff", "中".encode()])
def test_existing_two_mib_shell_contract_fits_exact_event_limit(tmp_path, payload):
    from openhands.sdk.event import ObservationEvent

    raw = (payload * (2 * 1024 * 1024 // len(payload))).decode("utf-8", "replace")
    result = {"output": raw, "exit_code": 0}
    check_source(result)
    observation = sdk.ShellObservation.from_text(
        json.dumps(result, ensure_ascii=False), exit_code=0
    )
    event = ObservationEvent(
        source="agent",
        observation=observation,
        action_id="00000000-0000-0000-0000-000000000001",
        tool_name="doonex_shell",
        tool_call_id="call-1",
    )
    check_source(event)
    text = event.model_dump_json(exclude_none=True)
    store = SessionFileStore(tmp_path)
    store.write("events/event-00000-12345678.json", text)
    assert store.read("events/event-00000-12345678.json") == text
    store.close()


def test_actual_sdk_swallowed_scan_error_cannot_overwrite_or_admit(
    tmp_path, monkeypatch
):
    from test_sdk import harness

    kwargs, reservations, settlements, commands, events = harness(
        tmp_path, should_stop=lambda: True
    )
    assert sdk.run_session(**kwargs) == "paused"
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    receipts = []
    original = SessionFileStore.list

    def broken_list(store, path):
        if path == "events":
            store._fail("Synthetic SDK scan failure")
        return original(store, path)

    monkeypatch.setattr(SessionFileStore, "list", broken_list)
    kwargs.update(
        prompt=None,
        resume=True,
        should_stop=lambda: False,
        scheduling=sdk.SessionScheduling(
            generation=0, should_yield=lambda: False, on_yield=receipts.append
        ),
    )
    events.clear()
    with pytest.raises(SessionStorageError):
        sdk.run_session(**kwargs)
    assert {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    } == before
    assert (
        not reservations
        and not settlements
        and not commands
        and not events
        and not receipts
    )
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


def test_actual_sdk_marker_failure_is_latched_before_model_or_safe_yield(
    tmp_path, monkeypatch
):
    from test_sdk import harness

    original = SessionFileStore.write

    def marker_failure(store, path, contents):
        if path.endswith(".marker"):
            store._fail("Synthetic marker failure")
        return original(store, path, contents)

    monkeypatch.setattr(SessionFileStore, "write", marker_failure)
    receipts, finishes = [], []
    kwargs, reservations, settlements, commands, _ = harness(
        tmp_path,
        scheduling=sdk.SessionScheduling(
            generation=0, should_yield=lambda: True, on_yield=receipts.append
        ),
        on_finish=finishes.append,
    )
    with pytest.raises(SessionStorageError):
        sdk.run_session(**kwargs)
    assert (
        not reservations
        and not settlements
        and not commands
        and not receipts
        and not finishes
    )
    assert list(tmp_path.rglob("event-*.json"))  # The prior append remains evidence.
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


def test_actual_sdk_close_failure_still_removes_callback_references(
    tmp_path, monkeypatch
):
    from test_sdk import harness

    original_close = sdk._M1Conversation.close
    instances = []

    def failed_close(self):
        instances.append(self)
        raise RuntimeError("Synthetic close failure")

    monkeypatch.setattr(sdk._M1Conversation, "close", failed_close)
    kwargs, *_ = harness(tmp_path, should_stop=lambda: True)
    try:
        with pytest.raises(RuntimeError, match="Synthetic close failure"):
            sdk.run_session(**kwargs)
    finally:
        for instance in instances:
            original_close(instance)
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES


def test_actual_sdk_oversized_old_session_rejected_before_construction(
    tmp_path, monkeypatch
):
    from test_sdk import harness

    kwargs, reservations, settlements, commands, _ = harness(
        tmp_path, should_stop=lambda: True
    )
    assert sdk.run_session(**kwargs) == "paused"
    root = next(tmp_path.glob("*/base_state.json")).parent
    extra = root / "unrecognized"
    extra.write_bytes(b"x" * (LIMITS.metadata_bytes + 1))
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized old session reached SDK construction")

    monkeypatch.setattr(sdk._M1Conversation, "__init__", forbidden)
    kwargs.update(prompt=None, resume=True, should_stop=lambda: False)
    with pytest.raises(SessionStorageError):
        sdk.run_session(**kwargs)
    assert before == {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert not reservations and not settlements and not commands


@pytest.mark.parametrize("kind", ["depth", "nodes"])
def test_saved_json_shape_checked_before_sdk_parse(tmp_path, kind):
    text = (
        "[" * 33 + "0" + "]" * 33
        if kind == "depth"
        else "[" + ",".join("0" for _ in range(131074)) + "]"
    )
    (tmp_path / "base_state.json").write_text(text)
    before = (tmp_path / "base_state.json").read_bytes()
    with pytest.raises(SessionStorageError):
        SessionFileStore(tmp_path)
    assert (tmp_path / "base_state.json").read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("hook_config", {"hooks": {}}),
        ("security_analyzer", {"kind": "unverified"}),
        ("secret_registry", {"secret_sources": {"PRIVATE": "unverified"}}),
        ("confirmation_policy", {"kind": "AlwaysConfirm"}),
        ("blocked_actions", {"unverified": "unverified"}),
        ("invoked_skills", ["unverified"]),
        ("new_unverified_extension", None),
    ],
)
def test_saved_inactive_extensions_never_enter_sdk_restore(
    tmp_path, monkeypatch, field, value
):
    from test_sdk import harness

    kwargs, reservations, settlements, commands, _ = harness(
        tmp_path, should_stop=lambda: True
    )
    assert sdk.run_session(**kwargs) == "paused"
    path = next(tmp_path.glob("*/base_state.json"))
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Inactive extension reached SDK construction")

    monkeypatch.setattr(sdk._M1Conversation, "__init__", forbidden)
    kwargs.update(prompt=None, resume=True, should_stop=lambda: False)
    with pytest.raises(ValueError, match="execution policy"):
        sdk.run_session(**kwargs)
    assert path.read_bytes() == before
    assert not reservations and not settlements and not commands


@pytest.mark.parametrize("count", [16, 17])
def test_actual_sdk_tool_batch_capacity_precedes_any_tool_dispatch(
    tmp_path, monkeypatch, count
):
    from test_sdk import answer, harness

    calls, commands, receipts = [], [], []

    def transport(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            return answer("finish", {"message": "done"}, sequence=99)
        response = answer("doonex_shell", {"command": "tool-0"})
        for index in range(1, count):
            response.choices[0].message.tool_calls.extend(
                answer("doonex_shell", {"command": f"tool-{index}"}, sequence=index + 1)
                .choices[0]
                .message.tool_calls
            )
        return response

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    kwargs, reservations, settlements, *_ = harness(
        tmp_path,
        shell=lambda command: commands.append(command) or {"exit_code": 0},
        scheduling=sdk.SessionScheduling(
            generation=0, should_yield=lambda: False, on_yield=receipts.append
        ),
    )
    if count == 16:
        assert sdk.run_session(**kwargs) == "finished"
        assert commands == [f"tool-{index}" for index in range(16)]
        assert len(calls) == len(reservations) == len(settlements) == 2
    else:
        with pytest.raises((SessionStorageError, RuntimeError)):
            sdk.run_session(**kwargs)
        assert commands == []
        assert len(calls) == len(reservations) == len(settlements) == 1
        assert settlements[0][1] == {"input_tokens": 100, "output_tokens": 20}
    assert receipts == []
    assert not sdk._CALLBACKS and not sdk._SCHEDULING and not sdk._SESSION_STORES
