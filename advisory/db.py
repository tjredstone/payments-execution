import os
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from psycopg import connect as pg_connect
from psycopg.rows import dict_row
from secret_store import read_secret
from werkzeug.security import check_password_hash, generate_password_hash

ENCRYPTION_KEY_ENV = "TOKENS_ENCRYPTION_KEY"
DATABASE_URL_ENV = "DATABASE_URL"
DB_SKIP_SCHEMA_INIT_ENV = "DB_SKIP_SCHEMA_INIT"
FIELD_ENC_PREFIX = "enc::"
SENSITIVE_TEXT_COLUMNS: dict[str, list[str]] = {
    "transactions": [
        "description",
        "merchant_name",
        "transaction_type",
        "transaction_category",
        "transaction_classification",
        "raw_json",
    ],
    "direct_debits": [
        "status",
        "payee_name",
        "variable_amount",
        "next_payment_date",
        "raw_json",
    ],
    "standing_orders": [
        "status",
        "payee_name",
        "frequency",
        "next_payment_date",
        "raw_json",
    ],
    "advisory_log": ["counterparty", "payload_json"],
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at_iso(seconds: int | None) -> str | None:
    if not seconds:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _database_url() -> str:
    url = os.getenv(DATABASE_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(f"Missing {DATABASE_URL_ENV} for postgres backend.")
    if "sslmode=" not in url:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}sslmode=require"
    return url


class PostgresConn:
    def __init__(self):
        self._conn = pg_connect(
            _database_url(),
            row_factory=dict_row,
        )

    @staticmethod
    def _translate_query(query: str) -> str:
        # Convert SQLite qmark parameters to psycopg %s parameters.
        return query.replace("?", "%s")

    def execute(self, query: str, params: Any = None):
        translated = self._translate_query(query)
        if params is None:
            return self._conn.execute(translated)
        return self._conn.execute(translated, params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self._conn.rollback()
        self._conn.close()


def get_conn():
    return PostgresConn()


def _get_cipher() -> Fernet:
    key = read_secret(ENCRYPTION_KEY_ENV)
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


def _is_encrypted_field(value: str) -> bool:
    return value.startswith(FIELD_ENC_PREFIX)


def encrypt_db_text(value: str | None) -> str | None:
    if value is None:
        return None
    if _is_encrypted_field(value):
        return value
    encrypted = _encrypt_token(value)
    return FIELD_ENC_PREFIX + encrypted


def decrypt_db_text(value: str | None) -> str | None:
    if value is None:
        return None
    if _is_encrypted_field(value):
        return _decrypt_token(value[len(FIELD_ENC_PREFIX) :])
    if _is_fernet_token(value):
        # Backwards compatibility if values were previously stored as raw Fernet tokens.
        return _decrypt_token(value)
    return value


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
    if os.getenv(DB_SKIP_SCHEMA_INIT_ENV, "0").strip() == "1":
        return

    schema = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_users (
            user_id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bank_connections (
            id BIGSERIAL PRIMARY KEY,
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
        """,
        """
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id BIGSERIAL PRIMARY KEY,
            connection_id BIGINT NOT NULL,
            provider_account_id TEXT NOT NULL,
            display_name TEXT,
            account_type TEXT,
            currency TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(connection_id, provider_account_id),
            FOREIGN KEY (connection_id) REFERENCES bank_connections(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id BIGSERIAL PRIMARY KEY,
            connection_id BIGINT NOT NULL,
            provider_account_id TEXT NOT NULL,
            available REAL,
            current REAL,
            currency TEXT,
            updated_at_provider TEXT,
            captured_at TEXT NOT NULL,
            FOREIGN KEY (connection_id) REFERENCES bank_connections(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id BIGSERIAL PRIMARY KEY,
            connection_id BIGINT NOT NULL,
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
        """,
        """
        CREATE TABLE IF NOT EXISTS advisory_log (
            id BIGSERIAL PRIMARY KEY,
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
        """,
        """
        CREATE TABLE IF NOT EXISTS privacy_audit_log (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS direct_debits (
            id BIGSERIAL PRIMARY KEY,
            connection_id BIGINT NOT NULL,
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
        """,
        """
        CREATE TABLE IF NOT EXISTS standing_orders (
            id BIGSERIAL PRIMARY KEY,
            connection_id BIGINT NOT NULL,
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
        """,
    ]

    with get_conn() as conn:
        for statement in schema:
            conn.execute(statement)
        conn.commit()


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


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def create_auth_user(email: str, password: str) -> str:
    normalized_email = _normalize_email(email)
    if "@" not in normalized_email:
        raise ValueError("Enter a valid email address.")
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters.")

    user_id = str(uuid.uuid4())
    now = _utc_now_iso()
    ensure_user(user_id)

    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO auth_users(
                    user_id, email, password_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    user_id,
                    normalized_email,
                    generate_password_hash(password),
                    now,
                    now,
                ),
            )
            conn.commit()
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise ValueError("An account with that email already exists.") from exc
            raise
    return user_id


def authenticate_auth_user(email: str, password: str) -> dict[str, Any] | None:
    normalized_email = _normalize_email(email)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT user_id, email, password_hash
            FROM auth_users
            WHERE email = ?
            LIMIT 1
        """,
            (normalized_email,),
        ).fetchone()
        if not row:
            return None
        if not check_password_hash(row["password_hash"], password):
            return None
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE auth_users
            SET last_login_at = ?, updated_at = ?
            WHERE user_id = ?
        """,
            (now, now, row["user_id"]),
        )
        conn.commit()
    return {"user_id": row["user_id"], "email": row["email"]}


def get_auth_user_by_id(user_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT user_id, email, created_at, updated_at, last_login_at
            FROM auth_users
            WHERE user_id = ?
            LIMIT 1
        """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def resolve_effective_user_id(env_user_id: str | None = None) -> str:
    explicit = (env_user_id or "").strip()
    if explicit:
        return explicit
    raise ValueError("Missing LOCAL_USER_ID. Set an explicit user id for CLI execution.")


def log_privacy_event(user_id: str, action: str, detail: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO privacy_audit_log(user_id, action, detail, created_at)
            VALUES (?, ?, ?, ?)
        """,
            (user_id, action, detail, _utc_now_iso()),
        )
        conn.commit()


def export_user_data(user_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        profile = conn.execute(
            """
            SELECT user_id, email, created_at, updated_at, last_login_at
            FROM auth_users
            WHERE user_id = ?
        """,
            (user_id,),
        ).fetchone()
        connections = conn.execute(
            """
            SELECT id, provider, token_type, scope, access_expires_at, refresh_expires_at,
                   last_refreshed_at, created_at, updated_at
            FROM bank_connections
            WHERE user_id = ?
            ORDER BY created_at ASC
        """,
            (user_id,),
        ).fetchall()

        connection_ids = [int(row["id"]) for row in connections]
        accounts: list[dict[str, Any]] = []
        balances: list[dict[str, Any]] = []
        transactions: list[dict[str, Any]] = []
        direct_debits: list[dict[str, Any]] = []
        standing_orders: list[dict[str, Any]] = []
        if connection_ids:
            placeholders = ",".join(["?"] * len(connection_ids))

            accounts = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM bank_accounts
                    WHERE connection_id IN ({placeholders})
                    ORDER BY created_at ASC
                """,
                    connection_ids,
                ).fetchall()
            ]
            balances = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM balance_snapshots
                    WHERE connection_id IN ({placeholders})
                    ORDER BY captured_at ASC
                """,
                    connection_ids,
                ).fetchall()
            ]
            transactions = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM transactions
                    WHERE connection_id IN ({placeholders})
                    ORDER BY COALESCE(timestamp, updated_at) ASC
                """,
                    connection_ids,
                ).fetchall()
            ]
            direct_debits = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM direct_debits
                    WHERE connection_id IN ({placeholders})
                    ORDER BY updated_at ASC
                """,
                    connection_ids,
                ).fetchall()
            ]
            standing_orders = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM standing_orders
                    WHERE connection_id IN ({placeholders})
                    ORDER BY updated_at ASC
                """,
                    connection_ids,
                ).fetchall()
            ]

        advisory_log = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM advisory_log
                WHERE user_id = ?
                ORDER BY created_at ASC
            """,
                (user_id,),
            ).fetchall()
        ]
        privacy_events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM privacy_audit_log
                WHERE user_id = ?
                ORDER BY created_at ASC
            """,
                (user_id,),
            ).fetchall()
        ]

    # Decrypt encrypted text fields for export readability.
    for row in transactions:
        for field in [
            "description",
            "merchant_name",
            "transaction_type",
            "transaction_category",
            "transaction_classification",
            "raw_json",
        ]:
            row[field] = decrypt_db_text(row.get(field))
    for row in direct_debits:
        for field in ["status", "payee_name", "variable_amount", "next_payment_date", "raw_json"]:
            row[field] = decrypt_db_text(row.get(field))
    for row in standing_orders:
        for field in ["status", "payee_name", "frequency", "next_payment_date", "raw_json"]:
            row[field] = decrypt_db_text(row.get(field))
    for row in advisory_log:
        row["counterparty"] = decrypt_db_text(row.get("counterparty"))
        row["payload_json"] = decrypt_db_text(row.get("payload_json"))

    return {
        "exported_at": _utc_now_iso(),
        "user_profile": dict(profile) if profile else None,
        "bank_connections": [dict(row) for row in connections],
        "bank_accounts": accounts,
        "balance_snapshots": balances,
        "transactions": transactions,
        "direct_debits": direct_debits,
        "standing_orders": standing_orders,
        "advisory_log": advisory_log,
        "privacy_audit_log": privacy_events,
    }


def delete_user_data(user_id: str) -> dict[str, int]:
    with get_conn() as conn:
        conn.execute("BEGIN")
        try:
            advisory_cur = conn.execute(
                """
                DELETE FROM advisory_log
                WHERE user_id = ?
            """,
                (user_id,),
            )
            privacy_cur = conn.execute(
                """
                DELETE FROM privacy_audit_log
                WHERE user_id = ?
            """,
                (user_id,),
            )
            user_cur = conn.execute(
                """
                DELETE FROM users
                WHERE id = ?
            """,
                (user_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "users_deleted": user_cur.rowcount,
        "advisory_rows_deleted": advisory_cur.rowcount,
        "privacy_events_deleted": privacy_cur.rowcount,
    }


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
    direct_debit_retention_days: int = 180,
    standing_order_retention_days: int = 180,
    advisory_log_retention_days: int = 365,
    raw_payload_retention_days: int = 30,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    tx_cutoff = (now - timedelta(days=transaction_retention_days)).isoformat()
    bal_cutoff = (now - timedelta(days=balance_retention_days)).isoformat()
    dd_cutoff = (now - timedelta(days=direct_debit_retention_days)).isoformat()
    so_cutoff = (now - timedelta(days=standing_order_retention_days)).isoformat()
    advisory_cutoff = (now - timedelta(days=advisory_log_retention_days)).isoformat()
    raw_payload_cutoff = (now - timedelta(days=raw_payload_retention_days)).isoformat()

    with get_conn() as conn:
        tx_payload_cur = conn.execute(
            """
            UPDATE transactions
            SET raw_json = '{}'
            WHERE updated_at < ?
              AND raw_json != '{}'
        """,
            (raw_payload_cutoff,),
        )
        dd_payload_cur = conn.execute(
            """
            UPDATE direct_debits
            SET raw_json = '{}'
            WHERE updated_at < ?
              AND raw_json != '{}'
        """,
            (raw_payload_cutoff,),
        )
        so_payload_cur = conn.execute(
            """
            UPDATE standing_orders
            SET raw_json = '{}'
            WHERE updated_at < ?
              AND raw_json != '{}'
        """,
            (raw_payload_cutoff,),
        )
        advisory_payload_cur = conn.execute(
            """
            UPDATE advisory_log
            SET payload_json = '{}'
            WHERE created_at < ?
              AND payload_json != '{}'
        """,
            (raw_payload_cutoff,),
        )
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
        dd_cur = conn.execute(
            """
            DELETE FROM direct_debits
            WHERE updated_at < ?
        """,
            (dd_cutoff,),
        )
        so_cur = conn.execute(
            """
            DELETE FROM standing_orders
            WHERE updated_at < ?
        """,
            (so_cutoff,),
        )
        advisory_cur = conn.execute(
            """
            DELETE FROM advisory_log
            WHERE created_at < ?
        """,
            (advisory_cutoff,),
        )
        conn.commit()

    return {
        "transactions_deleted": tx_cur.rowcount,
        "balance_snapshots_deleted": bal_cur.rowcount,
        "direct_debits_deleted": dd_cur.rowcount,
        "standing_orders_deleted": so_cur.rowcount,
        "advisory_log_deleted": advisory_cur.rowcount,
        "transactions_payload_scrubbed": tx_payload_cur.rowcount,
        "direct_debits_payload_scrubbed": dd_payload_cur.rowcount,
        "standing_orders_payload_scrubbed": so_payload_cur.rowcount,
        "advisory_payload_scrubbed": advisory_payload_cur.rowcount,
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
            INSERT INTO advisory_log(
                user_id, provider, decision_fingerprint, action, counterparty,
                due_date, amount, payload_json, sent_via, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider, decision_fingerprint) DO NOTHING
        """,
            (
                user_id,
                provider,
                fingerprint,
                str(decision.get("action", "")),
                encrypt_db_text(str(decision.get("counterparty", ""))),
                str(decision.get("due_date", "")),
                float(decision.get("recommended_amount", 0)),
                encrypt_db_text(json.dumps(decision, ensure_ascii=True, sort_keys=True)),
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


def list_connections(provider: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM bank_connections
            WHERE provider = ?
        """,
            (provider,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["access_token"] = _decrypt_token(data["access_token"])
        data["refresh_token"] = _decrypt_token(data["refresh_token"])
        result.append(data)
    return result


def has_connection(user_id: str, provider: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM bank_connections
            WHERE user_id = ? AND provider = ?
            LIMIT 1
        """,
            (user_id, provider),
        ).fetchone()
    return row is not None


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


def migrate_plaintext_sensitive_fields() -> int:
    ensure_token_crypto_ready()
    migration_plan = [(table, "id", columns) for table, columns in SENSITIVE_TEXT_COLUMNS.items()]

    migrated = 0
    with get_conn() as conn:
        for table, pk, columns in migration_plan:
            select_cols = ", ".join([pk] + columns)
            rows = conn.execute(f"SELECT {select_cols} FROM {table}").fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column in columns:
                    value = row[column]
                    if not isinstance(value, str):
                        continue
                    if _is_encrypted_field(value):
                        continue
                    updates[column] = encrypt_db_text(value)
                if not updates:
                    continue
                set_clause = ", ".join([f"{col} = ?" for col in updates])
                params = list(updates.values()) + [row[pk]]
                conn.execute(
                    f"UPDATE {table} SET {set_clause} WHERE {pk} = ?",
                    params,
                )
                migrated += 1
        conn.commit()
    return migrated


def rotate_encryption_key(old_key: str, new_key: str) -> dict[str, int]:
    old_cipher = Fernet(old_key.encode("utf-8"))
    new_cipher = Fernet(new_key.encode("utf-8"))

    counts: dict[str, int] = {
        "bank_connections_rotated": 0,
        "sensitive_fields_rotated": 0,
    }

    def _decrypt_old_fernet(value: str) -> str:
        try:
            return old_cipher.decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Old encryption key cannot decrypt existing DB data.") from exc

    with get_conn() as conn:
        conn.execute("BEGIN")
        try:
            token_rows = conn.execute(
                """
                SELECT id, access_token, refresh_token
                FROM bank_connections
            """
            ).fetchall()
            for row in token_rows:
                updates: dict[str, str] = {}
                for col in ("access_token", "refresh_token"):
                    value = row[col]
                    if not isinstance(value, str):
                        continue
                    if _is_fernet_token(value):
                        plaintext = _decrypt_old_fernet(value)
                    else:
                        plaintext = value
                    updates[col] = new_cipher.encrypt(plaintext.encode("utf-8")).decode("utf-8")
                if updates:
                    conn.execute(
                        """
                        UPDATE bank_connections
                        SET access_token = ?, refresh_token = ?, updated_at = ?
                        WHERE id = ?
                    """,
                        (
                            updates.get("access_token", row["access_token"]),
                            updates.get("refresh_token", row["refresh_token"]),
                            _utc_now_iso(),
                            row["id"],
                        ),
                    )
                    counts["bank_connections_rotated"] += 1

            for table, columns in SENSITIVE_TEXT_COLUMNS.items():
                select_cols = ", ".join(["id"] + columns)
                rows = conn.execute(f"SELECT {select_cols} FROM {table}").fetchall()
                for row in rows:
                    updates: dict[str, str] = {}
                    for column in columns:
                        value = row[column]
                        if not isinstance(value, str):
                            continue
                        if _is_encrypted_field(value):
                            plaintext = _decrypt_old_fernet(value[len(FIELD_ENC_PREFIX) :])
                        else:
                            plaintext = value
                        rotated = FIELD_ENC_PREFIX + new_cipher.encrypt(
                            plaintext.encode("utf-8")
                        ).decode("utf-8")
                        if rotated != value:
                            updates[column] = rotated
                    if updates:
                        set_clause = ", ".join([f"{col} = ?" for col in updates])
                        params = list(updates.values()) + [row["id"]]
                        conn.execute(
                            f"UPDATE {table} SET {set_clause} WHERE id = ?",
                            params,
                        )
                        counts["sensitive_fields_rotated"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return counts


def upsert_bank_account(
    connection_id: int,
    provider_account_id: str,
    display_name: str | None,
    account_type: str | None,
    currency: str | None,
    conn=None,
) -> None:
    now = _utc_now_iso()
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
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
    if own_conn:
        conn.commit()
        conn.close()


def add_balance_snapshot(
    connection_id: int,
    provider_account_id: str,
    available: float | None,
    current: float | None,
    currency: str | None,
    updated_at_provider: str | None,
    conn=None,
) -> None:
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
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
    if own_conn:
        conn.commit()
        conn.close()


def upsert_transaction(
    connection_id: int,
    provider_account_id: str,
    tx: dict[str, Any],
    raw_json: str,
    conn=None,
) -> None:
    def _as_db_text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        return json.dumps(value, ensure_ascii=True, sort_keys=True)

    now = _utc_now_iso()
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
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
            encrypt_db_text(_as_db_text(tx.get("description"))),
            encrypt_db_text(_as_db_text(tx.get("merchant_name"))),
            encrypt_db_text(_as_db_text(tx.get("transaction_type"))),
            encrypt_db_text(_as_db_text(tx.get("transaction_category"))),
            encrypt_db_text(_as_db_text(tx.get("transaction_classification"))),
            _as_db_text(tx.get("timestamp")),
            (tx.get("running_balance") or {}).get("amount"),
            encrypt_db_text(raw_json),
            now,
            now,
        ),
    )
    if own_conn:
        conn.commit()
        conn.close()


def upsert_direct_debit(
    connection_id: int,
    provider_account_id: str,
    direct_debit: dict[str, Any],
    raw_json: str,
    conn=None,
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

    own_conn = conn is None
    if own_conn:
        conn = get_conn()
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
            encrypt_db_text(str(direct_debit.get("status") or "")),
            encrypt_db_text(
                str(
                    direct_debit.get("payee_name")
                    or direct_debit.get("merchant_name")
                    or direct_debit.get("name")
                    or ""
                )
            ),
            encrypt_db_text(
                str(direct_debit.get("variable_amount"))
                if direct_debit.get("variable_amount") is not None
                else None
            ),
            encrypt_db_text(
                str(
                    direct_debit.get("next_payment_date")
                    or direct_debit.get("payment_date")
                    or direct_debit.get("timestamp")
                    or ""
                )
            ),
            latest_amount,
            latest_currency,
            encrypt_db_text(raw_json),
            now,
            now,
        ),
    )
    if own_conn:
        conn.commit()
        conn.close()


def upsert_standing_order(
    connection_id: int,
    provider_account_id: str,
    standing_order: dict[str, Any],
    raw_json: str,
    conn=None,
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

    own_conn = conn is None
    if own_conn:
        conn = get_conn()
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
            encrypt_db_text(str(standing_order.get("status") or "")),
            encrypt_db_text(
                str(
                    standing_order.get("payee_name")
                    or standing_order.get("beneficiary_name")
                    or standing_order.get("name")
                    or ""
                )
            ),
            encrypt_db_text(
                str(
                    standing_order.get("frequency")
                    or (
                        (standing_order.get("schedule") or {}).get("frequency")
                        if isinstance(standing_order.get("schedule"), dict)
                        else ""
                    )
                    or ""
                )
            ),
            encrypt_db_text(
                str(
                    standing_order.get("next_payment_date")
                    or standing_order.get("payment_date")
                    or standing_order.get("timestamp")
                    or ""
                )
            ),
            next_amount,
            next_currency,
            encrypt_db_text(raw_json),
            now,
            now,
        ),
    )
    if own_conn:
        conn.commit()
        conn.close()
