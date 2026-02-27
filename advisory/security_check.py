from __future__ import annotations

import json
import os
import stat
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    def load_dotenv() -> None:
        return None

from db import ENCRYPTION_KEY_ENV, ensure_token_crypto_ready
from secret_store import read_secret_with_source


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))


def run_security_check() -> dict[str, object]:
    load_dotenv()
    findings: list[dict[str, str]] = []
    checks: list[dict[str, str]] = []

    def ok(name: str, detail: str) -> None:
        checks.append({"check": name, "status": "ok", "detail": detail})

    def fail(name: str, detail: str, severity: str = "high") -> None:
        checks.append({"check": name, "status": "fail", "detail": detail})
        findings.append({"severity": severity, "issue": name, "detail": detail})

    try:
        ensure_token_crypto_ready()
        _, key_source = read_secret_with_source(ENCRYPTION_KEY_ENV)
        ok("encryption_key", f"{ENCRYPTION_KEY_ENV} is configured and valid (source={key_source}).")
    except Exception as exc:
        fail("encryption_key", str(exc))

    try:
        _, session_source = read_secret_with_source("APP_SESSION_SECRET")
        ok("session_secret", f"APP_SESSION_SECRET is configured (source={session_source}).")
    except Exception as exc:
        fail("session_secret", str(exc))

    db_backend = os.getenv("DB_BACKEND", "").strip().lower()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if db_backend and db_backend != "postgres":
        fail("db_backend", "This deployment is Postgres-only. Set DB_BACKEND=postgres (or unset).")
    else:
        ok("db_backend", "DB backend is postgres.")

    if not database_url:
        fail("database_url", "DATABASE_URL is required.")
    elif "sslmode=require" not in database_url:
        fail("database_url_ssl", "DATABASE_URL should include sslmode=require for secure DB transport.")
    else:
        ok("database_url_ssl", "DATABASE_URL includes sslmode=require.")

    flask_debug = os.getenv("FLASK_DEBUG", "0")
    if flask_debug == "1":
        fail("flask_debug", "FLASK_DEBUG=1 increases risk; use 0 for normal runs.", severity="medium")
    else:
        ok("flask_debug", "FLASK_DEBUG is disabled.")

    allow_remote = os.getenv("ALLOW_REMOTE_ACCESS", "0")
    if allow_remote == "1":
        fail("remote_access", "ALLOW_REMOTE_ACCESS=1 permits non-local requests.", severity="medium")
    else:
        ok("remote_access", "Remote access disabled (localhost-only).")

    enforce_https = os.getenv("ENFORCE_HTTPS", "0")
    session_cookie_secure = os.getenv("SESSION_COOKIE_SECURE", "0")
    if enforce_https == "1":
        ok("https_enforcement", "ENFORCE_HTTPS is enabled.")
    elif allow_remote == "1":
        fail(
            "https_enforcement",
            "Remote access is enabled without ENFORCE_HTTPS=1.",
            severity="high",
        )
    else:
        checks.append(
            {
                "check": "https_enforcement",
                "status": "info",
                "detail": "ENFORCE_HTTPS is disabled for local-only mode.",
            }
        )

    if enforce_https == "1" or session_cookie_secure == "1":
        ok("session_cookie_secure", "Secure session cookies are enabled.")
    elif allow_remote == "1":
        fail(
            "session_cookie_secure",
            "Remote access is enabled without secure session cookies.",
            severity="high",
        )
    else:
        checks.append(
            {
                "check": "session_cookie_secure",
                "status": "info",
                "detail": "Secure session cookies are optional in local HTTP mode.",
            }
        )

    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        mode = _mode(env_path)
        if stat.S_IMODE(env_path.stat().st_mode) & 0o077:
            fail("env_file_permissions", f".env permissions are {mode}; expected 0o600.", severity="medium")
        else:
            ok("env_file_permissions", f".env permissions are {mode}.")
    else:
        checks.append(
            {
                "check": "env_file_permissions",
                "status": "info",
                "detail": ".env not found in advisory/ (may rely on external env injection).",
            }
        )

    checks.append(
        {
            "check": "db_file_permissions",
            "status": "info",
            "detail": "Not applicable for postgres backend.",
        }
    )

    summary = {
        "passed": len([c for c in checks if c["status"] == "ok"]),
        "failed": len([c for c in checks if c["status"] == "fail"]),
        "info": len([c for c in checks if c["status"] == "info"]),
    }
    return {"summary": summary, "checks": checks, "findings": findings}


def main() -> None:
    print(json.dumps(run_security_check(), indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
