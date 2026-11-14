"""Offline deadline propagation probes for immutable content verification."""

import time

import pytest
from doonex import tasksets
from doonex.deadline import ExecutionDeadlineExceeded
from doonex.skills import _json, _sha


def test_taskset_snapshot_digest_unchanged_with_deadline(tmp_path):
    (tmp_path / "hello.py").write_text("print('hello')\n")
    (tmp_path / "empty").mkdir()
    deadline = time.monotonic() + 10
    plain, files = tasksets._snapshot(tmp_path, "source")
    bounded, bounded_files = tasksets._snapshot(tmp_path, "source", deadline=deadline)
    assert files == bounded_files and plain == bounded
    assert bounded["source_digest"] == _sha(
        _json(
            {
                "files": bounded["source_files"],
                "directories": bounded["source_directories"],
            }
        )
    )


def test_taskset_snapshot_rejects_expired_deadline_before_read(tmp_path, monkeypatch):
    def unexpected(*arguments, **kwargs):
        pytest.fail("Expired snapshot read content")

    monkeypatch.setattr(tasksets, "read_files", unexpected)
    with pytest.raises(ExecutionDeadlineExceeded):
        tasksets._snapshot(tmp_path, "source", deadline=time.monotonic() - 1)


def test_taskset_snapshot_passes_one_deadline_to_each_stage(tmp_path, monkeypatch):
    (tmp_path / "hello.py").write_text("print('hello')\n")
    deadline = time.monotonic() + 10
    seen = []
    for name in ["read_files", "_tree", "manifest", "_json_digest"]:
        original = getattr(tasksets, name)

        def tracked(*arguments, _original=original, _name=name, **kwargs):
            seen.append((_name, kwargs.get("deadline")))
            return _original(*arguments, **kwargs)

        monkeypatch.setattr(tasksets, name, tracked)
    tasksets._snapshot(tmp_path, "source", deadline=deadline)
    assert seen == [
        (name, deadline) for name in ["read_files", "_tree", "manifest", "_json_digest"]
    ]
