"""Create local service secrets without printing them or storing them in Git."""

import argparse
import json
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="~/Library/Application Support/DooNex")
    args = parser.parse_args()
    os.umask(0o077)
    directory = Path(args.data_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = [directory / "service.json", directory / "compose.env"]
    if any(path.exists() for path in files):
        raise SystemExit(
            "Local configuration already exists; preserved without changes."
        )
    password = secrets.token_hex(32)
    (directory / "compose.env").write_text("DOONEX_DB_PASSWORD=" + password + "\n")
    (directory / "service.json").write_text(
        json.dumps(
            {
                "database_url": "postgresql://doonex:"
                + password
                + "@127.0.0.1:55432/doonex"
            }
        )
    )
    for path in files:
        path.chmod(0o600)
    print("Private service configuration created in:", directory)
    print("No model credential or API budget is inferred by setup.")


if __name__ == "__main__":
    main()
