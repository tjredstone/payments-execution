from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

from db import (
    ensure_token_crypto_ready,
    has_advisory_been_sent,
    init_db,
    log_advisory_sent,
    make_decision_fingerprint,
    migrate_plaintext_tokens,
)
from engine import run_engine

DISCLAIMER = (
    "Informational guidance only. Not financial advice. "
    "Always verify obligations and payment timing yourself."
)


def _is_actionable(decision: dict[str, Any]) -> bool:
    action = decision.get("action")
    if action in {"PAY_NOW", "PARTIAL_PAY", "HOLD_AND_ALERT"}:
        return True
    return action == "WAIT" and int(decision.get("due_in_days", 999)) <= 2


def _format_decision_line(decision: dict[str, Any]) -> str:
    reasons = decision.get("rationale") or []
    reason = reasons[0] if reasons else "No rationale."
    return (
        f"[{decision['action']}] {decision['counterparty']} "
        f"{decision['recommended_amount']:.2f} {decision['currency']} "
        f"on {decision['recommended_execution_date']} "
        f"(due {decision['due_date']}; {reason})"
    )


def _post_webhook(webhook_url: str, payload: dict[str, Any], timeout_seconds: int = 10) -> None:
    resp = requests.post(
        webhook_url,
        json=payload,
        timeout=timeout_seconds,
    )
    resp.raise_for_status()


def run_notifier() -> None:
    load_dotenv()
    init_db()
    ensure_token_crypto_ready()
    migrate_plaintext_tokens()

    user_id = os.getenv("LOCAL_USER_ID", "local-dev-user")
    provider = os.getenv("OPEN_BANKING_PROVIDER", "truelayer")
    lookback_days = int(os.getenv("NORMALISE_LOOKBACK_DAYS", "120"))
    advisory_window_days = int(os.getenv("ADVISORY_WINDOW_DAYS", "14"))
    webhook_url = os.getenv("ADVISORY_WEBHOOK_URL", "").strip()

    engine_output = run_engine(
        user_id=user_id,
        provider=provider,
        normalise_lookback_days=lookback_days,
        advisory_window_days=advisory_window_days,
    )

    actionable = [d for d in engine_output["decisions"] if _is_actionable(d)]
    unsent: list[dict[str, Any]] = []
    for decision in actionable:
        fingerprint = make_decision_fingerprint(decision)
        if not has_advisory_been_sent(user_id, provider, fingerprint):
            unsent.append(decision)

    if not unsent:
        print("No new actionable advisories.")
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    text_lines = [_format_decision_line(decision) for decision in unsent]
    message = "\n".join(text_lines) + "\n\n" + DISCLAIMER
    print("New advisories:\n" + message)

    sent_channels: list[str] = ["console"]
    if webhook_url:
        webhook_payload = {
            "timestamp": timestamp,
            "user_id": user_id,
            "provider": provider,
            "count": len(unsent),
            "advisories": unsent,
            "text": message,
        }
        _post_webhook(webhook_url, webhook_payload)
        sent_channels.append("webhook")

    for decision in unsent:
        for channel in sent_channels:
            log_advisory_sent(
                user_id=user_id,
                provider=provider,
                decision=decision,
                sent_via=channel,
            )

    print(
        "Delivered advisories: "
        f"{len(unsent)} via {', '.join(sent_channels)}"
    )


def main() -> None:
    try:
        run_notifier()
    except ValueError as exc:
        print(f"{exc} Run advisory/app.py then advisory/run_daily.py first.")
    except RuntimeError as exc:
        print(f"{exc} Set TOKENS_ENCRYPTION_KEY in your environment.")
    except requests.HTTPError as exc:
        print(f"Notifier delivery failed: {exc}")
        raise


if __name__ == "__main__":
    main()
