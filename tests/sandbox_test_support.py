"""Real PostgreSQL-backed Run/capability bindings for Sandbox subprocess tests."""

import hashlib
import time
from dataclasses import dataclass

import pytest
from doonex import controller_operations as co
from doonex.sandbox import _Cleanup, _evidence, _Supervisor
from doonex.store import external_execution_fenced


@dataclass
class SandboxTestContext:
    store: object
    registry: co.ControllerRegistry
    run_id: str
    project_id: str
    expected_owner: str | None
    action_id: str

    @property
    def binding(self):
        return {
            "project_id": self.project_id,
            "expected_run_owner": self.expected_owner,
            "parent_action_id": self.action_id,
        }

    @property
    def cleanup_binding(self):
        return {
            key: value
            for key, value in self.binding.items()
            if key != "parent_action_id"
        }

    def records(self):
        with self.store.connect() as connection:
            return [
                row["data"]
                for row in connection.execute(
                    "SELECT data FROM controller_operations ORDER BY id"
                ).fetchall()
            ]

    def fenced(self):
        with self.store.connect() as connection:
            return co.fenced(connection)

    def supervisor(self, *, container_name="synthetic", seconds=10):
        """A real saved Run/action grant; callers retain all unknown receipts."""
        operation = self.registry.reserve(
            "sandbox_execute",
            scope=co.scope(
                intent_digest=hashlib.sha256(b"Sandbox test subprocess").hexdigest(),
                container_name=container_name,
                run_label="doonex.run_id=" + self.run_id,
            ),
            run_id=self.run_id,
            deadline_monotonic_ns=time.monotonic_ns() + int(seconds * 1_000_000_000),
            cleanup_allowance_seconds=15,
            **self.binding,
        )
        capability = operation.launch()
        return operation, _Supervisor(_evidence(), _Cleanup(), capability=capability)


@pytest.fixture
def sandbox_context(store, request_data):
    run = store.create_run(request_data)
    # Use exactly the saved Run request, including a genuine nullable queued owner.
    saved = store.get(run["id"])
    registry = co.ControllerRegistry(
        store,
        actor=co.fresh_actor("api"),
        check_owner=lambda: None,
        check_other_fences=external_execution_fenced,
    )
    context = SandboxTestContext(
        store,
        registry,
        run["id"],
        saved["request"]["project_id"],
        saved.get("owner"),
        store.action(run["id"], "shell", {"command": "controlled Sandbox test"}),
    )
    with co.bind_registry(registry):
        yield context
