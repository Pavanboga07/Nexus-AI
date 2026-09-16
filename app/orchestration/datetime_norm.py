"""Date and time normalization for natural language orchestration (Part 13).

Transforms conversational date/time expressions into structured, timezone-aware
constraints (e.g. {"date": "2026-09-16", "start_time": "18:00", "timezone": ...}).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


class NormalizedTimeConstraint(BaseModel):
    """Structured, validated time constraint representation."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO 8601 date (YYYY-MM-DD)")
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$", description="24-hour time (HH:MM)")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$", description="24-hour time (HH:MM)")
    timezone: str = Field(default="UTC", description="IANA Timezone name")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class DateTimeNormalizer:
    """Deterministic parser and validator for conversational date/time expressions."""

    WEEKDAYS = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    def __init__(self, default_timezone: str = "UTC") -> None:
        self._default_tz = default_timezone

    def normalize(
        self,
        raw_date: str | None = None,
        raw_time: str | None = None,
        reference_time: datetime | None = None,
        tz_name: str | None = None,
    ) -> NormalizedTimeConstraint:
        """Parse natural language date and time strings into a NormalizedTimeConstraint."""
        tz_str = tz_name or self._default_tz
        try:
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = timezone.utc
            tz_str = "UTC"

        now = reference_time or datetime.now(tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)

        # 1. Normalize Date
        parsed_date = self._parse_date(raw_date, now)

        # 2. Normalize Time
        start_time_str, end_time_str = self._parse_time(raw_time)

        return NormalizedTimeConstraint(
            date=parsed_date.isoformat(),
            start_time=start_time_str,
            end_time=end_time_str,
            timezone=tz_str,
        )

    def _parse_date(self, raw: str | None, now: datetime) -> date:
        if not raw:
            return (now + timedelta(days=1)).date()

        low = raw.strip().lower()

        # Check ISO format
        if re.match(r"^\d{4}-\d{2}-\d{2}$", low):
            return date.fromisoformat(low)

        if "today" in low or "tonight" in low:
            return now.date()

        if "day after tomorrow" in low:
            return (now + timedelta(days=2)).date()

        if "tomorrow" in low:
            return (now + timedelta(days=1)).date()

        # Check weekday names
        for day_name, day_num in self.WEEKDAYS.items():
            if day_name in low:
                curr_day = now.weekday()
                days_ahead = (day_num - curr_day) % 7
                if days_ahead == 0 or "next" in low:
                    days_ahead += 7
                return (now + timedelta(days=days_ahead)).date()

        # Default to tomorrow
        return (now + timedelta(days=1)).date()

    def _parse_time(self, raw: str | None) -> tuple[str | None, str | None]:
        if not raw:
            return None, None

        low = raw.strip().lower()

        # Common periods of day
        if "morning" in low:
            return "09:00", "12:00"
        if "afternoon" in low:
            return "14:00", "17:00"
        if "evening" in low or "tonight" in low:
            return "18:00", "21:00"

        # Check explicit 24h format (e.g. 18:00)
        match_24 = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", low)
        if match_24:
            hh = int(match_24.group(1))
            mm = int(match_24.group(2))
            return f"{hh:02d}:{mm:02d}", None

        # Check 12h formats (e.g. 6 PM, 6:30 PM, after 6, at 8)
        match_12 = re.search(r"(?:after|at|from)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", low)
        if match_12:
            hh = int(match_12.group(1))
            mm = int(match_12.group(2)) if match_12.group(2) else 0
            meridiem = match_12.group(3)

            if meridiem == "pm" and hh < 12:
                hh += 12
            elif meridiem == "am" and hh == 12:
                hh = 0
            elif not meridiem and (hh <= 6 or hh == 7 or hh == 8) and ("after" in low or "evening" in low):
                # Conversational "after 6" usually means 18:00
                hh += 12

            return f"{hh:02d}:{mm:02d}", None

        return None, None
