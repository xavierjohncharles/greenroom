"""Book a demo call, on camera, through the real job queue.

    uv run python scripts/book_demo_call.py

Goes through the Scheduler and the job queue rather than calling the Calendar API
directly, so what the demo shows is the actual path: a job is claimed, the handler runs,
a real event appears, and the target moves to `booked`.

The attendee must be in `config/targets.csv` — a calendar invite emails the attendee, so
it is subject to the same allow-list as a send.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from greenroom.agents.scheduler import Scheduler  # noqa: E402
from greenroom.config import get_config  # noqa: E402
from greenroom.jobs.queue import JobQueue  # noqa: E402
from greenroom.state.db import get_db  # noqa: E402
from greenroom.state.models import JobType, TargetStatus  # noqa: E402
from greenroom.state.repo import Repo  # noqa: E402
from greenroom.tools.calendar import CalendarTool  # noqa: E402
from greenroom.tools.gmail import GmailTool  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="crazyxydj@gmail.com", help="who to invite")
    parser.add_argument("--days", type=int, default=3, help="days from now")
    args = parser.parse_args()

    config = get_config()
    if args.email.lower() not in config.allowed_addresses:
        print(f"{args.email} is not in config/targets.csv — refusing.", file=sys.stderr)
        return 1

    db = get_db()
    repo, queue = Repo(db), JobQueue(db)
    target = next(t for t in config.targets.targets if t.email.lower() == args.email.lower())
    repo.upsert_target(target)

    # Walk it to a state a booking legitimately follows, if it is not already there.
    for nxt in (TargetStatus.RESEARCHED, TargetStatus.PITCHED, TargetStatus.REPLIED):
        try:
            repo.set_status(target.key, nxt)
        except Exception:
            pass

    start = (datetime.now(UTC) + timedelta(days=args.days)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    key = f"book:{target.key}:{start:%Y%m%dT%H%M}"
    queue.enqueue(
        job_type=JobType.BOOK_CALL,
        idempotency_key=key,
        target_id=target.key,
        payload={
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
            "summary": f"{config.brand.company_name} x {target.organisation} — intro call",
            "description": "Booked by Greenroom.",
        },
    )
    print(f"queued a booking for {target.organisation} at {start:%A %d %B %H:%M} UTC")

    scheduler = Scheduler(
        repo=repo,
        queue=queue,
        config=config,
        gmail=GmailTool(dry_run=True),
        calendar=CalendarTool(dry_run=False),
        dry_run=False,
    )
    tally = asyncio.run(scheduler.run_due_jobs(limit=5))
    print("tick:", tally)
    print("target status:", repo.get_target(target.key).status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
