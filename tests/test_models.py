import pytest
from doonex.config import redact
from doonex.models import Budget, ModelConfig, RunInput
from pydantic import ValidationError


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_cost_rejected(value):
    with pytest.raises(ValidationError):
        Budget(max_cost_usd=value)


def test_no_implicit_api_mode_or_credentials_in_url():
    with pytest.raises(ValidationError):
        ModelConfig(provider="anthropic", model_id="test", api_mode="responses")
    with pytest.raises(ValidationError):
        ModelConfig(
            provider="openai",
            model_id="test",
            base_url="https://name:secret@example.invalid",
        )
    with pytest.raises(ValidationError):
        RunInput(prompt="test", checks=[""])


def test_secret_redaction_preserves_json_types():
    assert redact(
        {"message": 'error SECRET"marker', "count": 1}, ['SECRET"marker']
    ) == {"message": "error [REDACTED]", "count": 1}
