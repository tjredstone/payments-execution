from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from config import load_truelayer_config
from db import (
    add_balance_snapshot,
    ensure_token_crypto_ready,
    get_connection,
    init_db,
    list_connections,
    migrate_plaintext_sensitive_fields,
    migrate_plaintext_tokens,
    purge_expired_oauth_states,
    purge_old_data,
    save_connection_tokens,
    upsert_bank_account,
    upsert_direct_debit,
    upsert_standing_order,
    upsert_transaction,
)
from truelayer_client import TrueLayerClient


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got: {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be >= 1, got: {parsed}")
    return parsed


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_expiring_soon(expires_at: str | None, safety_window_seconds: int = 120) -> bool:
    dt = _parse_iso_utc(expires_at)
    if not dt:
        return True
    now = datetime.now(timezone.utc)
    return dt <= (now + timedelta(seconds=safety_window_seconds))


def _extract_amount_and_currency(value: object) -> tuple[float | None, str | None]:
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, dict):
        amount = value.get("amount")
        currency = value.get("currency")
        return (float(amount), currency) if isinstance(amount, (int, float)) else (None, currency)
    return None, None


def _extract_balance_fields(balance_item: dict) -> tuple[float | None, float | None, str | None, str | None]:
    available, available_currency = _extract_amount_and_currency(balance_item.get("available"))
    current, current_currency = _extract_amount_and_currency(balance_item.get("current"))
    currency = current_currency or available_currency or balance_item.get("currency")
    updated_at_provider = balance_item.get("update_timestamp")
    return available, current, currency, updated_at_provider


