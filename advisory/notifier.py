from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

from db import (
    ensure_token_crypto_ready,
    has_advisory_been_sent,
    init_db,
    list_connections,
    log_advisory_sent,
    make_decision_fingerprint,
    migrate_plaintext_tokens,
    resolve_effective_user_id,
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


def _run_notifier_for_user(user_id: str, provider: str, lookback_days: int, advisory_window_days: int, webhook_url: str) -> None:
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
        print(f"No new actionable advisories for user={user_id}.")
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    text_lines = [_format_decision_line(decision) for decision in unsent]
    message = "\n".join(text_lines) + "\n\n" + DISCLAIMER
    print(f"New advisories for user={user_id}:\n{message}")

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

    print(f"Delivered advisories for user={user_id}: {len(unsent)} via {', '.join(sent_channels)}")


def run_notifier(user_id: str | None = None, all_users: bool = False) -> None:
    load_dotenv()
    init_db()
    ensure_token_crypto_ready()
    migrate_plaintext_tokens()

    env_user_id = os.getenv("LOCAL_USER_ID")
    provider = os.getenv("OPEN_BANKING_PROVIDER", "truelayer")
    lookback_days = int(os.getenv("NORMALISE_LOOKBACK_DAYS", "120"))
    advisory_window_days = int(os.getenv("ADVISORY_WINDOW_DAYS", "14"))
    webhook_url = os.getenv("ADVISORY_WEBHOOK_URL", "").strip()
    if all_users:
        user_ids = sorted({row["user_id"] for row in list_connections(provider=provider)})
        if not user_ids:
            print("No connected bank users found.")
            return
    else:
        try:
            user_ids = [resolve_effective_user_id(user_id or env_user_id)]
        except ValueError as exc:
            print(f"{exc} Example: python advisory/notifier.py --user-id <uuid>")
            return

    for target_user_id in user_ids:
        _run_notifier_for_user(
            user_id=target_user_id,
            provider=provider,
            lookback_days=lookback_days,
            advisory_window_days=advisory_window_days,
            webhook_url=webhook_url,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Send actionable advisory notifications.")
    parser.add_argument("--user-id", help="User ID to run notifier for.")
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Run notifier for every connected user.",
    )
    args = parser.parse_args()
    if args.user_id and args.all_users:
        print("Use either --user-id or --all-users, not both.")
        return

    try:
        run_notifier(user_id=args.user_id, all_users=args.all_users)
    except ValueError as exc:
        print(f"{exc} Run advisory/app.py then advisory/run_daily.py first.")
    except RuntimeError as exc:
        print(f"{exc} Set TOKENS_ENCRYPTION_KEY in your environment.")
    except requests.HTTPError as exc:
        print(f"Notifier delivery failed: {exc}")
        raise


if __name__ == "__main__":
    main()
