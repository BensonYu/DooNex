import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from doonex.config import Settings
from doonex.models import ModelConfig
from doonex.store import Store
from psycopg.conninfo import make_conninfo


@pytest.fixture
def store():
    config = os.environ.get("DOONEX_TEST_CONFIG")
    if not config:
        pytest.skip("Set DOONEX_TEST_CONFIG to a private test database configuration")
    dsn = json.loads(Path(config).read_text())["database_url"]
    schema = "test_" + uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema))
        )
    result = Store(make_conninfo(dsn, options=f"-c search_path={schema}"))
    result.initialize()
    try:
        yield result
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(schema)
                )
            )


@pytest.fixture
def model():
    # Deliberately synthetic prices/model, never a live provider configuration.
    return ModelConfig(
        provider="openai",
        model_id="openai/doonex-offline-test",
        max_input_tokens=32768,
        max_output_tokens=1024,
        input_price_per_million=1,
        output_price_per_million=2,
        price_source="synthetic offline test fixture",
    )


@pytest.fixture
def settings(store, model, tmp_path, monkeypatch):
    monkeypatch.setenv("DOONEX_LLM_API_KEY", "fake-key-for-offline-tests-only")
    return Settings(
        tmp_path / "private", store.database_url, "test-access-token-" * 3, model
    )


@pytest.fixture
def request_data(model, tmp_path):
    return {
        "project_id": str(uuid4()),
        "repo_path": str(tmp_path / "repo"),
        "base_commit": "0" * 40,
        "prompt": "Fix the controlled sample",
        "checks": ["python -m unittest"],
        "acceptance_version": 1,
        "model": model.model_dump(),
        "budget": {
            "max_calls": 2,
            "max_tokens": 100_000,
            "max_cost_usd": 1,
            "max_seconds": 600,
        },
    }
