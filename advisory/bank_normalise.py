from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median, pstdev
from typing import Any

from dotenv import load_dotenv

from db import decrypt_db_text, get_conn, get_connection, init_db, resolve_effective_user_id


@dataclass(frozen=True)
class LabelResult:
    label: str
    confidence: float
    mandatory: bool
    severity: str


def _to_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


def _infer_cash_direction(amount: float, transaction_type: str | None) -> str:
    tx_type = _clean_text(transaction_type)
    if "credit" in tx_type or "in" in tx_type:
        return "inflow"
    if "debit" in tx_type or "card" in tx_type or "out" in tx_type:
        return "outflow"
    return "inflow" if amount > 0 else "outflow"


def _label_transaction(description: str, merchant_name: str, tx_category: str) -> LabelResult:
    text = " ".join([description, merchant_name, tx_category]).strip()

    keyword_rules: list[tuple[str, tuple[str, float, bool, str]]] = [
        ("salary", ("income", 0.95, False, "none")),
        ("payroll", ("income", 0.95, False, "none")),
        ("wage", ("income", 0.95, False, "none")),
        ("benefit", ("income", 0.9, False, "none")),
        ("rent", ("rent", 0.95, True, "critical")),
        ("landlord", ("rent", 0.9, True, "critical")),
        ("council tax", ("tax", 0.95, True, "high")),
        ("hmrc", ("tax", 0.9, True, "high")),
        ("tax", ("tax", 0.85, True, "high")),
        ("electric", ("utilities", 0.9, True, "high")),
        ("gas", ("utilities", 0.9, True, "high")),
        ("water", ("utilities", 0.9, True, "high")),
        ("broadband", ("utilities", 0.85, True, "medium")),
        ("internet", ("utilities", 0.85, True, "medium")),
        ("mobile", ("utilities", 0.8, True, "medium")),
        ("phone", ("utilities", 0.8, True, "medium")),
        ("credit card", ("debt", 0.9, True, "high")),
        ("loan", ("debt", 0.9, True, "high")),
        ("mortgage", ("debt", 0.95, True, "critical")),
        ("finance", ("debt", 0.8, True, "high")),
    ]

    for needle, rule in keyword_rules:
        if needle in text:
            return LabelResult(*rule)

    if text:
        return LabelResult("discretionary", 0.6, False, "low")
    return LabelResult("other", 0.4, False, "low")


