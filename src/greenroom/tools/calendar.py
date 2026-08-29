"""Calendar access: propose free slots, create the booked call. Nothing else.

https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query
https://developers.google.com/workspace/calendar/api/v3/reference/events/insert

Containment, same principle as Gmail: this file has **no** update, patch, delete or
move method. Greenroom cannot modify or remove an existing event because the code to
do so does not exist. `freebusy` is used rather than `events.list` so that proposing a
slot never reads the contents of Xavier's other meetings — only whether a window is
busy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from greenroom.config import get_config
from greenroom.obs import get_logger
from greenroom.settings import get_settings

log = get_logger(__name__)

PRIMARY = "primary"


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime

    def to_human(self, tz: str) -> str:
        local = self.start.astimezone(ZoneInfo(tz))
        return local.strftime("%A %d %B, %H:%M")

    def to_rfc3339(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()


@dataclass(frozen=True)
class BookedEvent:
    event_id: str
    html_link: str
    start: datetime
    dry_run: bool


class CalendarTool:
    def __init__(self, *, dry_run: bool | None = None) -> None:
        settings = get_settings()
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.mailbox = settings.agent_mailbox
        self._svc = None

    @property
    def svc(self):
        if self._svc is None:
            from greenroom.tools.google_auth import calendar_service

            self._svc = calendar_service()
        return self._svc

    # -- reading -----------------------------------------------------------
    def busy_periods(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Busy blocks on the primary calendar. Returns times only, never event details."""
        if self.dry_run:
            log.info(
                "dry-run: would query freebusy",
                extra={"start": start.isoformat(), "end": end.isoformat()},
            )
            return []

        response = (
            self.svc.freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "items": [{"id": PRIMARY}],
                }
            )
            .execute()
        )
        busy = response.get("calendars", {}).get(PRIMARY, {}).get("busy", [])
        return [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in busy
        ]

    def propose_slots(self, *, now: datetime | None = None, days_ahead: int = 14) -> list[Slot]:
        """Offer the next N free slots that satisfy policy.yaml's meeting rules.

        Slot selection is pure policy — allowed weekdays, working hours, minimum notice
        — intersected with real free/busy. The agent never invents a time outside the
        envelope, so a slot in an email is always one Xavier would have offered.
        """
        policy = get_config().policy.meetings
        tz = ZoneInfo(policy.timezone)
        now = (now or datetime.now(tz)).astimezone(tz)

        earliest = now + timedelta(hours=policy.min_notice_hours)
        horizon = now + timedelta(days=days_ahead)
        busy = self.busy_periods(earliest, horizon)

        duration = timedelta(minutes=policy.duration_minutes)
        candidates: list[Slot] = []
        day = earliest.date()

        while day <= horizon.date() and len(candidates) < policy.slots_to_offer:
            if day.weekday() in policy.allowed_weekdays:
                hour = policy.earliest_hour
                while hour < policy.latest_hour and len(candidates) < policy.slots_to_offer:
                    start = datetime.combine(day, datetime.min.time(), tzinfo=tz).replace(hour=hour)
                    end = start + duration
                    if start >= earliest and not _overlaps(start, end, busy):
                        candidates.append(Slot(start, end))
                    hour += 1
            day += timedelta(days=1)

        log.info("slots proposed", extra={"count": len(candidates)})
        return candidates

    # -- writing (create only) ---------------------------------------------
    def create_event(
        self,
        *,
        summary: str,
        description: str,
        slot: Slot,
        attendee_email: str,
        idempotency_key: str,
    ) -> BookedEvent:
        """Create a call. There is no update or delete counterpart in this class.

        `idempotency_key` becomes the event's `id`, so a re-run of a crashed job
        collides with the existing event rather than double-booking. Google requires
        event ids to be base32hex-ish and 5-1024 chars.
        """
        policy = get_config().policy.meetings
        event_id = _sanitise_event_id(idempotency_key)

        if self.dry_run:
            log.info(
                "dry-run: would create calendar event",
                extra={
                    "summary": summary,
                    "start": slot.start.isoformat(),
                    "attendee": attendee_email,
                    "event_id": event_id,
                },
            )
            return BookedEvent(event_id, "https://calendar.google.com/DRYRUN", slot.start, True)

        start_rfc, end_rfc = slot.to_rfc3339()
        body = {
            "id": event_id,
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_rfc, "timeZone": policy.timezone},
            "end": {"dateTime": end_rfc, "timeZone": policy.timezone},
            "attendees": [{"email": attendee_email}],
            "reminders": {"useDefault": True},
        }

        from googleapiclient.errors import HttpError

        try:
            created = (
                self.svc.events().insert(calendarId=PRIMARY, body=body, sendUpdates="all").execute()
            )
        except HttpError as exc:
            # 409 means this exact job already created the event. That is success, not
            # failure — the whole point of the idempotency key.
            if exc.resp.status == 409:
                created = self.svc.events().get(calendarId=PRIMARY, eventId=event_id).execute()
                log.info("event already existed, reusing", extra={"event_id": event_id})
            else:
                raise

        log.info(
            "calendar event created",
            extra={"event_id": created["id"], "start": slot.start.isoformat()},
        )
        return BookedEvent(created["id"], created.get("htmlLink", ""), slot.start, False)


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(start < b_end and end > b_start for b_start, b_end in busy)


def _sanitise_event_id(key: str) -> str:
    """Google Calendar event ids allow lowercase a-v and 0-9, length 5-1024."""
    import hashlib

    allowed = set("abcdefghijklmnopqrstuv0123456789")
    cleaned = "".join(c for c in key.lower() if c in allowed)
    if len(cleaned) < 5:
        cleaned = hashlib.sha1(key.encode()).hexdigest()
        cleaned = "".join(c if c in allowed else "v" for c in cleaned)
    return cleaned[:1024]


def next_weekday_on_or_after(day: date, allowed: list[int]) -> date:
    while day.weekday() not in allowed:
        day += timedelta(days=1)
    return day
