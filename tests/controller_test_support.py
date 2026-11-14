"""Explicit real-PostgreSQL controller actor for tests that run product leaves.

Import the fixture only in the test modules that need it. The existing Store
fixture owns its disposable schema; this helper never reads product Settings,
replaces the registry, clears unknown records or adds a production opt-out.
"""

import pytest
from doonex import controller_operations as co
from doonex.store import external_execution_fenced


@pytest.fixture
def controller_registry(store):
    registry = co.ControllerRegistry(
        store,
        actor=co.fresh_actor("cli"),
        check_owner=lambda: None,
        check_other_fences=external_execution_fenced,
    )
    with co.bind_registry(registry):
        yield registry