def _fetch_accounts_with_latest_balances(connection_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        accounts = conn.execute(
            """
            SELECT provider_account_id, display_name, account_type, currency
            FROM bank_accounts
            WHERE connection_id = ?
            ORDER BY provider_account_id
        """,
            (connection_id,),
        ).fetchall()

        latest_balances = conn.execute(
            """
            SELECT provider_account_id, available, current, currency, captured_at
            FROM balance_snapshots
            WHERE connection_id = ?
            ORDER BY captured_at DESC
        """,
            (connection_id,),
        ).fetchall()

    latest_balance_map: dict[str, dict[str, Any]] = {}
    for row in latest_balances:
        account_id = row["provider_account_id"]
        if account_id in latest_balance_map:
            continue
        latest_balance_map[account_id] = {
            "available": row["available"],
            "current": row["current"],
            "currency": row["currency"],
            "captured_at": row["captured_at"],
        }

    result = []
    for row in accounts:
        account_id = row["provider_account_id"]
        bal = latest_balance_map.get(account_id, {})
        result.append(
            {
                "provider_account_id": account_id,
                "display_name": row["display_name"],
                "account_type": row["account_type"],
                "currency": row["currency"],
                "latest_balance": bal,
            }
        )
    return result


def _fetch_recent_transactions(connection_id: int, lookback_days: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT provider_account_id, provider_transaction_id, amount, currency, description,
                   merchant_name, transaction_type, transaction_category,
                   transaction_classification, timestamp, raw_json
            FROM transactions
            WHERE connection_id = ?
              AND (timestamp IS NULL OR timestamp >= ?)
            ORDER BY COALESCE(timestamp, '') DESC
        """,
            (connection_id, cutoff),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in [
            "description",
            "merchant_name",
            "transaction_type",
            "transaction_category",
            "transaction_classification",
            "raw_json",
        ]:
            item[field] = decrypt_db_text(item.get(field))
        result.append(item)
    return result


def _fetch_direct_debits(connection_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT provider_account_id, provider_direct_debit_id, status, payee_name, variable_amount,
                   next_payment_date, latest_amount, latest_currency
            FROM direct_debits
            WHERE connection_id = ?
        """,
            (connection_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in ["status", "payee_name", "variable_amount", "next_payment_date"]:
            item[field] = decrypt_db_text(item.get(field))
        result.append(item)
    return result


def _fetch_standing_orders(connection_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT provider_account_id, provider_standing_order_id, status, payee_name, frequency,
                   next_payment_date, next_payment_amount, next_payment_currency
            FROM standing_orders
            WHERE connection_id = ?
        """,
            (connection_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in ["status", "payee_name", "frequency", "next_payment_date"]:
            item[field] = decrypt_db_text(item.get(field))
        result.append(item)
    return result


def normalize_transactions(raw_transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tx in raw_transactions:
        amount = float(tx["amount"])
        direction = _infer_cash_direction(amount, tx.get("transaction_type"))
        signed_amount = abs(amount) if direction == "inflow" else -abs(amount)

        label = _label_transaction(
            description=_clean_text(tx.get("description")),
            merchant_name=_clean_text(tx.get("merchant_name")),
            tx_category=_clean_text(tx.get("transaction_category")),
        )

        counterparty = tx.get("merchant_name") or tx.get("description") or "unknown"
        posting_date = _to_iso_date(tx.get("timestamp"))

        normalized.append(
            {
                "transaction_id": tx["provider_transaction_id"],
                "provider_account_id": tx["provider_account_id"],
                "posting_date": posting_date,
                "signed_amount": round(signed_amount, 2),
                "absolute_amount": round(abs(amount), 2),
                "currency": tx.get("currency", "GBP"),
                "cash_direction": direction,
                "counterparty": counterparty,
                "description": tx.get("description"),
                "transaction_type": tx.get("transaction_type"),
                "intent_label": label.label,
                "label_confidence": label.confidence,
                "is_mandatory": label.mandatory,
                "severity": label.severity,
                "source_category": tx.get("transaction_category"),
                "source_classification": tx.get("transaction_classification"),
                "raw_json": json.loads(tx["raw_json"] or "{}"),
            }
        )
    return normalized


def _infer_frequency(interval_days: float) -> str | None:
    if 25 <= interval_days <= 35:
        return "monthly"
    if 12 <= interval_days <= 17:
        return "biweekly"
    if 6 <= interval_days <= 9:
        return "weekly"
    return None


def _priority_rank_for_severity(severity: str) -> int:
    return {"critical": 1, "high": 2, "medium": 3, "low": 4}.get(severity, 3)


def _dedupe_obligations(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}

    for item in obligations:
        key = (
            item.get("source", "unknown"),
            _clean_text(item.get("counterparty", "")),
            item.get("intent_label"),
            item.get("expected_due_date"),
            round(float(item.get("expected_amount", 0)), 2),
            item.get("currency", "GBP"),
        )
        if key not in grouped:
            current = dict(item)
            current["supporting_accounts"] = set(current.get("supporting_accounts") or [])
            grouped[key] = current
            continue

        existing = grouped[key]
        existing["confidence"] = max(
            float(existing.get("confidence", 0)),
            float(item.get("confidence", 0)),
        )
        existing["priority_rank"] = min(
            int(existing.get("priority_rank", 4)),
            int(item.get("priority_rank", 4)),
        )
        existing["due_in_days"] = min(
            int(existing.get("due_in_days", 999)),
            int(item.get("due_in_days", 999)),
        )
        existing_accounts = existing.get("supporting_accounts", set())
        new_accounts = set(item.get("supporting_accounts") or [])
        existing["supporting_accounts"] = existing_accounts.union(new_accounts)

    result: list[dict[str, Any]] = []
    for item in grouped.values():
        accounts = item.pop("supporting_accounts", set())
        item["supporting_account_count"] = len(accounts)
        result.append(item)

    result.sort(key=lambda x: (x["priority_rank"], x["due_in_days"]))
    return result


def _reliability_score(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return 0.5
    avg = sum(values) / len(values)
    if avg <= 0:
        return 0.0
    variation = pstdev(values) / avg if len(values) > 1 else 0
    return max(0.0, min(1.0, 1.0 - variation))


def infer_upcoming_obligations(normalized_transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tx in normalized_transactions:
        if tx["cash_direction"] != "outflow" or not tx["is_mandatory"]:
            continue
        if not tx["posting_date"]:
            continue
        key = f"{tx['intent_label']}::{_clean_text(tx['counterparty'])}"
        groups[key].append(tx)

    obligations: list[dict[str, Any]] = []
    today = date.today()
    for key, events in groups.items():
        if len(events) < 2:
            continue
        ordered = sorted(events, key=lambda item: item["posting_date"])
        dates = [date.fromisoformat(item["posting_date"]) for item in ordered]
        amounts = [item["absolute_amount"] for item in ordered]

        intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        if not intervals:
            continue
        median_interval = float(median(intervals))
        frequency = _infer_frequency(median_interval)
        if not frequency:
            continue

        due_date = dates[-1] + timedelta(days=round(median_interval))
        amount_expected = round(float(median(amounts)), 2)
        due_in_days = (due_date - today).days

        severity = ordered[-1]["severity"]
        severity_weight = {"critical": 1.0, "high": 0.85, "medium": 0.7, "low": 0.5}.get(
            severity, 0.5
        )
        amount_reliability = _reliability_score(amounts)
        timing_reliability = _reliability_score([abs(x - median_interval) for x in intervals])
        confidence = min(
            0.99,
            round(
                0.35 + (0.35 * amount_reliability) + (0.2 * timing_reliability) + (0.1 * severity_weight),
                2,
            ),
        )

        priority_rank = {"critical": 1, "high": 2, "medium": 3, "low": 4}.get(severity, 4)
        obligations.append(
            {
                "obligation_key": key,
                "intent_label": ordered[-1]["intent_label"],
                "counterparty": ordered[-1]["counterparty"],
                "frequency": frequency,
                "expected_due_date": due_date.isoformat(),
                "due_in_days": due_in_days,
                "expected_amount": amount_expected,
                "currency": ordered[-1]["currency"],
                "severity": severity,
                "priority_rank": priority_rank,
                "confidence": confidence,
            }
        )

    obligations.sort(key=lambda item: (item["priority_rank"], item["due_in_days"]))
    return obligations


def infer_obligations_from_payment_instruments(
    direct_debits: list[dict[str, Any]],
    standing_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    obligations: list[dict[str, Any]] = []
    today = date.today()

    for dd in direct_debits:
        status = _clean_text(dd.get("status"))
        if status and status not in {"active"}:
            continue
        payee = dd.get("payee_name") or "direct_debit_payee"
        label = _label_transaction(_clean_text(payee), _clean_text(payee), "direct_debit")
        due_date = _to_iso_date(dd.get("next_payment_date")) or today.isoformat()
        amount = dd.get("latest_amount")
        if amount is None:
            continue
        severity = label.severity if label.severity in {"critical", "high", "medium"} else "high"
        due_in_days = (date.fromisoformat(due_date) - today).days
        obligations.append(
            {
                "obligation_key": f"dd::{dd['provider_account_id']}::{dd['provider_direct_debit_id']}",
                "intent_label": label.label if label.label != "discretionary" else "utilities",
                "counterparty": payee,
                "frequency": "monthly",
                "expected_due_date": due_date,
                "due_in_days": due_in_days,
                "expected_amount": round(float(amount), 2),
                "currency": dd.get("latest_currency") or "GBP",
                "severity": severity,
                "priority_rank": _priority_rank_for_severity(severity),
                "confidence": 0.95,
                "source": "direct_debit",
                "supporting_accounts": [dd["provider_account_id"]],
            }
        )

    for so in standing_orders:
        status = _clean_text(so.get("status"))
        if status and status not in {"active"}:
            continue
        payee = so.get("payee_name") or "standing_order_payee"
        label = _label_transaction(_clean_text(payee), _clean_text(payee), "standing_order")
        due_date = _to_iso_date(so.get("next_payment_date")) or today.isoformat()
        amount = so.get("next_payment_amount")
        if amount is None:
            continue
        severity = label.severity if label.severity in {"critical", "high", "medium"} else "medium"
        due_in_days = (date.fromisoformat(due_date) - today).days
        obligations.append(
            {
                "obligation_key": f"so::{so['provider_account_id']}::{so['provider_standing_order_id']}",
                "intent_label": label.label if label.label != "discretionary" else "other",
                "counterparty": payee,
                "frequency": (so.get("frequency") or "monthly").lower(),
                "expected_due_date": due_date,
                "due_in_days": due_in_days,
                "expected_amount": round(float(amount), 2),
                "currency": so.get("next_payment_currency") or "GBP",
                "severity": severity,
                "priority_rank": _priority_rank_for_severity(severity),
                "confidence": 0.95,
                "source": "standing_order",
                "supporting_accounts": [so["provider_account_id"]],
            }
        )

    return _dedupe_obligations(obligations)


def infer_income_schedule(normalized_transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tx in normalized_transactions:
        if tx["intent_label"] != "income" or tx["cash_direction"] != "inflow":
            continue
        if not tx["posting_date"]:
            continue
        key = _clean_text(tx["counterparty"])
        groups[key].append(tx)

    incomes: list[dict[str, Any]] = []
    for key, events in groups.items():
        if len(events) < 2:
            continue
        ordered = sorted(events, key=lambda item: item["posting_date"])
        dates = [date.fromisoformat(item["posting_date"]) for item in ordered]
        amounts = [item["absolute_amount"] for item in ordered]
        intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        if not intervals:
            continue

        median_interval = float(median(intervals))
        frequency = _infer_frequency(median_interval)
        if not frequency:
            continue
        next_date = dates[-1] + timedelta(days=round(median_interval))

        confidence = min(
            0.99,
            round(
                0.4
                + (0.35 * _reliability_score(amounts))
                + (0.25 * _reliability_score([abs(x - median_interval) for x in intervals])),
                2,
            ),
        )
        incomes.append(
            {
                "source": key or "unknown",
                "frequency": frequency,
                "expected_date": next_date.isoformat(),
                "expected_amount": round(float(median(amounts)), 2),
                "currency": ordered[-1]["currency"],
                "confidence": confidence,
            }
        )

    incomes.sort(key=lambda item: item["expected_date"])
    return incomes


def infer_income_schedule_from_streams(
    normalized_transactions: list[dict[str, Any]],
    lookback_days: int = 120,
) -> list[dict[str, Any]]:
    today = date.today()
    cutoff = today - timedelta(days=lookback_days)

    signature_events: dict[tuple[str, float, str], list[dict[str, Any]]] = defaultdict(list)
    for tx in normalized_transactions:
        posting = tx.get("posting_date")
        if not posting or date.fromisoformat(posting) < cutoff:
            continue
        if tx.get("cash_direction") != "inflow":
            continue

        amount = round(float(tx.get("absolute_amount", 0)), 2)
        if amount <= 0:
            continue
        counterparty = _clean_text(tx.get("counterparty")) or "unknown"
        currency = tx.get("currency", "GBP")
        signature = (counterparty, amount, currency)
        signature_events[signature].append(tx)

    incomes: list[dict[str, Any]] = []
    for signature, events in signature_events.items():
        by_date_accounts: dict[date, set[str]] = defaultdict(set)
        for tx in events:
            d = date.fromisoformat(tx["posting_date"])
            by_date_accounts[d].add(tx["provider_account_id"])

        mirrored_dates = [d for d, accounts in by_date_accounts.items() if len(accounts) > 1]
        is_mirrored_signature = len(mirrored_dates) >= 2

        dates = sorted(by_date_accounts.keys())
        if len(dates) < 2:
            continue

        intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        median_interval = float(median(intervals))
        frequency = _infer_frequency(median_interval)
        if not frequency:
            continue

        stream_amount = signature[1]
        next_date = dates[-1] + timedelta(days=round(median_interval))
        timing_reliability = _reliability_score([abs(x - median_interval) for x in intervals])
        confidence = round(0.5 + (0.35 * timing_reliability), 2)
        if is_mirrored_signature:
            confidence = max(0.3, round(confidence - 0.1, 2))

        incomes.append(
            {
                "source": signature[0],
                "frequency": frequency,
                "expected_date": next_date.isoformat(),
                "expected_amount": stream_amount,
                "currency": signature[2],
                "confidence": min(0.95, confidence),
                "stream_type": "mirrored" if is_mirrored_signature else "direct",
                "stream_event_count": len(dates),
            }
        )

    incomes.sort(key=lambda item: item["expected_date"])
    return incomes


def infer_income_fallback(
    normalized_transactions: list[dict[str, Any]],
    accounts_summary: list[dict[str, Any]],
    lookback_days: int = 60,
) -> list[dict[str, Any]]:
    today = date.today()
    cutoff = today - timedelta(days=lookback_days)

    transaction_account_ids = {
        account["provider_account_id"]
        for account in accounts_summary
        if _clean_text(account.get("account_type")) == "transaction"
    }

    # Deduplicate likely mirrored sandbox inflows across accounts.
    unique_inflows: dict[tuple[Any, ...], float] = {}
    currencies: dict[str, float] = defaultdict(float)
    for tx in normalized_transactions:
        posting = tx.get("posting_date")
        if not posting or date.fromisoformat(posting) < cutoff:
            continue
        if tx.get("cash_direction") != "inflow":
            continue
        if transaction_account_ids and tx.get("provider_account_id") not in transaction_account_ids:
            continue

        amount = float(tx.get("absolute_amount", 0))
        dedupe_key = (
            posting,
            round(amount, 2),
            _clean_text(tx.get("counterparty", "")),
            tx.get("currency", "GBP"),
        )
        if dedupe_key not in unique_inflows:
            unique_inflows[dedupe_key] = amount

    inflow_total = sum(unique_inflows.values())
    for key, amount in unique_inflows.items():
        currencies[key[3]] += amount

    if inflow_total <= 0:
        return []

    monthly_estimate = round((inflow_total / max(1, lookback_days)) * 30, 2)
    if monthly_estimate < 100:
        return []

    dominant_currency = max(currencies, key=currencies.get)
    return [
        {
            "source": "inferred_fallback",
            "frequency": "monthly",
            "expected_date": (today + timedelta(days=30)).isoformat(),
            "expected_amount": monthly_estimate,
            "currency": dominant_currency,
            "confidence": 0.35,
            "fallback_method": "deduped_transaction_inflows",
        }
    ]


def build_risk_flags(
    accounts_summary: list[dict[str, Any]],
    upcoming_obligations: list[dict[str, Any]],
    income_schedule: list[dict[str, Any]],
    normalized_transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    available_total = 0.0
    for account in accounts_summary:
        latest = account.get("latest_balance", {})
        candidate = latest.get("available")
        if candidate is None:
            candidate = latest.get("current")
        if candidate is not None:
            available_total += float(candidate)

    today = date.today()
    horizon = today + timedelta(days=14)

    upcoming_outflows = sum(
        item["expected_amount"]
        for item in upcoming_obligations
        if today <= date.fromisoformat(item["expected_due_date"]) <= horizon
    )
    upcoming_inflows = sum(
        item["expected_amount"]
        for item in income_schedule
        if today <= date.fromisoformat(item["expected_date"]) <= horizon
    )
    projected_balance_14d = available_total + upcoming_inflows - upcoming_outflows

    last_30_days = today - timedelta(days=30)
    inflow_30d = 0.0
    outflow_30d = 0.0
    fixed_obligations_30d = 0.0
    for tx in normalized_transactions:
        posting = tx.get("posting_date")
        if not posting or date.fromisoformat(posting) < last_30_days:
            continue
        amount = tx["absolute_amount"]
        if tx["cash_direction"] == "inflow":
            inflow_30d += amount
        else:
            outflow_30d += amount
            if tx["is_mandatory"]:
                fixed_obligations_30d += amount

    fixed_outflow_ratio = fixed_obligations_30d / inflow_30d if inflow_30d > 0 else 1.0

    flags: list[dict[str, Any]] = []
    if available_total < 200:
        flags.append(
            {
                "flag": "low_cash_buffer",
                "severity": "high",
                "detail": f"Available balance is {available_total:.2f}.",
            }
        )
    if projected_balance_14d < 0:
        flags.append(
            {
                "flag": "negative_projected_balance",
                "severity": "critical",
                "detail": f"Projected 14-day balance is {projected_balance_14d:.2f}.",
            }
        )
    if fixed_outflow_ratio > 0.7:
        flags.append(
            {
                "flag": "high_fixed_outflow_ratio",
                "severity": "medium",
                "detail": f"Fixed outflow ratio is {fixed_outflow_ratio:.2f} over last 30 days.",
            }
        )
    has_only_fallback_income = bool(income_schedule) and all(
        item.get("source") == "inferred_fallback" for item in income_schedule
    )
    has_low_confidence_income = bool(income_schedule) and max(
        float(item.get("confidence", 0.0)) for item in income_schedule
    ) < 0.6
    mirrored_income_ratio = 0.0
    if income_schedule:
        mirrored_count = sum(1 for item in income_schedule if item.get("stream_type") == "mirrored")
        mirrored_income_ratio = mirrored_count / len(income_schedule)
    if not income_schedule or has_only_fallback_income or has_low_confidence_income:
        flags.append(
            {
                "flag": "income_pattern_uncertain",
                "severity": "medium",
                "detail": "No reliable recurring income pattern detected from recent transactions.",
            }
        )
    if mirrored_income_ratio >= 0.6:
        flags.append(
            {
                "flag": "income_streams_mirrored",
                "severity": "medium",
                "detail": (
                    f"{mirrored_income_ratio:.0%} of detected income streams appear mirrored across accounts; "
                    "treat projected inflows with caution."
                ),
            }
        )

    return flags


def build_normalized_payload(
    user_id: str,
    provider: str = "truelayer",
    lookback_days: int = 120,
) -> dict[str, Any]:
    connection = get_connection(user_id=user_id, provider=provider)
    if not connection:
        raise ValueError("No connected bank found for given user/provider.")

    connection_id = int(connection["id"])
    accounts_summary = _fetch_accounts_with_latest_balances(connection_id=connection_id)
    direct_debits = _fetch_direct_debits(connection_id=connection_id)
    standing_orders = _fetch_standing_orders(connection_id=connection_id)
    raw_transactions = _fetch_recent_transactions(
        connection_id=connection_id, lookback_days=lookback_days
    )
    normalized_transactions = normalize_transactions(raw_transactions)
    instrument_obligations = infer_obligations_from_payment_instruments(
        direct_debits=direct_debits,
        standing_orders=standing_orders,
    )
    tx_inferred_obligations = infer_upcoming_obligations(normalized_transactions)
    if instrument_obligations:
        upcoming_obligations = instrument_obligations
    else:
        upcoming_obligations = _dedupe_obligations(tx_inferred_obligations)
    income_schedule = infer_income_schedule(normalized_transactions)
    if not income_schedule:
        income_schedule = infer_income_schedule_from_streams(normalized_transactions)
    if not income_schedule:
        income_schedule = infer_income_fallback(
            normalized_transactions=normalized_transactions,
            accounts_summary=accounts_summary,
        )
    risk_flags = build_risk_flags(
        accounts_summary=accounts_summary,
        upcoming_obligations=upcoming_obligations,
        income_schedule=income_schedule,
        normalized_transactions=normalized_transactions,
    )

    net_flow_30d = sum(
        tx["signed_amount"]
        for tx in normalized_transactions
        if tx["posting_date"]
        and date.fromisoformat(tx["posting_date"]) >= (date.today() - timedelta(days=30))
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "provider": provider,
        "lookback_days": lookback_days,
        "accounts_summary": accounts_summary,
        "upcoming_obligations": upcoming_obligations,
        "income_schedule": income_schedule,
        "risk_flags": risk_flags,
        "metrics": {
            "transaction_count": len(normalized_transactions),
            "obligation_count": len(upcoming_obligations),
            "income_stream_count": len(income_schedule),
            "net_flow_last_30_days": round(net_flow_30d, 2),
            "direct_debit_count": len(direct_debits),
            "standing_order_count": len(standing_orders),
            "obligation_source": "payment_instruments"
            if instrument_obligations
            else "transaction_inference",
        },
        "normalized_transactions": normalized_transactions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize bank data for advisory engine.")
    parser.add_argument("--user-id", help="User ID to run normalization for.")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only compact summary fields instead of full normalized payload JSON.",
    )
    args = parser.parse_args()

    load_dotenv()
    init_db()
    try:
        user_id = resolve_effective_user_id(args.user_id or os.getenv("LOCAL_USER_ID"))
    except ValueError as exc:
        print(f"{exc} Example: python advisory/bank_normalise.py --user-id <uuid>")
        return
    provider = os.getenv("OPEN_BANKING_PROVIDER", "truelayer")
    lookback_days = int(os.getenv("NORMALISE_LOOKBACK_DAYS", "120"))

    try:
        payload = build_normalized_payload(
            user_id=user_id,
            provider=provider,
            lookback_days=lookback_days,
        )
    except ValueError as exc:
        print(f"{exc} Run advisory/app.py to connect and advisory/run_daily.py to ingest data.")
        return

    if args.summary_only:
        summary = {
            "generated_at": payload["generated_at"],
            "user_id": payload["user_id"],
            "provider": payload["provider"],
            "lookback_days": payload["lookback_days"],
            "metrics": payload["metrics"],
            "risk_flags": payload["risk_flags"],
            "upcoming_obligations_count": len(payload["upcoming_obligations"]),
            "income_schedule_count": len(payload["income_schedule"]),
            "sample_upcoming_obligations": payload["upcoming_obligations"][:5],
            "sample_income_schedule": payload["income_schedule"][:5],
        }
        print(json.dumps(summary, indent=2, ensure_ascii=True))
        return

    print(json.dumps(payload, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
