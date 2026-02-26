from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    def load_dotenv() -> None:
        return None

from db import DB_PATH, ENCRYPTION_KEY_ENV, init_db, rotate_encryption_key


def _validate_fernet_key(key: str, label: str) -> None:
    try:
        Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise ValueError(f"{label} is not a valid Fernet key.") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate encrypted DB data from OLD_TOKENS_ENCRYPTION_KEY to TOKENS_ENCRYPTION_KEY."
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Skip creating a timestamped DB backup before rotation.",
    )
    args = parser.parse_args()

    load_dotenv()
    init_db()

    old_key = os.getenv("OLD_TOKENS_ENCRYPTION_KEY", "").strip()
    new_key = os.getenv(ENCRYPTION_KEY_ENV, "").strip()
    if not old_key:
        raise RuntimeError("Missing OLD_TOKENS_ENCRYPTION_KEY in environment.")
    if not new_key:
        raise RuntimeError(f"Missing {ENCRYPTION_KEY_ENV} in environment.")
    if old_key == new_key:
        raise RuntimeError("Old and new keys are identical. Rotation is unnecessary.")

    _validate_fernet_key(old_key, "OLD_TOKENS_ENCRYPTION_KEY")
    _validate_fernet_key(new_key, ENCRYPTION_KEY_ENV)

    backup_path: Path | None = None
    if not args.skip_backup:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = DB_PATH.with_name(f"{DB_PATH.stem}.backup.{timestamp}{DB_PATH.suffix}")
        shutil.copy2(DB_PATH, backup_path)

    result = rotate_encryption_key(old_key=old_key, new_key=new_key)
    print("Rotation complete.")
    print(f"bank_connections_rotated: {result['bank_connections_rotated']}")
    print(f"sensitive_fields_rotated: {result['sensitive_fields_rotated']}")
    if backup_path:
        print(f"backup_created: {backup_path}")


if __name__ == "__main__":
    main()