def run_daily() -> None:
    load_dotenv()
    init_db()
    ensure_token_crypto_ready()
    migrate_plaintext_tokens()
    migrate_plaintext_sensitive_fields()
    purge_expired_oauth_states()

    user_id_filter = os.getenv("LOCAL_USER_ID", "").strip()
    provider = "truelayer"
    lookback_days = _env_int("TX_LOOKBACK_DAYS", 35)
    tx_retention_days = _env_int("TX_RETENTION_DAYS", 180)
    balance_retention_days = _env_int("BALANCE_RETENTION_DAYS", 180)
    direct_debit_retention_days = _env_int("DIRECT_DEBIT_RETENTION_DAYS", 180)
    standing_order_retention_days = _env_int("STANDING_ORDER_RETENTION_DAYS", 180)
    advisory_retention_days = _env_int("ADVISORY_RETENTION_DAYS", 365)
    raw_payload_retention_days = _env_int("RAW_PAYLOAD_RETENTION_DAYS", 30)

    cfg = load_truelayer_config()
    client = TrueLayerClient(cfg)

    tx_from = date.today() - timedelta(days=lookback_days)
    tx_to = date.today()

    total_balances = 0
    total_transactions = 0
    total_direct_debits = 0
    total_standing_orders = 0
    total_accounts = 0

    if user_id_filter:
        connection = get_connection(user_id=user_id_filter, provider=provider)
        connections = [connection] if connection else []
    else:
        connections = list_connections(provider=provider)

    if not connections:
        print("No connected bank found. Start advisory/app.py and complete /connect first.")
        return

    for connection in connections:
        if not connection:
            continue
        user_id = connection["user_id"]
        access_token = connection["access_token"]
        refresh_token = connection["refresh_token"]
        connection_id = connection["id"]

        if _is_expiring_soon(connection["access_expires_at"]):
            print(f"Access token expiring/expired for user={user_id}. Refreshing token...")
            refreshed = client.refresh_tokens(refresh_token)
            if not refreshed.get("refresh_token"):
                refreshed["refresh_token"] = refresh_token
            save_connection_tokens(user_id=user_id, provider=provider, tokens=refreshed)
            updated = get_connection(user_id=user_id, provider=provider)
            if not updated:
                raise RuntimeError("Connection missing after token refresh save.")
            access_token = updated["access_token"]
            print(f"Token refreshed for user={user_id}.")

        print(f"Fetching accounts for user={user_id}...")
        accounts = client.list_accounts(access_token)
        if not accounts:
            print(f"No accounts returned by provider for user={user_id}.")
            continue
        total_accounts += len(accounts)

        for account in accounts:
            account_id = account["account_id"]
            upsert_bank_account(
                connection_id=connection_id,
                provider_account_id=account_id,
                display_name=account.get("display_name"),
                account_type=account.get("account_type"),
                currency=account.get("currency"),
            )

            balances = client.get_balance(access_token=access_token, account_id=account_id)
            for bal in balances:
                available, current, currency, updated_at_provider = _extract_balance_fields(bal)
                add_balance_snapshot(
                    connection_id=connection_id,
                    provider_account_id=account_id,
                    available=available,
                    current=current,
                    currency=currency,
                    updated_at_provider=updated_at_provider,
                )
                total_balances += 1

            transactions = client.get_transactions(
                access_token=access_token,
                account_id=account_id,
                from_date=tx_from,
                to_date=tx_to,
            )
            for tx in transactions:
                tx_id = tx.get("transaction_id")
                amount = tx.get("amount")
                if not tx_id or amount is None:
                    continue
                upsert_transaction(
                    connection_id=connection_id,
                    provider_account_id=account_id,
                    tx=tx,
                    raw_json=json.dumps(tx, ensure_ascii=True, sort_keys=True),
                )
                total_transactions += 1

            try:
                direct_debits = client.get_direct_debits(access_token=access_token, account_id=account_id)
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "unknown"
                print(
                    f"Direct debit fetch failed for user={user_id} account={account_id} "
                    f"(status={code}); continuing."
                )
                direct_debits = []
            for dd in direct_debits:
                upsert_direct_debit(
                    connection_id=connection_id,
                    provider_account_id=account_id,
                    direct_debit=dd,
                    raw_json=json.dumps(dd, ensure_ascii=True, sort_keys=True),
                )
                total_direct_debits += 1

            try:
                standing_orders = client.get_standing_orders(access_token=access_token, account_id=account_id)
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "unknown"
                print(
                    f"Standing order fetch failed for user={user_id} account={account_id} "
                    f"(status={code}); continuing."
                )
                standing_orders = []
            for so in standing_orders:
                upsert_standing_order(
                    connection_id=connection_id,
                    provider_account_id=account_id,
                    standing_order=so,
                    raw_json=json.dumps(so, ensure_ascii=True, sort_keys=True),
                )
                total_standing_orders += 1

    print("Daily pull complete.")
    print(f"Connections processed: {len(connections)}")
    print(f"Accounts: {total_accounts}")
    print(f"Balance snapshots stored: {total_balances}")
    print(f"Transactions upserted: {total_transactions}")
    print(f"Direct debits upserted: {total_direct_debits}")
    print(f"Standing orders upserted: {total_standing_orders}")
    print(f"Transaction window: {tx_from.isoformat()} to {tx_to.isoformat()}")
    purge_result = purge_old_data(
        transaction_retention_days=tx_retention_days,
        balance_retention_days=balance_retention_days,
        direct_debit_retention_days=direct_debit_retention_days,
        standing_order_retention_days=standing_order_retention_days,
        advisory_log_retention_days=advisory_retention_days,
        raw_payload_retention_days=raw_payload_retention_days,
    )
    print(
        "Retention cleanup: "
        f"transactions={purge_result['transactions_deleted']}, "
        f"balance_snapshots={purge_result['balance_snapshots_deleted']}, "
        f"direct_debits={purge_result['direct_debits_deleted']}, "
        f"standing_orders={purge_result['standing_orders_deleted']}, "
        f"advisory_log={purge_result['advisory_log_deleted']}, "
        f"tx_payload_scrubbed={purge_result['transactions_payload_scrubbed']}, "
        f"dd_payload_scrubbed={purge_result['direct_debits_payload_scrubbed']}, "
        f"so_payload_scrubbed={purge_result['standing_orders_payload_scrubbed']}, "
        f"advisory_payload_scrubbed={purge_result['advisory_payload_scrubbed']}"
    )


if __name__ == "__main__":
    try:
        run_daily()
    except requests.HTTPError as exc:
        print(f"Provider request failed: {exc}")
        raise
