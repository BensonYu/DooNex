"""Private model profiles and explicit, bounded gateway metadata reads.

Model lists are candidates, never proof of tool/API compatibility. Account
metadata stays separate from Run accounting and never changes a budget ledger.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import Field, ValidationError, field_validator

from doonex.models import ModelConfig, StrictModel

MAX_PROFILES = 64
MAX_CONFIG_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
METRIC_FIELDS = frozenset(
    {
        "requests",
        "tokens",
        "cost",
        "today_requests",
        "today_tokens",
        "today_cost",
        "total_requests",
        "total_tokens",
        "total_cost",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "rpm",
        "tpm",
        "rpm_limit",
        "tpm_limit",
    }
)


class RegistryError(ValueError):
    """A safe configuration error without user-supplied values."""


class ProfileConflict(RegistryError):
    """The supplied profile revision no longer matches the stored profile."""


class _ProfileSpec(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    model: ModelConfig
    usage_adapter: Literal["none", "gateway_v1"] = "none"

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if not value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("Invalid profile name")
        return value


class ModelProfile(_ProfileSpec):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    read_only: bool = False


def _json(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()


def _profile(spec, *, read_only=False):
    payload = spec.model_dump()
    return ModelProfile(
        **payload,
        revision=hashlib.sha256(_json(payload)).hexdigest(),
        read_only=read_only,
    )


def _regular_private(descriptor):
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise RegistryError("模型配置文件必须是独立的 0600 普通文件")


class ModelRegistry:
    def __init__(self, settings):
        self.settings = settings
        self.home = Path(settings.home)

    def _read(self, directory):
        try:
            descriptor = os.open(
                "profiles.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return []
        try:
            _regular_private(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                raw = source.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise RegistryError("模型配置文件超过大小上限")
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or set(data) != {"schema_version", "profiles"}
                or type(data["schema_version"]) is not int
                or data["schema_version"] != 1
            ):
                raise RegistryError("不支持的模型配置文件版本")
            profiles = data["profiles"]
            if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
                raise RegistryError("模型配置档数量无效")
            result, ids = [], set()
            for entry in profiles:
                profile = ModelProfile.model_validate(entry)
                spec = _ProfileSpec.model_validate(
                    profile.model_dump(exclude={"revision", "read_only"})
                )
                if (
                    profile.id == "legacy"
                    or profile.id in ids
                    or profile.read_only
                    or profile.revision != _profile(spec).revision
                ):
                    raise RegistryError("模型配置档标识或版本无效")
                ids.add(profile.id)
                result.append(profile)
            return result
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            if isinstance(error, RegistryError):
                raise
            raise RegistryError("模型配置文件格式无效") from None
        finally:
            os.close(descriptor)

    def list(self) -> list[ModelProfile]:
        profiles = []
        try:
            directory = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            directory = None
        except OSError:
            raise RegistryError("模型配置目录不可安全读取") from None
        if directory is not None:
            try:
                profiles = self._read(directory)
            except OSError:
                raise RegistryError("模型配置文件不可安全读取") from None
            finally:
                os.close(directory)
        if self.settings.model is not None:
            profiles.insert(
                0,
                _profile(
                    _ProfileSpec(
                        id="legacy", name="默认模型", model=self.settings.model
                    ),
                    read_only=True,
                ),
            )
        return profiles

    def get(self, profile_id: str) -> ModelProfile:
        for profile in self.list():
            if profile.id == profile_id:
                return profile
        raise KeyError("找不到模型配置档")

    def save(
        self, profile_id, name, model, *, usage_adapter="none", expected_revision=None
    ) -> ModelProfile:
        try:
            spec = _ProfileSpec(
                id=profile_id, name=name, model=model, usage_adapter=usage_adapter
            )
        except ValidationError:
            raise RegistryError("模型配置不符合已支持的字段和限制") from None
        if spec.id == "legacy":
            raise RegistryError("默认配置档只读，请复制为新的配置档")
        profile = _profile(spec)
        try:
            self.home.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise RegistryError("模型配置目录不可安全写入") from None
        lock = None
        temporary = None
        try:
            lock = os.open(
                ".profiles.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            _regular_private(lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            profiles = self._read(directory)
            existing = next((item for item in profiles if item.id == spec.id), None)
            if (existing and expected_revision != existing.revision) or (
                not existing and expected_revision is not None
            ):
                raise ProfileConflict("模型配置档已变化，请重新读取后保存")
            profiles = [profile if item.id == profile.id else item for item in profiles]
            if existing is None:
                if len(profiles) >= MAX_PROFILES:
                    raise RegistryError("模型配置档数量已达上限")
                profiles.append(profile)
            payload = (
                _json(
                    {
                        "schema_version": 1,
                        "profiles": [item.model_dump() for item in profiles],
                    }
                )
                + b"\n"
            )
            if len(payload) > MAX_CONFIG_BYTES:
                raise RegistryError("模型配置文件超过大小上限")
            temporary = ".profiles-" + secrets.token_hex(16)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(
                temporary, "profiles.json", src_dir_fd=directory, dst_dir_fd=directory
            )
            temporary = None
            os.fsync(directory)
            return profile
        except OSError:
            raise RegistryError("模型配置文件不可安全保存") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            if lock is not None:
                os.close(lock)
            os.close(directory)


def _number(value):
    if (
        type(value) not in (int, float)
        or value < 0
        or value > 1e18
        or not math.isfinite(value)
    ):
        raise ValueError("Invalid metadata number")
    return value


def _text(value, maximum=256):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid metadata text")
    return value


def _metric_name(value):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _usage(data):
    if not isinstance(data, dict) or not set(data) & {
        "isValid",
        "mode",
        "planName",
        "remaining",
        "unit",
        "usage",
        "windows",
    }:
        raise ValueError("Unsupported usage response")
    account_valid = data.get("isValid")
    if account_valid is not None and type(account_valid) is not bool:
        raise ValueError("Invalid account validity")
    result = {
        "is_valid": account_valid,
        "mode": _text(data["mode"], 64) if data.get("mode") is not None else None,
        "plan_name": _text(data["planName"])
        if data.get("planName") is not None
        else None,
        "remaining": _number(data["remaining"])
        if data.get("remaining") is not None
        else None,
        "unit": _text(data["unit"], 32) if data.get("unit") is not None else None,
        "unrestricted": data.get("mode") == "unrestricted",
        "metrics": {},
        "windows": [],
    }
    metrics = data.get("usage", {})
    if not isinstance(metrics, dict) or len(metrics) > 100:
        raise ValueError("Invalid usage metrics")
    for key, value in metrics.items():
        normalized = _metric_name(key)
        if normalized in METRIC_FIELDS and value is not None:
            if normalized in result["metrics"]:
                raise ValueError("Ambiguous usage metric aliases")
            result["metrics"][normalized] = _number(value)
    windows = data.get("windows", [])
    if not isinstance(windows, list) or len(windows) > 24:
        raise ValueError("Invalid usage windows")
    for window in windows:
        if not isinstance(window, dict):
            raise TypeError("Invalid usage window")
        item = {"name": _text(window.get("name"), 100)}
        for key in ("used", "limit", "remaining"):
            item[key] = _number(window[key]) if window.get(key) is not None else None
        item["unit"] = (
            _text(window["unit"], 32) if window.get("unit") is not None else None
        )
        reset = window.get("reset_at")
        if reset is not None:
            _text(reset, 100)
            parsed = datetime.fromisoformat(reset)
            if parsed.tzinfo is None:
                raise ValueError("Usage window needs an explicit timezone")
            reset = parsed.astimezone(UTC).isoformat()
        item["reset_at"] = reset
        result["windows"].append(item)
    return result


def _origin(model):
    model = ModelConfig.model_validate(model.model_dump())
    if model.provider != "openai" or not model.base_url:
        return None
    parsed = urlsplit(model.base_url)
    if (
        not parsed.hostname
        or parsed.path not in {"", "/", "/v1", "/v1/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    # URL canonicalization validates the port and rejects malformed authorities.
    url = httpx.URL(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
    if url.scheme not in {"http", "https"}:
        return None
    return str(url).rstrip("/")


def _redact(value, hidden):
    if isinstance(value, str):
        for secret in hidden:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, hidden) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, hidden) for key, item in value.items()}
    return value


def _reject_constant(_):
    raise ValueError("Non-finite JSON number")


class MetadataClient:
    def __init__(
        self, settings, *, transport=None, timeout=10.0, max_bytes=MAX_RESPONSE_BYTES
    ):
        if not 0 < timeout <= 30 or not 1 <= max_bytes <= 2 * MAX_RESPONSE_BYTES:
            raise ValueError("Metadata request bounds are invalid")
        self.settings, self.transport = settings, transport
        self.timeout, self.max_bytes = timeout, max_bytes

    def discover(self, profile: ModelProfile) -> dict:
        return self._query(profile, "models")

    def usage(self, profile: ModelProfile) -> dict:
        return self._query(profile, "usage")

    def _query(self, profile, endpoint):
        result = {
            "status": "not_configured",
            "profile_id": profile.id,
            "revision": profile.revision,
            "source": None,
            "queried_at": datetime.now(UTC).isoformat(),
            "http_status": None,
            "error": None,
            **({"models": []} if endpoint == "models" else {"usage": None}),
        }
        hidden = (self.settings.token, self.settings.database_url)

        def fail(status, message):
            result.update(status=status, error=message)
            return _redact(result, hidden)

        if endpoint == "usage" and profile.usage_adapter == "none":
            return fail("not_configured", "尚未启用账户用量查询")
        try:
            origin = _origin(profile.model)
        except (ValueError, httpx.InvalidURL):
            origin = None
        if origin is None:
            return fail("unsupported", "此配置不支持固定同源的网关元数据接口")
        result["source"] = origin + "/v1/" + endpoint
        try:
            key = self.settings.credential(profile.model.model_dump())
        except Exception:  # noqa: BLE001 - configuration failures must not reveal secrets
            return fail("not_configured", "无法安全读取此配置档的凭据引用")
        if not key:
            return fail("not_configured", "此配置档缺少可用凭据")
        hidden = (*hidden, key)
        deadline = time.monotonic() + self.timeout
        try:
            with (
                httpx.Client(
                    transport=self.transport,
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "GET",
                    result["source"],
                    headers={
                        "Authorization": "Bearer " + key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response,
            ):
                status = response.status_code
                result["http_status"] = status
                if status in {401, 403}:
                    return fail("unauthorized", "元数据接口拒绝此凭据")
                if status == 429:
                    return fail("rate_limited", "元数据接口暂时限流，请稍后刷新")
                if status in {404, 405, 501}:
                    return fail("unsupported", "供应商未提供此固定元数据接口")
                if not 200 <= status < 300:
                    return fail("unavailable", "元数据接口暂时不可用或要求重定向")
                if (
                    response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    return fail("invalid_response", "元数据响应编码不受支持")
                content_length = response.headers.get("content-length")
                if content_length is not None and (
                    not content_length.isdigit() or int(content_length) > self.max_bytes
                ):
                    return fail("invalid_response", "元数据响应超过大小上限或长度无效")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise httpx.ReadTimeout("Metadata body deadline exceeded")
                    payload.extend(chunk)
                    if len(payload) > self.max_bytes:
                        return fail("invalid_response", "元数据响应超过大小上限")
            data = json.loads(payload, parse_constant=_reject_constant)
            if endpoint == "models":
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("data"), list)
                    or len(data["data"]) > 1000
                ):
                    raise ValueError("Invalid model list")
                ids = [
                    _text(item.get("id"), 200)
                    for item in data["data"]
                    if isinstance(item, dict)
                ]
                if len(ids) != len(data["data"]):
                    raise ValueError("Invalid model item")
                if any(secret in item for item in ids for secret in hidden if secret):
                    raise ValueError("Model identifiers echoed a credential")
                result["models"] = list(dict.fromkeys(ids))
            else:
                result["usage"] = _usage(data)
            result["status"] = "ok"
            return _redact(result, hidden)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return fail("invalid_response", "元数据响应不符合受支持的 JSON 字段和限制")
        except Exception:  # noqa: BLE001 - HTTP exceptions can contain credentials and response bodies
            return fail("unavailable", "元数据请求失败或超时")
