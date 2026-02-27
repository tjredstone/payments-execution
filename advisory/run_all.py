from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from db import resolve_effective_user_id
from notifier import run_notifier
from run_daily import run_daily


def run_all(user_id: str | None = None, all_users: bool = False) -> None:
    started_at = datetime.now(timezone.utc)
    print(f"[run_all] Started at {started_at.isoformat()}")

    print("[run_all] Step 1/2: running daily ingestion...")
    run_daily(user_id=user_id, all_users=all_users)

    print("[run_all] Step 2/2: running notifier...")
    run_notifier(user_id=user_id, all_users=all_users)

    finished_at = datetime.now(timezone.utc)
    duration_seconds = (finished_at - started_at).total_seconds()
    print(
        f"[run_all] Completed at {finished_at.isoformat()} "
        f"(duration={duration_seconds:.2f}s)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ingestion + notifier for one user or all users.")
    parser.add_argument("--user-id", help="User ID to process.")
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Process every connected user.",
    )
    args = parser.parse_args()
    if args.user_id and args.all_users:
        print("[run_all] Use either --user-id or --all-users, not both.")
        return
    load_dotenv()
    user_id: str | None = None
    if not args.all_users:
        try:
            user_id = resolve_effective_user_id(args.user_id or os.getenv("LOCAL_USER_ID"))
        except ValueError as exc:
            print(f"[run_all] Failed: {exc}")
            print("[run_all] Example: python advisory/run_all.py --user-id <uuid>")
            return

    try:
        run_all(user_id=user_id, all_users=args.all_users)
    except (ValueError, RuntimeError) as exc:
        print(f"[run_all] Failed: {exc}")
    except requests.HTTPError as exc:
        print(f"[run_all] Provider/network failure: {exc}")
        raise


if __name__ == "__main__":
    main()
