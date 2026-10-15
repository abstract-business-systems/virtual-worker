"""Reminder request parsing (requirement 4).

Understands utterances like:
  "remind me tomorrow at 10:00 that I need to prepare the report"
  "remind me in 20 minutes to check the build"
  "Alex, remind me at 15:30 about the review"
Times are returned as absolute UTC plus the requester's IANA zone.
"""
import re
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_PATTERNS = [
    # in N minutes/hours/seconds
    (re.compile(r"\bin\s+(\d+)\s*(second|minute|hour)s?\b", re.I), "rel"),
    # tomorrow at HH:MM
    (re.compile(r"\btomorrow\s+at\s+(\d{1,2}):(\d{2})\b", re.I), "tomorrow"),
    # at HH:MM (today, or tomorrow if already past)
    (re.compile(r"\bat\s+(\d{1,2}):(\d{2})\b", re.I), "today"),
]


def parse_reminder(text, default_tz="UTC", requester_tz=None):
    """Returns dict {who?, what, due_utc, requester_tz} or None."""
    m = re.search(r"remind\s+(?:me\s+|(\w[\w-]*)\s+)?(?:that\s+|to\s+|about\s+|that\s+i\s+)?(.+)", text, re.I)
    if not m:
        return None
    who = m.group(1) or None
    rest = text[m.start(2):].strip(" .!?")
    if not rest:
        return None

    tz_name = requester_tz or default_tz
    tz = timezone.utc
    if ZoneInfo and tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc

    for pattern, kind in _PATTERNS:
        pm = pattern.search(rest)
        if not pm:
            continue
        if kind == "rel":
            n = int(pm.group(1))
            unit = pm.group(2).lower()
            delta = {"second": timedelta(seconds=n),
                     "minute": timedelta(minutes=n),
                     "hour": timedelta(hours=n)}[unit]
            due = datetime.now(timezone.utc) + delta
        else:
            hh, mm = int(pm.group(1)), int(pm.group(2))
            local_now = datetime.now(tz)
            candidate = local_now.replace(hour=hh, minute=mm, second=0,
                                          microsecond=0)
            if kind == "tomorrow" or candidate <= local_now:
                candidate += timedelta(days=1)
            due = candidate.astimezone(timezone.utc)
        what = (rest[:pm.start()] + " " + rest[pm.end():]).strip(" ,.")
        if not what:
            what = rest
        return {"who": who, "what": what, "due_utc": due.timestamp(),
                "requester_tz": tz_name}
    return None