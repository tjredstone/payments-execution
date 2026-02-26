from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from db import get_conn, init_db


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _clean(value: str | None) -> str:
    return (value or "").strip().lower()


def _fetch_logged_advisories(user_id: str, provider: str, lookback_days: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT payload_json, action, counterparty, due_date, amount, created_at
            FROM advisory_log
            WHERE user_id = ? AND provider = ? AND created_at >= ?
            ORDER BY created_at ASC
        """,
            (user_id, provider, cutoff),
        ).fetchall()

    advisories: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload.setdefault("action", row["action"])
        payload.setdefault("counterparty", row["counterparty"])
        payload.setdefault("due_date", row["due_date"])
        payload.setdefault("recommended_amount", row["amount"])
        payload.setdefault("created_at", row["created_at"])
        advisories.append(payload)
    return advisories


def _fetch_transactions(user_id: str, provider: str, lookback_days: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.amount, t.currency, t.description, t.merchant_name, t.timestamp
            FROM transactions t
            JOIN bank_connections bc ON bc.id = t.connection_id
            WHERE bc.user_id = ? AND bc.provider = ?
              AND COALESCE(t.timestamp, t.updated_at) >= ?
            ORDER BY COALESCE(t.timestamp, t.updated_at) ASC
        """,
            (user_id, provider, cutoff),
        ).fetchall()
    return [dict(row) for row in rows]


def _is_outflow(amount: float) -> bool:
    return float(amount) < 0


def _match_transaction(
    advisory: dict[str, Any],
    transactions: list[dict[str, Any]],
    amount_tolerance: float,
    day_window: int,
) -> dict[str, Any] | None:
    target_due = _to_date(advisory.get("due_date"))
    if not target_due:
        return None

    target_counterparty = _clean(advisory.get("counterparty"))
    target_amount = abs(float(advisory.get("recommended_amount", 0)))
    start = target_due - timedelta(days=day_window)
    end = target_due + timedelta(days=day_window)

    candidates: list[dict[str, Any]] = []
    for tx in transactions:
        tx_date = _to_date(tx.get("timestamp"))
        if not tx_date or tx_date < start or tx_date > end:
            continue
        amount = float(tx.get("amount", 0))
        if not _is_outflow(amount):
            continue
        if abs(abs(amount) - target_amount) > amount_tolerance:
            continue
        text = _clean(tx.get("merchant_name")) + " " + _clean(tx.get("description"))
        if target_counterparty and target_counterparty not in text:
            continue
        candidates.append(tx)

    if not candidates:
        return None
    candidates.sort(key=lambda tx: _to_date(tx.get("timestamp")) or date.max)
    return candidates[0]


def build_backtest_report(
    user_id: str,
    provider: str,
    lookback_days: int = 120,
    amount_tolerance: float = 1.0,
    day_window: int = 5,
) -> dict[str, Any]:
    advisories = _fetch_logged_advisories(user_id, provider, lookback_days)
    transactions = _fetch_transactions(user_id, provider, lookback_days)

    today = date.today()
    evaluable = [a for a in advisories if (_to_date(a.get("due_date")) or today) <= today]

    matched_on_time = 0
    matched_late = 0
    unmatched = 0

    action_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []

    for advisory in evaluable:
        action = str(advisory.get("action", "UNKNOWN"))
        action_counts[action] = action_counts.get(action, 0) + 1

        tx = _match_transaction(
            advisory=advisory,
            transactions=transactions,
            amount_tolerance=amount_tolerance,
            day_window=day_window,
        )
        due_date = _to_date(advisory.get("due_date"))
        if not due_date:
            unmatched += 1
            continue

        status = "unmatched"
        matched_date: str | None = None
        if tx:
            tx_date = _to_date(tx.get("timestamp"))
            matched_date = tx_date.isoformat() if tx_date else None
            if tx_date and tx_date <= due_date:
                matched_on_time += 1
                status = "matched_on_time"
            else:
                matched_late += 1
                status = "matched_late"
        else:
            unmatched += 1

        if len(samples) < 10:
            samples.append(
                {
                    "action": action,
                    "counterparty": advisory.get("counterparty"),
                    "due_date": advisory.get("due_date"),
                    "recommended_amount": advisory.get("recommended_amount"),
                    "match_status": status,
                    "matched_transaction_date": matched_date,
                }
            )

    total = len(evaluable)
    on_time_rate = round((matched_on_time / total), 3) if total else 0.0
    late_rate = round((matched_late / total), 3) if total else 0.0
    unmatched_rate = round((unmatched / total), 3) if total else 0.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": lookback_days,
        "user_id": user_id,
        "provider": provider,
        "summary": {
            "advisories_logged": len(advisories),
            "advisories_evaluable": total,
            "matched_on_time": matched_on_time,
            "matched_late": matched_late,
            "unmatched": unmatched,
            "on_time_rate": on_time_rate,
            "late_rate": late_rate,
            "unmatched_rate": unmatched_rate,
        },
        "actions": action_counts,
        "samples": samples,
        "caveat": (
            "Matching is heuristic (counterparty text + amount window). "
            "Use this as directional validation, not accounting-grade truth."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest advisory decisions against observed transactions.")
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--amount-tolerance", type=float, default=1.0)
    parser.add_argument("--day-window", type=int, default=5)
    args = parser.parse_args()

    load_dotenv()
    init_db()
    user_id = os.getenv("LOCAL_USER_ID", "local-dev-user")
    provider = os.getenv("OPEN_BANKING_PROVIDER", "truelayer")

    report = build_backtest_report(
        user_id=user_id,
        provider=provider,
        lookback_days=args.lookback_days,
        amount_tolerance=args.amount_tolerance,
        day_window=args.day_window,
    )
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
