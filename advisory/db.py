import os
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

DB_PATH = Path(__file__).parent / "rentbot.db"
ENCRYPTION_KEY_ENV = "TOKENS_ENCRYPTION_KEY"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at_iso(seconds: int | None) -> str | None:
    if not seconds:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _harden_db_file_permissions() -> None:
    if not DB_PATH.exists():
        return
    # Owner read/write only for local secret storage.
    os.chmod(DB_PATH, 0o600)


def _get_cipher() -> Fernet:
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"Missing {ENCRYPTION_KEY_ENV}. Generate one and set it in your environment."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Invalid {ENCRYPTION_KEY_ENV}. Must be a Fernet key.") from exc


def ensure_token_crypto_ready() -> None:
    _get_cipher()


def _is_fernet_token(value: str) -> bool:
    return value.startswith("gAAAA")


def _encrypt_token(token: str) -> str:
    cipher = _get_cipher()
    return cipher.encrypt(token.encode("utf-8")).decode("utf-8")


def _decrypt_token(token: str) -> str:
    cipher = _get_cipher()
    try:
        return cipher.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Could not decrypt stored tokens. Check TOKENS_ENCRYPTION_KEY.") from exc


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                token_type TEXT,
                scope TEXT,
                access_expires_at TEXT,
                refresh_expires_at TEXT,
                last_refreshed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, provider),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER NOT NULL,
                provider_account_id TEXT NOT NULL,
                display_name TEXT,
                account_type TEXT,
                currency TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(connection_id, provider_account_id),
                FOREIGN KEY (connection_id) REFERENCES bank_connections(id) ON DELETE CASCADE
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER NOT NULL,
                provider_account_id TEXT NOT NULL,
                available REAL,
                current REAL,
                currency TEXT,
                updated_at_provider TEXT,
                captured_at TEXT NOT NULL,
                FOREIGN KEY (connection_id) REFERENCES bank_connections(id) ON DELETE CASCADE
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER NOT NULL,
                provider_account_id TEXT NOT NULL,
                provider_transaction_id TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                description TEXT,
                merchant_name TEXT,
                transaction_type TEXT,
                transaction_category TEXT,
                transaction_classification TEXT,
                timestamp TEXT,
                running_balance REAL,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(connection_id, provider_transaction_id),
                FOREIGN KEY (connection_id) REFERENCES bank_connections(id) ON DELETE CASCADE
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS advisory_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                decision_fingerprint TEXT NOT NULL,
                action TEXT NOT NULL,
                counterparty TEXT NOT NULL,
                due_date TEXT NOT NULL,
                amount REAL NOT NULL,
                payload_json TEXT NOT NULL,
                sent_via TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, provider, decision_fingerprint)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS direct_debits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER NOT NULL,
                provider_account_id TEXT NOT NULL,
                provider_direct_debit_id TEXT NOT NULL,
                status TEXT,
                payee_name TEXT,
                variable_amount TEXT,
                next_payment_date TEXT,
                latest_amount REAL,
                latest_currency TEXT,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(connection_id, provider_account_id, provider_direct_debit_id),
                FOREIGN KEY (connection_id) REFERENCES bank_connections(id) ON DELETE CASCADE
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS standing_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER NOT NULL,
                provider_account_id TEXT NOT NULL,
                provider_standing_order_id TEXT NOT NULL,
                status TEXT,
                payee_name TEXT,
                frequency TEXT,
                next_payment_date TEXT,
                next_payment_amount REAL,
                next_payment_currency TEXT,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(connection_id, provider_account_id, provider_standing_order_id),
                FOREIGN KEY (connection_id) REFERENCES bank_connections(id) ON DELETE CASCADE
            )
        """
        )
        conn.commit()
    _harden_db_file_permissions()


def ensure_user(user_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users(id, created_at)
            VALUES (?, ?)
            ON CONFLICT(id) DO NOTHING
        """,
            (user_id, _utc_now_iso()),
        )
        conn.commit()


