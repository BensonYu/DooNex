"""Run the complete local suite against a disposable PostgreSQL instance."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

IMAGE = "postgres:18.1-alpine@sha256:aa6eb304ddb6dd26df23d05db4e5cb05af8951cda3e0dc57731b771e0ef4ab29"


def main():
    name = "doonex-test-" + uuid.uuid4().hex
    password = secrets.token_hex(24)
    environment = os.environ.copy()
    environment["POSTGRES_PASSWORD"] = password
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            "doonex.test=true",
            "-e",
            "POSTGRES_USER=doonex",
            "-e",
            "POSTGRES_DB=doonex_test",
            "-e",
            "POSTGRES_PASSWORD",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        ],
        env=environment,
        check=True,
        capture_output=True,
    )
    try:
        for _ in range(60):
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-U",
                    "doonex",
                    "-d",
                    "doonex_test",
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        port = (
            subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True)
            .strip()
            .split(":")[-1]
        )
        with tempfile.TemporaryDirectory(prefix="doonex-test-") as temporary:
            config = Path(temporary) / "database.json"
            config.write_text(
                json.dumps(
                    {
                        "database_url": f"postgresql://doonex:{password}@127.0.0.1:{port}/doonex_test"
                    }
                )
            )
            config.chmod(0o600)
            environment.pop("POSTGRES_PASSWORD", None)
            environment["DOONEX_TEST_CONFIG"] = str(config)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *sys.argv[1:]],
                env=environment,
                check=False,
            )
            return result.returncode
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
