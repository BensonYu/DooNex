"""Private local service configuration; never copied into task containers."""

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from doonex.deadline import check_deadline
from doonex.models import ModelConfig

MAX_CREDENTIAL_BYTES = 1024 * 1024


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

    def credential(self, model: dict | None = None, *, deadline=None) -> str | None:
        check_deadline(deadline)
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
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            information = os.fstat(fd)
            if not stat.S_ISREG(information.st_mode):
                raise ValueError("凭据文件必须是普通文件")
            if information.st_mode & 0o077:
                raise ValueError("凭据文件权限必须是 0600")
            if information.st_size > MAX_CREDENTIAL_BYTES:
                raise ValueError("凭据文件超过 1 MiB 限额")
            body = bytearray()
            while True:
                check_deadline(deadline)
                chunk = os.read(
                    fd, min(64 * 1024, MAX_CREDENTIAL_BYTES + 1 - len(body))
                )
                check_deadline(deadline)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_CREDENTIAL_BYTES:
                    raise ValueError("凭据文件超过 1 MiB 限额")
        finally:
            os.close(fd)
        try:
            document = json.loads(body)
        except (ValueError, UnicodeError):
            raise ValueError("凭据文件 JSON 格式无效") from None
        check_deadline(deadline)
        if type(document) is not dict or (
            document.get(name) is not None and type(document[name]) is not str
        ):
            raise ValueError("凭据值必须是字符串或 null")
        return document.get(name)


def redact(value, secrets_to_hide=()):
    """Defense in depth; task processes never receive these secrets."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(
                json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]"
            )
    return json.loads(text)
