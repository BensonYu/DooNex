"""Bounded private credential loading; every value here is synthetic."""

import json
import os
import time

import pytest
from doonex import config
from doonex.config import MAX_CREDENTIAL_BYTES, Settings
from doonex.deadline import ExecutionDeadlineExceeded


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.delenv("DOONEX_LLM_API_KEY", raising=False)
    return Settings(tmp_path, "unused", "synthetic-token", None)


def write(settings, value):
    path = settings.home / "credentials.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_environment_and_private_file_retain_existing_values(private, monkeypatch):
    assert private.credential() is None
    write(private, {"DOONEX_LLM_API_KEY": "synthetic-private-key"})
    assert private.credential(deadline=time.monotonic() + 5) == "synthetic-private-key"
    monkeypatch.setenv("DOONEX_LLM_API_KEY", "synthetic-environment-key")
    assert private.credential() == "synthetic-environment-key"


@pytest.mark.parametrize("value", [[], {"DOONEX_LLM_API_KEY": 123}])
def test_invalid_value_has_no_content_in_error(private, value):
    write(private, value)
    with pytest.raises(ValueError, match="字符串或 null"):
        private.credential()


@pytest.mark.parametrize("kind", ["permissions", "symlink", "fifo", "large", "json"])
def test_unsafe_or_unbounded_file_is_rejected(private, kind):
    path = private.home / "credentials.json"
    if kind == "symlink":
        target = private.home / "target"
        target.write_text("synthetic-private-sentinel")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(
            b" " * (MAX_CREDENTIAL_BYTES + 1)
            if kind == "large"
            else b"synthetic-private-sentinel"
        )
        path.chmod(0o644 if kind == "permissions" else 0o600)
    with pytest.raises(ValueError) as caught:
        private.credential(deadline=time.monotonic() + 5)
    assert "synthetic-private-sentinel" not in str(caught.value)


def test_expired_credentials_never_open_file(private, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Expired loader opened a credential file")

    monkeypatch.setattr(config.os, "open", forbidden)
    with pytest.raises(ExecutionDeadlineExceeded):
        private.credential(deadline=time.monotonic() - 1)


def test_expiry_between_chunks_closes_the_original_file(private, monkeypatch):
    write(private, {"DOONEX_LLM_API_KEY": "s" * 100_000})
    original_read = config.os.read
    original_close = config.os.close
    observed, closed = [], []

    def read(fd, size):
        observed.append((fd, size))
        return original_read(fd, size)

    def check(deadline):
        if observed:
            raise ExecutionDeadlineExceeded("Synthetic expiry after first chunk")

    def close(fd):
        closed.append(fd)
        return original_close(fd)

    monkeypatch.setattr(config.os, "read", read)
    monkeypatch.setattr(config.os, "close", close)
    monkeypatch.setattr(config, "check_deadline", check)
    with pytest.raises(ExecutionDeadlineExceeded):
        private.credential(deadline=time.monotonic() + 5)
    assert len(observed) == 1 and observed[0][1] == 64 * 1024
    assert closed == [observed[0][0]]
