"""When scheduled work is due: one-off times, fixed intervals, cron.

Pure code on purpose. TypeSafe documents that Jev reads dates as text and
is unreliable at ordering, offsets and windows ("Extract components;
compare in code"), and the live model already turns "every weekday at 9"
into structured arguments. So no model ever does schedule arithmetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re

# (low, high) per standard 5-field crontab: minute hour dom month dow.
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = {3: {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())},
          4: {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
            "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
            "@annually": "0 0 1 1 *"}


def _value(text, index):
    text = text.strip().lower()
    names = _NAMES.get(index, {})
    if text in names:
        return names[text]
    if not text.isdigit():
        raise ValueError(f"Invalid cron value {text!r}")
    return int(text)


def _field(text, index):
    low, high = _FIELDS[index]
    values = set()
    for part in text.split(","):
        if not part:
            raise ValueError("Empty cron list item")
        base, _, step = part.partition("/")
        step = int(step) if step else 1
        if step < 1:
            raise ValueError("Cron step must be positive")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = _value(a, index), _value(b, index)
        else:
            start = _value(base, index)
            end = high if "/" in part else start  # "5/15" = from 5 to the end
        if not low <= start <= high or not low <= end <= high or start > end:
            raise ValueError(f"Cron value out of range in {part!r}")
        values.update(range(start, end + 1, step))
    if index == 4 and 7 in values:  # both 0 and 7 mean Sunday
        values.discard(7)
        values.add(0)
    return values


class Cron:
    def __init__(self, expression: str):
        text = _ALIASES.get(expression.strip().lower(), expression)
        parts = text.split()
        if len(parts) != 5:
            raise ValueError("A cron expression needs 5 fields: minute hour day-of-month month day-of-week")
        self.expression = " ".join(parts)
        self.minutes, self.hours, self.days, self.months, self.weekdays = (
            _field(p, i) for i, p in enumerate(parts))
        # Vixie-cron rule: when both day fields are restricted, either may match.
        self._dom_any, self._dow_any = parts[2] == "*", parts[4] == "*"

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.weekdays
        if self._dom_any or self._dow_any:
            return dom and dow
        return dom or dow

    def next_after(self, moment: datetime) -> datetime:
        """First matching minute strictly after `moment` (naive local time)."""
        start = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        for _ in range(366 * 5):  # a Feb-29-only schedule can take four years
            if self._day_matches(day):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate >= start:
                            return candidate
            day += timedelta(days=1)
        raise ValueError(f"Cron expression {self.expression!r} never matches")


def next_run(schedule: dict, after: float) -> float | None:
    """Epoch seconds of the next run strictly after `after`, or None when a
    one-off schedule has already passed."""
    if "at" in schedule:
        at = float(schedule["at"])
        return at if at > after else None
    if "every_minutes" in schedule:
        return after + 60 * float(schedule["every_minutes"])
    if "cron" in schedule:
        return Cron(schedule["cron"]).next_after(datetime.fromtimestamp(after)).timestamp()
    raise ValueError("Schedule needs at, every_minutes or cron")


# A spoken time reaches the tool a few seconds after it was said: "at 17:34"
# said at 17:33:55 arrives at 17:34:03. That is "now", not "already passed".
PAST_GRACE_SECONDS = 120

_CLOCK = re.compile(r"^(?:(today|tonight|tomorrow)\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?$")


def _clock(text: str, now: float) -> float | None:
    """'18:30', '6:30 pm', 'today 18:30', 'tomorrow 9am' -> epoch, or None
    when `text` is not a bare clock time. A bare time is its next
    occurrence; 'today' is today even when it has passed (the caller then
    says so instead of silently moving it to tomorrow)."""
    match = _CLOCK.match(text.strip().lower())
    if not match:
        return None
    day, hour, minute, half = match.group(1), int(match.group(2)), int(match.group(3) or 0), match.group(4)
    if match.group(3) is None and not half:
        return None  # a lone number is not a time
    if half:
        if not 1 <= hour <= 12:
            raise ValueError(f"Could not read the time {text!r}")
        hour = hour % 12 + (12 if half.startswith("p") else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Could not read the time {text!r}")
    base = datetime.fromtimestamp(now)
    moment = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "tomorrow":
        moment += timedelta(days=1)
    elif day is None and moment.timestamp() <= now - PAST_GRACE_SECONDS:
        moment += timedelta(days=1)
    return moment.timestamp()


def parse_at(text: str, now: float) -> float:
    """Local ISO date-time ("2026-09-23T09:00", "2026-09-23 09:00"), or a
    clock time ("18:30", "6:30 pm", "today 18:30", "tomorrow 09:00"). A bare
    clock time means its next occurrence."""
    text = str(text).strip()
    try:
        clock = _clock(text, now)
        if clock is not None:
            return clock
        moment = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError as exc:
        raise ValueError(f"Could not read the time {text!r}; use local ISO like 2026-09-23T09:00") from exc
    return moment.timestamp()  # naive means local time, as spoken; an explicit offset is honoured


def describe_when(epoch: float | None, now: float) -> str:
    """'today 18:30 (in 57 min)', 'tomorrow 09:00 (in 15 h)', 'Tue 2026-10-06
    16:30 (in 6 days)': what the assistant repeats back, so a wrong day is
    heard at once."""
    if epoch is None:
        return "never"
    moment, today = datetime.fromtimestamp(epoch), datetime.fromtimestamp(now).date()
    days = (moment.date() - today).days
    label = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days, moment.strftime("%a %Y-%m-%d"))
    seconds = epoch - now
    if seconds < 0:
        rel = "overdue"
    elif seconds < 90:
        rel = "in under 2 min"
    elif seconds < 5400:
        rel = f"in {round(seconds / 60)} min"
    elif seconds < 172800:
        rel = f"in {seconds / 3600:.1f} h".replace(".0 h", " h")
    else:
        rel = f"in {round(seconds / 86400)} days"
    return f"{label} {moment.strftime('%H:%M')} ({rel})"


def describe(epoch: float | None) -> str:
    if epoch is None:
        return "never"
    return datetime.fromtimestamp(epoch).strftime("%a %Y-%m-%d %H:%M")
