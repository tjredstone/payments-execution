from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from bank_normalise import build_normalized_payload
from db import init_db


def _annualized_income(income_schedule: list[dict[str, Any]]) -> float:
    per_month = 0.0
    for income in income_schedule:
        amount = float(income.get("expected_amount", 0))
        frequency = income.get("frequency")
        if frequency == "monthly":
            per_month += amount
        elif frequency == "biweekly":
            per_month += amount * 2
        elif frequency == "weekly":
            per_month += amount * 4
    return per_month


def _available_total(accounts_summary: list[dict[str, Any]]) -> float:
    total = 0.0
    for account in accounts_summary:
        latest = account.get("latest_balance") or {}
        available = latest.get("available")
        current = latest.get("current")
        if available is not None:
            total += float(available)
        elif current is not None:
            total += float(current)
    return round(total, 2)


def _severity_score(severity: str) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(severity, 1)


def _build_rationale(
    obligation: dict[str, Any],
    action: str,
    cash_after: float,
    reserve_floor: float,
) -> list[str]:
    due_in = int(obligation["due_in_days"])
    severity = obligation["severity"]
    reasons = [
        f"Priority={obligation['priority_rank']} ({severity})",
        f"Due in {due_in} day(s)",
        f"Expected amount={obligation['expected_amount']:.2f}",
    ]
    if action == "PAY_NOW":
        reasons.append("Urgency/risk threshold reached.")
    elif action == "WAIT":
        reasons.append("Safe to defer while preserving liquidity.")
    elif action == "PARTIAL_PAY":
        reasons.append("Insufficient liquid cash to cover full amount.")
    else:
        reasons.append("Payment held to avoid destabilizing critical obligations.")
    reasons.append(f"Projected cash after action={cash_after:.2f} (reserve floor={reserve_floor:.2f})")
    return reasons


def generate_advisories(
    normalized_payload: dict[str, Any],
    days_ahead: int = 14,
) -> dict[str, Any]:
    today = date.today()
    obligations = [
        item
        for item in normalized_payload["upcoming_obligations"]
        if item["due_in_days"] <= days_ahead
    ]
    obligations.sort(key=lambda x: (x["priority_rank"], x["due_in_days"]))

    available_total = _available_total(normalized_payload["accounts_summary"])
    monthly_income_estimate = _annualized_income(normalized_payload["income_schedule"])
    reserve_floor = max(200.0, round(monthly_income_estimate * 0.10, 2))
    allocatable_cash = max(0.0, available_total - reserve_floor)

    decisions: list[dict[str, Any]] = []
    for obligation in obligations:
        amount = float(obligation["expected_amount"])
        due_date = date.fromisoformat(obligation["expected_due_date"])
        due_in_days = int(obligation["due_in_days"])
        severity = obligation["severity"]

        severity_val = _severity_score(severity)
        urgent = due_in_days <= 1 or (severity_val >= 3 and due_in_days <= 3)
        past_due = due_in_days < 0

        recommended_amount = 0.0
        action = "HOLD_AND_ALERT"
        execution_date = today.isoformat()

        if allocatable_cash >= amount:
            recommended_amount = amount
            allocatable_cash = round(allocatable_cash - amount, 2)
            if urgent or past_due:
                action = "PAY_NOW"
                execution_date = today.isoformat()
            else:
                action = "WAIT"
                execution_date = max(today, due_date - timedelta(days=1)).isoformat()
        elif allocatable_cash > 0:
            recommended_amount = round(allocatable_cash, 2)
            allocatable_cash = 0.0
            action = "PARTIAL_PAY"
            execution_date = today.isoformat()

        cash_after_action = round(available_total - recommended_amount, 2)
        decisions.append(
            {
                "obligation_key": obligation["obligation_key"],
                "counterparty": obligation["counterparty"],
                "intent_label": obligation["intent_label"],
                "severity": severity,
                "due_date": obligation["expected_due_date"],
                "due_in_days": due_in_days,
                "action": action,
                "recommended_execution_date": execution_date,
                "recommended_amount": round(recommended_amount, 2),
                "currency": obligation["currency"],
                "confidence": obligation["confidence"],
                "rationale": _build_rationale(
                    obligation=obligation,
                    action=action,
                    cash_after=cash_after_action,
                    reserve_floor=reserve_floor,
                ),
            }
        )

    risk_flags = normalized_payload.get("risk_flags", [])
    critical_risk_count = sum(1 for flag in risk_flags if flag.get("severity") == "critical")
    high_risk_count = sum(1 for flag in risk_flags if flag.get("severity") == "high")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days_ahead,
        "summary": {
            "available_total": available_total,
            "monthly_income_estimate": round(monthly_income_estimate, 2),
            "reserve_floor": reserve_floor,
            "allocatable_cash_remaining": round(allocatable_cash, 2),
            "obligations_in_window": len(obligations),
            "decisions": len(decisions),
            "critical_risk_count": critical_risk_count,
            "high_risk_count": high_risk_count,
        },
        "risk_flags": risk_flags,
        "decisions": decisions,
    }


def run_engine(
    user_id: str,
    provider: str = "truelayer",
    normalise_lookback_days: int = 120,
    advisory_window_days: int = 14,
) -> dict[str, Any]:
    normalized_payload = build_normalized_payload(
        user_id=user_id,
        provider=provider,
        lookback_days=normalise_lookback_days,
    )
    return generate_advisories(
        normalized_payload=normalized_payload,
        days_ahead=advisory_window_days,
    )


def main() -> None:
    load_dotenv()
    init_db()

    user_id = os.getenv("LOCAL_USER_ID", "local-dev-user")
    provider = os.getenv("OPEN_BANKING_PROVIDER", "truelayer")
    lookback_days = int(os.getenv("NORMALISE_LOOKBACK_DAYS", "120"))
    advisory_window_days = int(os.getenv("ADVISORY_WINDOW_DAYS", "14"))

    try:
        result = run_engine(
            user_id=user_id,
            provider=provider,
            normalise_lookback_days=lookback_days,
            advisory_window_days=advisory_window_days,
        )
    except ValueError as exc:
        print(f"{exc} Run advisory/app.py then advisory/run_daily.py first.")
        return

    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
