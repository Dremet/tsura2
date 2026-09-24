"""Convert browser-local calendar inputs into absolute race times."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def weekly_starts(local_value: str, timezone_name: str, count: int,
                  utc_offset: int | None = None) -> list[datetime]:
    """Return UTC starts, keeping the same local clock time across DST changes."""
    try:
        local = datetime.strptime(local_value, "%Y-%m-%dT%H:%M")
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        raise ValueError("Enter a valid date, time and IANA time zone.") from None
    if not 1 <= count <= 26:
        raise ValueError("Choose between 1 and 26 events.")

    starts = []
    for week in range(count):
        wall_time = local + timedelta(weeks=week)
        choices = []
        for fold in (0, 1):
            candidate = wall_time.replace(tzinfo=zone, fold=fold)
            if candidate.astimezone(timezone.utc).astimezone(zone).replace(
                    tzinfo=None) == wall_time:
                choices.append(candidate)
        if not choices:
            raise ValueError(
                f"{wall_time:%Y-%m-%d %H:%M} does not exist in {timezone_name} "
                "because clocks change then. Choose another time."
            )
        selected = choices[0]
        if week == 0 and utc_offset is not None:
            matching = [c for c in choices
                        if int(c.utcoffset().total_seconds() / 60) == utc_offset]
            if not matching:
                raise ValueError("The time zone does not match your system time.")
            selected = matching[0]
        starts.append(selected.astimezone(timezone.utc))
    return starts
