from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from bank_normalise import build_normalized_payload
from db import default_user_preferences, get_user_preferences, init_db, list_connections, resolve_effective_user_id


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def _last_monday(year: int, month: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    return d


def _uk_bank_holidays(year: int) -> set[date]:
    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)
    easter_monday = easter + timedelta(days=1)

    new_year = date(year, 1, 1)
    if new_year.weekday() == 5:  # Saturday
        new_year_substitute = date(year, 1, 3)
    elif new_year.weekday() == 6:  # Sunday
        new_year_substitute = date(year, 1, 2)
    else:
        new_year_substitute = new_year

    christmas = date(year, 12, 25)
    boxing_day = date(year, 12, 26)
    if christmas.weekday() == 5:  # Saturday
        christmas_substitute = date(year, 12, 27)
        boxing_substitute = date(year, 12, 28)
    elif christmas.weekday() == 6:  # Sunday
        christmas_substitute = date(year, 12, 27)
        boxing_substitute = date(year, 12, 26) if boxing_day.weekday() != 6 else date(year, 12, 28)
    elif boxing_day.weekday() == 5:  # Saturday
        christmas_substitute = christmas
        boxing_substitute = date(year, 12, 28)
    elif boxing_day.weekday() == 6:  # Sunday
        christmas_substitute = christmas
        boxing_substitute = date(year, 12, 27)
    else:
        christmas_substitute = christmas
        boxing_substitute = boxing_day

    return {
        new_year_substitute,
        good_friday,
        easter_monday,
        _first_monday(year, 5),   # Early May bank holiday
        _last_monday(year, 5),    # Spring bank holiday
        _last_monday(year, 8),    # Summer bank holiday
        christmas_substitute,
        boxing_substitute,
    }


def _is_uk_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _uk_bank_holidays(d.year)


def _previous_uk_business_day(d: date) -> date:
    candidate = d
    while not _is_uk_business_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _next_uk_business_day(d: date) -> date:
    candidate = d
    while not _is_uk_business_day(candidate):
        candidate += timedelta(days=1)
    return candidate


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
    execution_date: str,
    cash_after: float,
    reserve_floor: float,
    user_preferences: dict[str, Any],
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
        due_date = obligation.get("expected_due_date")
        if due_date and execution_date and execution_date != due_date:
            reasons.append(f"Scheduled on business day: {execution_date}.")
    elif action == "PARTIAL_PAY":
        reasons.append("Insufficient liquid cash to cover full amount.")
    else:
        reasons.append("Payment held to avoid destabilizing critical obligations.")
    reasons.append(
        f"Profile: payment_style={user_preferences['payment_style']}, "
        f"risk_tolerance={user_preferences['risk_tolerance']}, "
        f"buffer_preference={user_preferences['buffer_preference']}"
    )
    if user_preferences.get("card_strategy"):
        reasons.append(f"Card strategy={user_preferences['card_strategy']}.")
    reasons.append(f"Projected cash after action={cash_after:.2f} (reserve floor={reserve_floor:.2f})")
    return reasons


def _reserve_floor(
    monthly_income_estimate: float,
    user_preferences: dict[str, Any],
) -> float:
    base_floor = max(200.0, round(monthly_income_estimate * 0.10, 2))
    override = user_preferences.get("reserve_floor")
    if override is not None:
        return max(0.0, round(float(override), 2))
    multiplier = {
        "minimise_idle_cash": 0.85,
        "moderate_buffer": 1.0,
        "high_buffer": 1.35,
    }.get(str(user_preferences.get("buffer_preference", "moderate_buffer")), 1.0)
    return max(200.0, round(base_floor * multiplier, 2))


def _urgency_rule(due_in_days: int, severity_val: int, risk_tolerance: str) -> bool:
    if risk_tolerance == "low":
        return due_in_days <= 2 or (severity_val >= 3 and due_in_days <= 4)
    if risk_tolerance == "high":
        return due_in_days <= 0 or (severity_val >= 3 and due_in_days <= 2)
    return due_in_days <= 1 or (severity_val >= 3 and due_in_days <= 3)


def _latest_safe_execution_date(due_date: date, business_calendar: str) -> date:
    latest = due_date - timedelta(days=1)
    if business_calendar == "uk":
        latest = _previous_uk_business_day(latest)
    return latest


