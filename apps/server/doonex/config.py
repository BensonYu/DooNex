"""Private local service configuration; never copied into task containers."""

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from doonex.models import ModelConfig


@dataclass(frozen=True)
class Settings:
    home: Path
    database_url: str
    token: str
    model: ModelConfig | None

    @classmethod
    def load(cls):
        os.umask(0o077)
        home = (
            Path(os.environ.get("DOONEX_HOME", "~/Library/Application Support/DooNex"))
            .expanduser()
            .resolve()
        )
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        token_path = home / "access-token"
        if token_path.is_symlink():
            raise ValueError("访问令牌不能是符号链接")
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
        token = token_path.read_text().strip()
        if len(token) < 32:
            raise ValueError("本地访问令牌长度不足")
        model_path = Path(
            os.environ.get("DOONEX_MODEL_CONFIG", str(home / "model.json"))
        ).expanduser()
        model = (
            ModelConfig.model_validate_json(model_path.read_text())
            if model_path.is_file()
            else None
        )
        database_url = os.environ.get("DOONEX_DATABASE_URL")
        service_path = home / "service.json"
        if (
            not database_url
            and service_path.is_file()
            and not service_path.is_symlink()
        ):
            if service_path.stat().st_mode & 0o077:
                raise ValueError("服务配置权限必须是 0600")
            database_url = json.loads(service_path.read_text()).get("database_url")
        if not database_url:
            raise ValueError("请设置 DOONEX_DATABASE_URL；参见 README 本地启动说明")
        return cls(home, database_url, token, model)

    def credential(self, model: dict | None = None) -> str | None:
        name = (
            model["credential_env"]
            if model
            else (self.model.credential_env if self.model else "DOONEX_LLM_API_KEY")
        )
        if os.environ.get(name):
            return os.environ[name]
        path = self.home / "credentials.json"
        if path.is_symlink():
            raise ValueError("凭据文件不能是符号链接")
        if path.is_file():
            if path.stat().st_mode & 0o077:
                raise ValueError("凭据文件权限必须是 0600")
            return json.loads(path.read_text()).get(name)
        return None


def redact(value, secrets_to_hide=()):
    """Defense in depth; task processes never receive these secrets."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(
                json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]"
            )
    return json.loads(text)
