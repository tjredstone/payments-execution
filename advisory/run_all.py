from __future__ import annotations

from datetime import datetime, timezone

import requests

from notifier import run_notifier
from run_daily import run_daily


def run_all() -> None:
    started_at = datetime.now(timezone.utc)
    print(f"[run_all] Started at {started_at.isoformat()}")

    print("[run_all] Step 1/2: running daily ingestion...")
    run_daily()

    print("[run_all] Step 2/2: running notifier...")
    run_notifier()

    finished_at = datetime.now(timezone.utc)
    duration_seconds = (finished_at - started_at).total_seconds()
    print(
        f"[run_all] Completed at {finished_at.isoformat()} "
        f"(duration={duration_seconds:.2f}s)"
    )


def main() -> None:
    try:
        run_all()
    except (ValueError, RuntimeError) as exc:
        print(f"[run_all] Failed: {exc}")
    except requests.HTTPError as exc:
        print(f"[run_all] Provider/network failure: {exc}")
        raise


if __name__ == "__main__":
    main()