def create_oauth_state(user_id: str, ttl_seconds: int = 900) -> str:
    ensure_user(user_id)
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO oauth_states(state, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
        """,
            (state, user_id, now.isoformat(), expires_at),
        )
        conn.commit()
    return state


def consume_oauth_state(user_id: str, state: str) -> bool:
    now = _utc_now_iso()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT state
            FROM oauth_states
            WHERE state = ?
              AND user_id = ?
              AND consumed_at IS NULL
              AND expires_at > ?
        """,
            (state, user_id, now),
        ).fetchone()
        if not row:
            return False

        conn.execute(
            "UPDATE oauth_states SET consumed_at = ? WHERE state = ?",
            (now, state),
        )
        conn.commit()
    return True


def purge_expired_oauth_states() -> int:
    now = _utc_now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            DELETE FROM oauth_states
            WHERE consumed_at IS NOT NULL OR expires_at <= ?
        """,
            (now,),
        )
        conn.commit()
        return cur.rowcount


def purge_old_data(
    *,
    transaction_retention_days: int = 180,
    balance_retention_days: int = 180,
) -> dict[str, int]:
    tx_cutoff = (datetime.now(timezone.utc) - timedelta(days=transaction_retention_days)).isoformat()
    bal_cutoff = (datetime.now(timezone.utc) - timedelta(days=balance_retention_days)).isoformat()

    with get_conn() as conn:
        tx_cur = conn.execute(
            """
            DELETE FROM transactions
            WHERE COALESCE(timestamp, updated_at) < ?
        """,
            (tx_cutoff,),
        )
        bal_cur = conn.execute(
            """
            DELETE FROM balance_snapshots
            WHERE captured_at < ?
        """,
            (bal_cutoff,),
        )
        conn.commit()

    return {
        "transactions_deleted": tx_cur.rowcount,
        "balance_snapshots_deleted": bal_cur.rowcount,
    }


def make_decision_fingerprint(decision: dict[str, Any]) -> str:
    material = "|".join(
        [
            str(decision.get("obligation_key", "")),
            str(decision.get("action", "")),
            str(decision.get("recommended_execution_date", "")),
            str(decision.get("recommended_amount", "")),
            str(decision.get("due_date", "")),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def has_advisory_been_sent(user_id: str, provider: str, fingerprint: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM advisory_log
            WHERE user_id = ? AND provider = ? AND decision_fingerprint = ?
            LIMIT 1
        """,
            (user_id, provider, fingerprint),
        ).fetchone()
        return row is not None


def log_advisory_sent(
    user_id: str,
    provider: str,
    decision: dict[str, Any],
    sent_via: str,
) -> None:
    fingerprint = make_decision_fingerprint(decision)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO advisory_log(
                user_id, provider, decision_fingerprint, action, counterparty,
                due_date, amount, payload_json, sent_via, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                user_id,
                provider,
                fingerprint,
                str(decision.get("action", "")),
                str(decision.get("counterparty", "")),
                str(decision.get("due_date", "")),
                float(decision.get("recommended_amount", 0)),
                json.dumps(decision, ensure_ascii=True, sort_keys=True),
                sent_via,
                _utc_now_iso(),
            ),
        )
        conn.commit()


def save_connection_tokens(user_id: str, provider: str, tokens: dict) -> None:
    ensure_user(user_id)
    now = _utc_now_iso()

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token or not refresh_token:
        raise ValueError("Token payload missing access_token or refresh_token")
    encrypted_access_token = _encrypt_token(access_token)
    encrypted_refresh_token = _encrypt_token(refresh_token)

    access_expires_at = _expires_at_iso(tokens.get("expires_in"))
    refresh_ttl = tokens.get("refresh_expires_in") or tokens.get(
        "refresh_token_expires_in"
    )
    refresh_expires_at = _expires_at_iso(refresh_ttl)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bank_connections(
                user_id, provider, access_token, refresh_token, token_type, scope,
                access_expires_at, refresh_expires_at, last_refreshed_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_type = excluded.token_type,
                scope = excluded.scope,
                access_expires_at = excluded.access_expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                last_refreshed_at = excluded.last_refreshed_at,
                updated_at = excluded.updated_at
        """,
            (
                user_id,
                provider,
                encrypted_access_token,
                encrypted_refresh_token,
                tokens.get("token_type"),
                tokens.get("scope"),
                access_expires_at,
                refresh_expires_at,
                now,
                now,
                now,
            ),
        )
        conn.commit()


def get_connection(user_id: str, provider: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM bank_connections
            WHERE user_id = ? AND provider = ?
        """,
            (user_id, provider),
        ).fetchone()
    if not row:
        return None

    data = dict(row)
    data["access_token"] = _decrypt_token(data["access_token"])
    data["refresh_token"] = _decrypt_token(data["refresh_token"])
    return data


