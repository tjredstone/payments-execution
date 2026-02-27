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


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _freshness_score_from_age_days(age_days: float) -> float:
    if age_days <= 0.5:
        return 1.0
    if age_days <= 1:
        return 0.9
    if age_days <= 2:
        return 0.75
    if age_days <= 3:
        return 0.6
    if age_days <= 7:
        return 0.4
    return 0.2


def _freshness_contributor(normalized_payload: dict[str, Any], today: date) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)

    balance_timestamps: list[datetime] = []
    for account in normalized_payload.get("accounts_summary", []):
        captured_at = (account.get("latest_balance") or {}).get("captured_at")
        if not captured_at:
            continue
        try:
            balance_timestamps.append(datetime.fromisoformat(str(captured_at).replace("Z", "+00:00")))
        except ValueError:
            continue

    latest_balance_age_days = None
    balance_score = 0.5
    if balance_timestamps:
        latest_balance_dt = max(balance_timestamps)
        latest_balance_age_days = max(0.0, (now_dt - latest_balance_dt).total_seconds() / 86400)
        balance_score = _freshness_score_from_age_days(latest_balance_age_days)

    posting_dates: list[date] = []
    for tx in normalized_payload.get("normalized_transactions", []):
        posting = tx.get("posting_date")
        if not posting:
            continue
        try:
            posting_dates.append(date.fromisoformat(str(posting)))
        except ValueError:
            continue

    latest_tx_age_days = None
    tx_score = 0.5
    if posting_dates:
        latest_tx_date = max(posting_dates)
        latest_tx_age_days = max(0.0, float((today - latest_tx_date).days))
        tx_score = _freshness_score_from_age_days(latest_tx_age_days)

    score = round(_clamp01((balance_score * 0.6) + (tx_score * 0.4)), 3)
    return {
        "score": score,
        "latest_balance_age_days": round(latest_balance_age_days, 2) if latest_balance_age_days is not None else None,
        "latest_transaction_age_days": round(latest_tx_age_days, 2) if latest_tx_age_days is not None else None,
    }


def _volatility_contributor(normalized_payload: dict[str, Any], today: date) -> dict[str, Any]:
    start = today - timedelta(days=30)
    outflow_by_day: dict[date, float] = {}
    for tx in normalized_payload.get("normalized_transactions", []):
        posting = tx.get("posting_date")
        if not posting:
            continue
        try:
            d = date.fromisoformat(str(posting))
        except ValueError:
            continue
        if d < start or d > today:
            continue
        amount = float(tx.get("signed_amount") or 0.0)
        if amount >= 0:
            continue
        outflow_by_day[d] = outflow_by_day.get(d, 0.0) + abs(amount)

    values = list(outflow_by_day.values())
    if len(values) < 5:
        return {"score": 0.65, "daily_outflow_cv": None, "sample_days": len(values)}

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    stddev = variance ** 0.5
    cv = (stddev / mean) if mean > 0 else 0.0

    if cv <= 0.35:
        score = 0.95
    elif cv <= 0.70:
        score = 0.80
    elif cv <= 1.00:
        score = 0.65
    elif cv <= 1.50:
        score = 0.50
    else:
        score = 0.35

    return {
        "score": round(score, 3),
        "daily_outflow_cv": round(cv, 3),
        "sample_days": len(values),
    }


def _decision_confidence(base_confidence: float, freshness_score: float, volatility_score: float) -> float:
    return round(
        _clamp01((0.60 * _clamp01(base_confidence)) + (0.25 * _clamp01(freshness_score)) + (0.15 * _clamp01(volatility_score))),
        3,
    )


