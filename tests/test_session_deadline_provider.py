"""A live execution bound can tighten, never renew, an existing SDK store."""

import time

import pytest
from doonex.session_store import SessionFileStore, SessionStorageError


def test_provider_tightens_and_cannot_renew_deadline(tmp_path):
    original = time.monotonic() + 60
    bounds = [original]
    with SessionFileStore(
        tmp_path, deadline=original, deadline_provider=lambda: bounds[0]
    ) as store:
        store.write("sidecar", "original")
        bounds[0] = original - 30
        store.check_ok()
        assert store.deadline == original - 30
        bounds[0] = original + 100
        store.check_ok()
        assert store.deadline == original - 30
        assert store.read("sidecar") == "original"


@pytest.mark.parametrize("invalid", [None, float("nan"), "60", "expired"])
def test_invalid_or_expired_provider_latches_without_overwriting(tmp_path, invalid):
    bounds = [time.monotonic() + 60]
    with SessionFileStore(
        tmp_path, deadline=bounds[0], deadline_provider=lambda: bounds[0]
    ) as store:
        store.write("sidecar", "original")
        bounds[0] = time.monotonic() - 1 if invalid == "expired" else invalid
        with pytest.raises(SessionStorageError, match="deadline"):
            store.write("sidecar", "must not replace original")
        bounds[0] = time.monotonic() + 60
        with pytest.raises(SessionStorageError, match="deadline"):
            store.write("sidecar", "must not recover silently")
    assert (tmp_path / "sidecar").read_text() == "original"
