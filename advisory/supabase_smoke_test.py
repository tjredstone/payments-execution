from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    def load_dotenv() -> None:
        return None

from db import get_conn, init_db


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_smoke_test() -> dict[str, object]:
    load_dotenv()
    init_db()

    db_url = os.getenv("DATABASE_URL", "")
    ssl_required = "sslmode=require" in db_url

    test_user_id = f"smoke-{uuid.uuid4()}"
    result: dict[str, object] = {
        "generated_at": _utc_now_iso(),
        "checks": [],
        "summary": {"passed": 0, "failed": 0},
    }

    def pass_check(name: str, detail: str) -> None:
        result["checks"].append({"check": name, "status": "ok", "detail": detail})  # type: ignore[index]
        result["summary"]["passed"] += 1  # type: ignore[index]

    def fail_check(name: str, detail: str) -> None:
        result["checks"].append({"check": name, "status": "fail", "detail": detail})  # type: ignore[index]
        result["summary"]["failed"] += 1  # type: ignore[index]

    if ssl_required:
        pass_check("database_url_ssl", "DATABASE_URL includes sslmode=require.")
    else:
        fail_check("database_url_ssl", "DATABASE_URL missing sslmode=require.")

    with get_conn() as conn:
        try:
            row = conn.execute("SELECT current_database() AS db_name, current_schema() AS schema_name").fetchone()
            pass_check(
                "connectivity",
                f"Connected to db={row['db_name']} schema={row['schema_name']}.",
            )
        except Exception as exc:
            fail_check("connectivity", f"Could not query database metadata: {exc}")
            return result

        # Safe write/read/delete cycle on existing tables.
        try:
            conn.execute(
                """
                INSERT INTO users(id, created_at)
                VALUES (?, ?)
                ON CONFLICT(id) DO NOTHING
            """,
                (test_user_id, _utc_now_iso()),
            )
            conn.execute(
                """
                INSERT INTO privacy_audit_log(user_id, action, detail, created_at)
                VALUES (?, ?, ?, ?)
            """,
                (test_user_id, "smoke_test", "connectivity check", _utc_now_iso()),
            )
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM privacy_audit_log
                WHERE user_id = ? AND action = ?
            """,
                (test_user_id, "smoke_test"),
            ).fetchone()
            if int(row["cnt"]) >= 1:
                pass_check("write_read", "Successfully inserted and read smoke test rows.")
            else:
                fail_check("write_read", "Write/read cycle did not return expected rows.")

            conn.execute("DELETE FROM privacy_audit_log WHERE user_id = ?", (test_user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (test_user_id,))
            conn.commit()
            pass_check("cleanup", "Smoke test rows cleaned up.")
        except Exception as exc:
            conn.rollback()
            fail_check("write_read", f"Write/read/delete cycle failed: {exc}")

    return result


def main() -> None:
    print(json.dumps(run_smoke_test(), indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