def migrate_plaintext_tokens() -> int:
    ensure_token_crypto_ready()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, access_token, refresh_token
            FROM bank_connections
        """
        ).fetchall()

        migrated = 0
        for row in rows:
            access_token = row["access_token"]
            refresh_token = row["refresh_token"]
            if _is_fernet_token(access_token) and _is_fernet_token(refresh_token):
                continue

            conn.execute(
                """
                UPDATE bank_connections
                SET access_token = ?, refresh_token = ?, updated_at = ?
                WHERE id = ?
            """,
                (
                    _encrypt_token(access_token),
                    _encrypt_token(refresh_token),
                    _utc_now_iso(),
                    row["id"],
                ),
            )
            migrated += 1

        conn.commit()
        return migrated


def upsert_bank_account(
    connection_id: int,
    provider_account_id: str,
    display_name: str | None,
    account_type: str | None,
    currency: str | None,
) -> None:
    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bank_accounts(
                connection_id, provider_account_id, display_name, account_type, currency, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id, provider_account_id) DO UPDATE SET
                display_name = excluded.display_name,
                account_type = excluded.account_type,
                currency = excluded.currency,
                updated_at = excluded.updated_at
        """,
            (
                connection_id,
                provider_account_id,
                display_name,
                account_type,
                currency,
                now,
                now,
            ),
        )
        conn.commit()


def add_balance_snapshot(
    connection_id: int,
    provider_account_id: str,
    available: float | None,
    current: float | None,
    currency: str | None,
    updated_at_provider: str | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO balance_snapshots(
                connection_id, provider_account_id, available, current, currency, updated_at_provider, captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                connection_id,
                provider_account_id,
                available,
                current,
                currency,
                updated_at_provider,
                _utc_now_iso(),
            ),
        )
        conn.commit()


def upsert_transaction(
    connection_id: int,
    provider_account_id: str,
    tx: dict[str, Any],
    raw_json: str,
) -> None:
    def _as_db_text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        return json.dumps(value, ensure_ascii=True, sort_keys=True)

    now = _utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO transactions(
                connection_id, provider_account_id, provider_transaction_id, amount, currency,
                description, merchant_name, transaction_type, transaction_category,
                transaction_classification, timestamp, running_balance, raw_json, first_seen_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id, provider_transaction_id) DO UPDATE SET
                amount = excluded.amount,
                currency = excluded.currency,
                description = excluded.description,
                merchant_name = excluded.merchant_name,
                transaction_type = excluded.transaction_type,
                transaction_category = excluded.transaction_category,
                transaction_classification = excluded.transaction_classification,
                timestamp = excluded.timestamp,
                running_balance = excluded.running_balance,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
        """,
            (
                connection_id,
                provider_account_id,
                tx["transaction_id"],
                tx.get("amount"),
                tx.get("currency", "GBP"),
                _as_db_text(tx.get("description")),
                _as_db_text(tx.get("merchant_name")),
                _as_db_text(tx.get("transaction_type")),
                _as_db_text(tx.get("transaction_category")),
                _as_db_text(tx.get("transaction_classification")),
                _as_db_text(tx.get("timestamp")),
                (tx.get("running_balance") or {}).get("amount"),
                raw_json,
                now,
                now,
            ),
        )
        conn.commit()


def upsert_direct_debit(
    connection_id: int,
    provider_account_id: str,
    direct_debit: dict[str, Any],
    raw_json: str,
) -> None:
    def _extract_amount_currency(value: Any) -> tuple[float | None, str | None]:
        if isinstance(value, dict):
            amount_value = value.get("amount")
            currency_value = value.get("currency")
            amount = float(amount_value) if isinstance(amount_value, (int, float)) else None
            return amount, currency_value
        if isinstance(value, (int, float)):
            return float(value), None
        return None, None

    now = _utc_now_iso()
    direct_debit_id = (
        direct_debit.get("direct_debit_id")
        or direct_debit.get("mandate_id")
        or direct_debit.get("id")
    )
    if not direct_debit_id:
        return

    latest_amount_obj = (
        (direct_debit.get("latest_payment") or {}).get("amount")
        or direct_debit.get("latest_amount")
        or direct_debit.get("previous_payment_amount")
    )
    latest_amount, latest_currency = _extract_amount_currency(latest_amount_obj)
    if not latest_currency:
        latest_currency = direct_debit.get("currency")

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO direct_debits(
                connection_id, provider_account_id, provider_direct_debit_id, status, payee_name,
                variable_amount, next_payment_date, latest_amount, latest_currency, raw_json,
                first_seen_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id, provider_account_id, provider_direct_debit_id) DO UPDATE SET
                status = excluded.status,
                payee_name = excluded.payee_name,
                variable_amount = excluded.variable_amount,
                next_payment_date = excluded.next_payment_date,
                latest_amount = excluded.latest_amount,
                latest_currency = excluded.latest_currency,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
        """,
            (
                connection_id,
                provider_account_id,
                str(direct_debit_id),
                str(direct_debit.get("status") or ""),
                str(
                    direct_debit.get("payee_name")
                    or direct_debit.get("merchant_name")
                    or direct_debit.get("name")
                    or ""
                ),
                str(direct_debit.get("variable_amount")) if direct_debit.get("variable_amount") is not None else None,
                str(
                    direct_debit.get("next_payment_date")
                    or direct_debit.get("payment_date")
                    or direct_debit.get("timestamp")
                    or ""
                ),
                latest_amount,
                latest_currency,
                raw_json,
                now,
                now,
            ),
        )
        conn.commit()