def _build_rationale(
    obligation: dict[str, Any],
    action: str,
    execution_date: str,
    cash_after: float,
    reserve_floor: float,
    user_preferences: dict[str, Any],
    forecast_assessment: dict[str, Any] | None = None,
    confidence_assessment: dict[str, Any] | None = None,
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
    if forecast_assessment:
        wait_hard = bool(forecast_assessment.get("wait_breaches_hard_floor"))
        wait_reserve = bool(forecast_assessment.get("wait_breaches_reserve_floor"))
        now_hard = bool(forecast_assessment.get("pay_now_breaches_hard_floor"))
        reasons.append(
            "Forecast check: "
            f"WAIT(hard={wait_hard}, reserve={wait_reserve}) vs "
            f"PAY_NOW(hard={now_hard})."
        )
    if confidence_assessment:
        reasons.append(
            "Confidence: "
            f"overall={confidence_assessment.get('overall_confidence')}, "
            f"freshness={confidence_assessment.get('freshness_score')}, "
            f"volatility={confidence_assessment.get('volatility_score')}."
        )
        if confidence_assessment.get("note"):
            reasons.append(str(confidence_assessment.get("note")))
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


def _subtract_business_days(value: date, days: int, business_calendar: str) -> date:
    if days <= 0:
        return value
    candidate = value
    remaining = days
    while remaining > 0:
        candidate -= timedelta(days=1)
        if business_calendar == "uk":
            if _is_uk_business_day(candidate):
                remaining -= 1
        else:
            if candidate.weekday() < 5:
                remaining -= 1
    return candidate


def _rail_type_for_obligation(obligation: dict[str, Any]) -> str:
    source = str(obligation.get("source") or "").strip().lower()
    if source == "direct_debit":
        return "direct_debit"
    intent = str(obligation.get("intent_label") or "").strip().lower()
    if intent == "debt":
        return "card_manual"
    return "bank_transfer"


def _latest_safe_for_obligation(
    due_date: date,
    rail_type: str,
    business_calendar: str,
) -> date:
    buffer_days = {
        "bank_transfer": 1,
        "card_manual": 2,
        "direct_debit": 1,
    }.get(rail_type, 1)
    return _subtract_business_days(due_date, buffer_days, business_calendar)


def _income_event_buckets(
    income_schedule: list[dict[str, Any]],
    start_day: date,
    end_day: date,
) -> dict[date, float]:
    events: dict[date, float] = {}
    for income in income_schedule:
        amount = float(income.get("expected_amount") or 0)
        if amount <= 0:
            continue
        expected_date_raw = str(income.get("expected_date") or "").strip()
        if not expected_date_raw:
            continue
        try:
            current = date.fromisoformat(expected_date_raw)
        except ValueError:
            continue

        frequency = str(income.get("frequency") or "").lower().strip()
        delta_days = {"weekly": 7, "biweekly": 14, "monthly": 30}.get(frequency, 0)
        if delta_days <= 0:
            if start_day <= current <= end_day:
                events[current] = round(events.get(current, 0.0) + amount, 2)
            continue

        while current < start_day:
            current += timedelta(days=delta_days)
        while current <= end_day:
            events[current] = round(events.get(current, 0.0) + amount, 2)
            current += timedelta(days=delta_days)
    return events


def _simulate_horizon(
    *,
    start_balance: float,
    start_day: date,
    horizon_days: int,
    inflows_by_day: dict[date, float],
    outflows_by_day: dict[date, float],
    reserve_floor: float,
    hard_floor: float = 0.0,
) -> dict[str, Any]:
    balance = float(start_balance)
    min_balance = balance
    min_balance_day = start_day
    hard_breach = False
    reserve_breach = False
    hard_breach_day: str | None = None
    reserve_breach_day: str | None = None

    for offset in range(0, horizon_days + 1):
        day = start_day + timedelta(days=offset)
        inflow = float(inflows_by_day.get(day, 0.0))
        outflow = float(outflows_by_day.get(day, 0.0))
        balance = round(balance + inflow - outflow, 2)
        if balance < min_balance:
            min_balance = balance
            min_balance_day = day
        if (not hard_breach) and balance < hard_floor:
            hard_breach = True
            hard_breach_day = day.isoformat()
        if (not reserve_breach) and balance < reserve_floor:
            reserve_breach = True
            reserve_breach_day = day.isoformat()

    return {
        "min_balance": round(min_balance, 2),
        "min_balance_day": min_balance_day.isoformat(),
        "breaches_hard_floor": hard_breach,
        "hard_breach_day": hard_breach_day,
        "breaches_reserve_floor": reserve_breach,
        "reserve_breach_day": reserve_breach_day,
    }


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
    forecast_window_days = int(os.getenv("FORECAST_WINDOW_DAYS", "35"))
    forecast_window_days = max(30, min(45, forecast_window_days))
    forecast_end = today + timedelta(days=forecast_window_days)
    hard_floor = 0.0

    available_total = _available_total(normalized_payload["accounts_summary"])
    monthly_income_estimate = _annualized_income(normalized_payload["income_schedule"])
    reserve_floor = _reserve_floor(monthly_income_estimate, prefs)
    allocatable_cash = max(0.0, available_total - reserve_floor)
    freshness = _freshness_contributor(normalized_payload, today)
    volatility = _volatility_contributor(normalized_payload, today)
    inflows_by_day = _income_event_buckets(
        normalized_payload.get("income_schedule", []),
        start_day=today,
        end_day=forecast_end,
    )

    risk_tolerance = str(prefs.get("risk_tolerance", "medium"))
    payment_style = str(prefs.get("payment_style", "balanced"))

    obligation_events: list[dict[str, Any]] = []
    for obligation in obligations:
        due_date = date.fromisoformat(obligation["expected_due_date"])
        rail_type = _rail_type_for_obligation(obligation)
        earliest_safe = today
        if business_calendar == "uk":
            earliest_safe = _next_uk_business_day(earliest_safe)
        latest_safe = _latest_safe_for_obligation(
            due_date=due_date,
            rail_type=rail_type,
            business_calendar=business_calendar,
        )
        if latest_safe < earliest_safe:
            latest_safe = earliest_safe
        if payment_style == "conservative":
            recommended_date = earliest_safe
        elif payment_style == "last_safe_moment":
            recommended_date = latest_safe
        else:
            span_days = max(0, (latest_safe - earliest_safe).days)
            recommended_date = earliest_safe + timedelta(days=span_days // 2)
        if recommended_date < earliest_safe:
            recommended_date = earliest_safe
        if recommended_date > latest_safe:
            recommended_date = latest_safe
        if business_calendar == "uk":
            recommended_date = _previous_uk_business_day(recommended_date)
            if recommended_date < earliest_safe:
                recommended_date = earliest_safe

        amount_expected = float(obligation["expected_amount"])
        amount_for_forecast = amount_expected
        if risk_tolerance == "low":
            amount_for_forecast = round(amount_expected * 1.10, 2)

        default_outflow_day = due_date if rail_type == "direct_debit" else recommended_date
        pay_now_day = due_date if rail_type == "direct_debit" else earliest_safe
        obligation_events.append(
            {
                "obligation_key": obligation["obligation_key"],
                "rail_type": rail_type,
                "due_date": due_date,
                "earliest_safe_date": earliest_safe,
                "latest_safe_date": latest_safe,
                "recommended_date": recommended_date,
                "default_outflow_day": default_outflow_day,
                "pay_now_day": pay_now_day,
                "amount_expected": amount_expected,
                "amount_for_forecast": amount_for_forecast,
            }
        )

    def build_outflows(
        *,
        override_obligation_key: str | None = None,
        override_day: date | None = None,
        override_amount: float | None = None,
    ) -> dict[date, float]:
        outflows: dict[date, float] = {}
        for event in obligation_events:
            day = event["default_outflow_day"]
            amount = float(event["amount_for_forecast"])
            if override_obligation_key and event["obligation_key"] == override_obligation_key:
                if override_day is not None:
                    day = override_day
                if override_amount is not None:
                    amount = override_amount
            outflows[day] = round(outflows.get(day, 0.0) + amount, 2)
        return outflows

    decisions: list[dict[str, Any]] = []
    reserve_breach_projection_count = 0
    hard_floor_breach_projection_count = 0
    low_confidence_decision_count = 0

    for obligation, event in zip(obligations, obligation_events):
        amount = float(event["amount_expected"])
        due_date = date.fromisoformat(obligation["expected_due_date"])
        due_in_days = int(obligation["due_in_days"])
        severity = obligation["severity"]

        severity_val = _severity_score(severity)
        urgent = _urgency_rule(due_in_days, severity_val, risk_tolerance)
        past_due = due_in_days < 0

        recommended_amount = 0.0
        action = "HOLD_AND_ALERT"
        execution_date_obj = today
        decision_rule = "default_hold_no_liquidity"
        base_confidence = _clamp01(float(obligation.get("confidence", 0.7)))
        overall_confidence = _decision_confidence(
            base_confidence=base_confidence,
            freshness_score=float(freshness["score"]),
            volatility_score=float(volatility["score"]),
        )
        low_confidence = overall_confidence < 0.50
        confidence_note: str | None = None

        wait_outflows = build_outflows(
            override_obligation_key=obligation["obligation_key"],
            override_day=event["default_outflow_day"],
            override_amount=event["amount_for_forecast"],
        )
        pay_now_outflows = build_outflows(
            override_obligation_key=obligation["obligation_key"],
            override_day=event["pay_now_day"],
            override_amount=event["amount_for_forecast"],
        )
        wait_forecast = _simulate_horizon(
            start_balance=available_total,
            start_day=today,
            horizon_days=forecast_window_days,
            inflows_by_day=inflows_by_day,
            outflows_by_day=wait_outflows,
            reserve_floor=reserve_floor,
            hard_floor=hard_floor,
        )
        pay_now_forecast = _simulate_horizon(
            start_balance=available_total,
            start_day=today,
            horizon_days=forecast_window_days,
            inflows_by_day=inflows_by_day,
            outflows_by_day=pay_now_outflows,
            reserve_floor=reserve_floor,
            hard_floor=hard_floor,
        )

        wait_unsafe = bool(wait_forecast["breaches_hard_floor"]) or (
            risk_tolerance == "low" and bool(wait_forecast["breaches_reserve_floor"])
        )
        pay_now_unsafe = bool(pay_now_forecast["breaches_hard_floor"]) or (
            risk_tolerance == "low" and bool(pay_now_forecast["breaches_reserve_floor"])
        )
        if wait_forecast["breaches_reserve_floor"]:
            reserve_breach_projection_count += 1
        if wait_forecast["breaches_hard_floor"]:
            hard_floor_breach_projection_count += 1

        if allocatable_cash >= amount:
            if wait_unsafe and (not pay_now_unsafe):
                action = "PAY_NOW"
                execution_date_obj = event["pay_now_day"]
                decision_rule = "wait_unsafe_pay_now_safe"
            elif wait_unsafe and pay_now_unsafe:
                action = "PARTIAL_PAY"
                execution_date_obj = today
                recommended_amount = round(min(amount, allocatable_cash), 2)
                decision_rule = "both_paths_unsafe_partial"
            elif urgent or past_due:
                action = "PAY_NOW"
                execution_date_obj = event["pay_now_day"]
                decision_rule = "urgency_threshold"
            else:
                action = "WAIT"
                execution_date_obj = event["recommended_date"]
                decision_rule = "wait_safe_recommended_date"

            if low_confidence and action == "PAY_NOW":
                if severity_val == 2:
                    action = "HOLD_AND_ALERT"
                    execution_date_obj = today
                    recommended_amount = 0.0
                    decision_rule = "low_confidence_medium_risk_hold"
                    confidence_note = (
                        "Low confidence + medium risk: moved to HOLD_AND_ALERT for manual review."
                    )
                elif severity_val >= 3:
                    decision_rule = f"{decision_rule}_low_confidence_high_risk_escalated_note"
                    confidence_note = (
                        "Low confidence + high risk: keeping PAY NOW with stronger caution."
                    )

            if action in {"PAY_NOW", "WAIT"}:
                recommended_amount = amount
                allocatable_cash = round(max(0.0, allocatable_cash - amount), 2)
            elif action == "PARTIAL_PAY":
                allocatable_cash = round(max(0.0, allocatable_cash - recommended_amount), 2)
        elif allocatable_cash > 0:
            action = "PARTIAL_PAY"
            execution_date_obj = today
            recommended_amount = round(allocatable_cash, 2)
            allocatable_cash = 0.0
            decision_rule = "insufficient_full_amount_partial"
        else:
            action = "HOLD_AND_ALERT"
            execution_date_obj = today
            decision_rule = "no_allocatable_cash_hold"

        if low_confidence:
            low_confidence_decision_count += 1
        execution_date = execution_date_obj.isoformat()
        forecast_assessment = {
            "wait_breaches_hard_floor": bool(wait_forecast["breaches_hard_floor"]),
            "wait_breaches_reserve_floor": bool(wait_forecast["breaches_reserve_floor"]),
            "pay_now_breaches_hard_floor": bool(pay_now_forecast["breaches_hard_floor"]),
            "pay_now_breaches_reserve_floor": bool(pay_now_forecast["breaches_reserve_floor"]),
            "wait_min_balance": wait_forecast["min_balance"],
            "pay_now_min_balance": pay_now_forecast["min_balance"],
            "hard_floor": hard_floor,
            "reserve_floor": reserve_floor,
            "earliest_safe_date": event["earliest_safe_date"].isoformat(),
            "latest_safe_date": event["latest_safe_date"].isoformat(),
            "recommended_date": event["recommended_date"].isoformat(),
        }
        confidence_assessment = {
            "base_confidence": round(base_confidence, 3),
            "overall_confidence": overall_confidence,
            "low_confidence": low_confidence,
            "freshness_score": freshness["score"],
            "volatility_score": volatility["score"],
            "note": confidence_note,
        }

        cash_after_action = round(available_total - recommended_amount, 2)
        decisions.append(
            {
                "obligation_key": obligation["obligation_key"],
                "counterparty": obligation["counterparty"],
                "intent_label": obligation["intent_label"],
                "severity": severity,
                "due_date": obligation["expected_due_date"],
                "due_in_days": due_in_days,
                "rail_type": event["rail_type"],
                "decision_rule": decision_rule,
                "action": action,
                "recommended_execution_date": execution_date,
                "recommended_amount": round(recommended_amount, 2),
                "currency": obligation["currency"],
                "confidence": overall_confidence,
                "confidence_assessment": confidence_assessment,
                "forecast_assessment": forecast_assessment,
                "rationale": _build_rationale(
                    obligation=obligation,
                    action=action,
                    execution_date=execution_date,
                    cash_after=cash_after_action,
                    reserve_floor=reserve_floor,
                    user_preferences=prefs,
                    forecast_assessment=forecast_assessment,
                    confidence_assessment=confidence_assessment,
                ),
            }
        )

    risk_flags = normalized_payload.get("risk_flags", [])
    critical_risk_count = sum(1 for flag in risk_flags if flag.get("severity") == "critical")
    high_risk_count = sum(1 for flag in risk_flags if flag.get("severity") == "high")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days_ahead,
        "forecast_window_days": forecast_window_days,
        "summary": {
            "available_total": available_total,
            "monthly_income_estimate": round(monthly_income_estimate, 2),
            "reserve_floor": reserve_floor,
            "hard_floor": hard_floor,
            "allocatable_cash_remaining": round(allocatable_cash, 2),
            "obligations_in_window": len(obligations),
            "decisions": len(decisions),
            "forecast_reserve_breach_count": reserve_breach_projection_count,
            "forecast_hard_floor_breach_count": hard_floor_breach_projection_count,
            "low_confidence_decision_count": low_confidence_decision_count,
            "critical_risk_count": critical_risk_count,
            "high_risk_count": high_risk_count,
        },
        "preferences_applied": prefs,
        "confidence_inputs": {
            "freshness": freshness,
            "volatility": volatility,
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
