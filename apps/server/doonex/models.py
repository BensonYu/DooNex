"""Immutable run inputs. No credentials belong in these models."""

from ipaddress import ip_address, ip_network
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Budget(StrictModel):
    max_calls: int = Field(default=10, ge=1, le=100)
    max_tokens: int = Field(default=100_000, ge=1, le=10_000_000)
    max_cost_usd: float | None = Field(default=2, gt=0, le=100)
    max_seconds: int = Field(default=600, ge=1, le=7200)


class ModelConfig(StrictModel):
    provider: Literal["openai", "anthropic"]
    model_id: str = Field(min_length=1, max_length=200)
    api_mode: Literal["chat", "responses"] = "chat"
    credential_env: str = Field(
        default="DOONEX_LLM_API_KEY", pattern=r"^[A-Z][A-Z0-9_]{0,100}$"
    )
    base_url: str | None = Field(default=None, max_length=4096)
    allow_private_http: bool = False
    billing_mode: Literal["metered", "subscription"] = "metered"
    max_input_tokens: int = Field(default=32_768, ge=16_384, le=200_000)
    max_output_tokens: int = Field(default=2048, ge=128, le=16_384)
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    input_price_per_million: float | None = Field(default=None, gt=0)
    output_price_per_million: float | None = Field(default=None, gt=0)
    price_source: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_endpoint(self):
        if self.provider != "openai" and self.api_mode == "responses":
            raise ValueError("Responses 接口仅支持 OpenAI 兼容配置")
        if self.base_url:
            url = urlparse(self.base_url)
            if not url.hostname or any(char.isspace() for char in self.base_url):
                raise ValueError("模型地址必须包含有效主机名且不能含空白")
            try:
                _ = url.port
            except ValueError:
                raise ValueError("模型地址端口无效") from None
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("模型地址不能包含凭据、查询或片段")
            private = False
            try:
                address = ip_address(url.hostname or "")
                private = address.is_private or address in ip_network("100.64.0.0/10")
            except ValueError:
                pass
            if url.scheme != "https" and not (
                url.scheme == "http"
                and (
                    url.hostname in {"127.0.0.1", "localhost", "::1"}
                    or (private and self.allow_private_http)
                )
            ):
                raise ValueError("远程模型地址须用 HTTPS；已授权的私网 HTTP 须显式启用")
        return self


class RunInput(StrictModel):
    recommendation_id: str | None = Field(default=None, min_length=36, max_length=36)
    skill_version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    project_id: str | None = Field(default=None, min_length=36, max_length=36)
    model_profile_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    reviewer_model_profile_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    workflow: Literal["execute", "execute_review"] = "execute"
    prompt: str = Field(min_length=1, max_length=20_000)
    base_ref: str = Field(
        default="HEAD", min_length=1, max_length=200, pattern=r"^[^\s\x00]+$"
    )
    checks: list[str] = Field(default_factory=list, max_length=20)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def validate_checks(self):
        if any(
            not command.strip() or len(command) > 8000 or "\x00" in command
            for command in self.checks
        ):
            raise ValueError("检查命令不能为空、过长或含 NUL")
        return self


class ProjectInput(StrictModel):
    path: str = Field(min_length=1, max_length=4096)


class ResumeInput(StrictModel):
    acknowledge_unknown: bool = False


class ModelProfileInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    model: ModelConfig
    usage_adapter: Literal["none", "gateway_v1"] = "none"
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def model_for_role(request: dict, role: str | None = None) -> dict:
    """Frozen role settings; historical single-model runs need no migration."""
    return request.get("role_models", {}).get(role or "executor", request["model"])


def run_roles(request: dict) -> tuple[str, ...]:
    return (
        ("executor", "reviewer")
        if request.get("workflow") == "execute_review"
        else ("executor",)
    )