def upsert_standing_order(
    connection_id: int,
    provider_account_id: str,
    standing_order: dict[str, Any],
    raw_json: str,
) -> None:
    def _extract_amount_currency(value: Any) -> tuple[float | None, str | None]:
        if isinstance(value, dict):
            amount_value = value.get("amount")
            currency_value = value.get("currency")
            amount = float(amount_value) if isinstance(amount_value, (int, float)) else None
            return amount, currency_value
        if isinstance(value, (int, float)):
            return float(value), None
        return None, None

    now = _utc_now_iso()
    standing_order_id = (
        standing_order.get("standing_order_id")
        or standing_order.get("id")
        or standing_order.get("order_id")
        or standing_order.get("mandate_id")
    )
    if not standing_order_id:
        standing_order_id = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()[:32]
    if not standing_order_id:
        return

    next_amount_obj = (
        standing_order.get("next_payment_amount")
        or standing_order.get("amount")
        or standing_order.get("payment_amount")
    )
    next_amount, next_currency = _extract_amount_currency(next_amount_obj)
    if not next_currency:
        next_currency = standing_order.get("currency")

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO standing_orders(
                connection_id, provider_account_id, provider_standing_order_id, status, payee_name,
                frequency, next_payment_date, next_payment_amount, next_payment_currency, raw_json,
                first_seen_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id, provider_account_id, provider_standing_order_id) DO UPDATE SET
                status = excluded.status,
                payee_name = excluded.payee_name,
                frequency = excluded.frequency,
                next_payment_date = excluded.next_payment_date,
                next_payment_amount = excluded.next_payment_amount,
                next_payment_currency = excluded.next_payment_currency,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
        """,
            (
                connection_id,
                provider_account_id,
                str(standing_order_id),
                str(standing_order.get("status") or ""),
                str(
                    standing_order.get("payee_name")
                    or standing_order.get("beneficiary_name")
                    or standing_order.get("name")
                    or ""
                ),
                str(
                    standing_order.get("frequency")
                    or ((standing_order.get("schedule") or {}).get("frequency") if isinstance(standing_order.get("schedule"), dict) else "")
                    or ""
                ),
                str(
                    standing_order.get("next_payment_date")
                    or standing_order.get("payment_date")
                    or standing_order.get("timestamp")
                    or ""
                ),
                next_amount,
                next_currency,
                raw_json,
                now,
                now,
            ),
        )
        conn.commit()