def _choose_wait_date(
    *,
    today: date,
    due_date: date,
    business_calendar: str,
    payment_style: str,
) -> date:
    latest_safe = _latest_safe_execution_date(due_date, business_calendar)
    if latest_safe <= today:
        return today

    earliest_safe = today
    if business_calendar == "uk":
        earliest_safe = _next_uk_business_day(earliest_safe)
        if earliest_safe > latest_safe:
            return latest_safe

    if payment_style == "conservative":
        candidate = earliest_safe
    elif payment_style == "last_safe_moment":
        candidate = latest_safe
    else:
        span_days = (latest_safe - earliest_safe).days
        midpoint = earliest_safe + timedelta(days=max(0, span_days // 2))
        candidate = midpoint

    if business_calendar == "uk":
        if candidate > latest_safe:
            candidate = latest_safe
        candidate = _previous_uk_business_day(candidate)
        if candidate < earliest_safe:
            candidate = earliest_safe
    return candidate


def generate_advisories(
    normalized_payload: dict[str, Any],
    days_ahead: int = 14,
    user_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prefs = default_user_preferences()
    if user_preferences:
        prefs.update(user_preferences)
    today = date.today()
    obligations = [
        item
        for item in normalized_payload["upcoming_obligations"]
        if item["due_in_days"] <= days_ahead
    ]
    obligations.sort(key=lambda x: (x["priority_rank"], x["due_in_days"]))
    business_calendar = os.getenv("BUSINESS_CALENDAR", "uk").lower().strip()

    available_total = _available_total(normalized_payload["accounts_summary"])
    monthly_income_estimate = _annualized_income(normalized_payload["income_schedule"])
    reserve_floor = _reserve_floor(monthly_income_estimate, prefs)
    allocatable_cash = max(0.0, available_total - reserve_floor)

    decisions: list[dict[str, Any]] = []
    for obligation in obligations:
        amount = float(obligation["expected_amount"])
        due_date = date.fromisoformat(obligation["expected_due_date"])
        due_in_days = int(obligation["due_in_days"])
        severity = obligation["severity"]

        severity_val = _severity_score(severity)
        urgent = _urgency_rule(due_in_days, severity_val, str(prefs["risk_tolerance"]))
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
                target_date = _choose_wait_date(
                    today=today,
                    due_date=due_date,
                    business_calendar=business_calendar,
                    payment_style=str(prefs["payment_style"]),
                )
                execution_date = target_date.isoformat()
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
                    execution_date=execution_date,
                    cash_after=cash_after_action,
                    reserve_floor=reserve_floor,
                    user_preferences=prefs,
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
        "preferences_applied": prefs,
        "risk_flags": risk_flags,
        "decisions": decisions,
    }


def run_engine(
    user_id: str,
    provider: str = "truelayer",
    normalise_lookback_days: int = 120,
    advisory_window_days: int = 14,
) -> dict[str, Any]:
    user_preferences = get_user_preferences(user_id)
    normalized_payload = build_normalized_payload(
        user_id=user_id,
        provider=provider,
        lookback_days=normalise_lookback_days,
    )
    return generate_advisories(
        normalized_payload=normalized_payload,
        days_ahead=advisory_window_days,
        user_preferences=user_preferences,
    )


def _connected_user_ids(provider: str) -> list[str]:
    return sorted({row["user_id"] for row in list_connections(provider=provider)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate payment advisory decisions.")
    parser.add_argument("--user-id", help="User ID to run advisory engine for.")
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Run advisory engine for every connected user.",
    )
    args = parser.parse_args()
    if args.user_id and args.all_users:
        print("Use either --user-id or --all-users, not both.")
        return

    load_dotenv()
    init_db()

    provider = os.getenv("OPEN_BANKING_PROVIDER", "truelayer")
    lookback_days = int(os.getenv("NORMALISE_LOOKBACK_DAYS", "120"))
    advisory_window_days = int(os.getenv("ADVISORY_WINDOW_DAYS", "14"))
    if args.all_users:
        user_ids = _connected_user_ids(provider)
        if not user_ids:
            print("No connected bank users found.")
            return
    else:
        try:
            user_ids = [resolve_effective_user_id(args.user_id or os.getenv("LOCAL_USER_ID"))]
        except ValueError as exc:
            print(f"{exc} Example: python advisory/engine.py --user-id <uuid>")
            return

    if len(user_ids) == 1:
        try:
            result = run_engine(
                user_id=user_ids[0],
                provider=provider,
                normalise_lookback_days=lookback_days,
                advisory_window_days=advisory_window_days,
            )
        except ValueError as exc:
            print(f"{exc} Run advisory/app.py then advisory/run_daily.py first.")
            return
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return

    results: list[dict[str, Any]] = []
    for user_id in user_ids:
        try:
            result = run_engine(
                user_id=user_id,
                provider=provider,
                normalise_lookback_days=lookback_days,
                advisory_window_days=advisory_window_days,
            )
            results.append(result)
        except ValueError as exc:
            results.append({"user_id": user_id, "error": str(exc)})

    print(
        json.dumps(
            {
                "provider": provider,
                "user_count": len(user_ids),
                "results": results,
            },
            indent=2,
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
