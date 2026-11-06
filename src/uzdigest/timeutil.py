"""Время: в БД всё хранится в UTC строками ISO 8601, сутки и окна считаются по Ташкенту."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TASHKENT = ZoneInfo("Asia/Tashkent")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def tashkent_to_utc(local: datetime) -> datetime:
    """Время без пояса, записанное по Ташкенту, → UTC."""
    return local.replace(tzinfo=TASHKENT).astimezone(UTC)


def tashkent_date(moment: datetime) -> date:
    return moment.astimezone(TASHKENT).date()


def day_end_utc(day: date) -> datetime:
    """Последняя секунда ташкентских суток в UTC: верхняя граница для материалов с точностью до дня."""
    return tashkent_to_utc(datetime.combine(day + timedelta(days=1), time()) - timedelta(seconds=1))


def tashkent_days(since: datetime, until: datetime) -> list[date]:
    """Ташкентские даты от since до until включительно."""
    first, last = tashkent_date(since), tashkent_date(until)
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]
