"""Wiring: one place that builds the repo, queue, tools and Scheduler.

Constructed lazily and cached, so importing the web app costs nothing and a container
that only serves /health never opens a Firestore connection.
"""

from __future__ import annotations

from functools import lru_cache

from greenroom.agents.scheduler import Scheduler
from greenroom.config import get_config
from greenroom.jobs.queue import JobQueue
from greenroom.settings import get_settings
from greenroom.state.db import get_db
from greenroom.state.repo import Repo
from greenroom.tools.calendar import CalendarTool
from greenroom.tools.gmail import GmailTool


@lru_cache(maxsize=1)
def get_repo() -> Repo:
    return Repo(get_db())


@lru_cache(maxsize=1)
def get_queue() -> JobQueue:
    return JobQueue(get_db())


@lru_cache(maxsize=1)
def get_scheduler() -> Scheduler:
    settings = get_settings()
    return Scheduler(
        repo=get_repo(),
        queue=get_queue(),
        config=get_config(),
        gmail=GmailTool(),
        calendar=CalendarTool(),
        dry_run=settings.dry_run,
    )
