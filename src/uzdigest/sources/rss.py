"""Адаптер RSS/Atom (feedparser)."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import feedparser

from uzdigest.config import Source
from uzdigest.extract.normalize import html_to_text
from uzdigest.models import RawItem
from uzdigest.sources.base import FetchContext, FetchOutcome, SourceError
from uzdigest.web.http import HttpClient


class FeedError(SourceError):
    """Лента не разобрана или пуста."""


def fetch(http: HttpClient, source: Source, ctx: FetchContext) -> FetchOutcome:
    fetched = http.fetch(
        source.url, etag=ctx.etag, last_modified=ctx.last_modified, check_robots=not source.robots_exempt
    )
    if fetched.not_modified:
        return FetchOutcome(not_modified=True, etag=fetched.etag, last_modified=fetched.last_modified)
    return FetchOutcome(
        items=parse(fetched.content, fetched.url), etag=fetched.etag, last_modified=fetched.last_modified
    )


def parse(content: bytes, base_url: str) -> list[RawItem]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise FeedError(f"лента не разобрана: {feed.bozo_exception}")
    items = []
    for entry in feed.entries:
        link = (entry.get("link") or "").strip()
        title = html_to_text(entry.get("title", ""))
        if not link or not title:
            continue
        body = _body(entry)
        lead = html_to_text(entry.get("summary", "")) or None
        items.append(
            RawItem(
                url=urljoin(base_url, link),  # у lex.uz ссылки относительные
                title=title,
                guid=entry.get("id") or None,
                lead=None if lead == body else lead,  # некоторые ленты дублируют полный текст в summary
                body=body,
                published_at=_published(entry),
                categories=_categories(entry),
            )
        )
    if not items:
        raise FeedError("в ленте нет ни одного материала со ссылкой и заголовком")
    return items


def _published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:  # feedparser уже привёл время к UTC
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
    return None


def _categories(entry: Any) -> tuple[str, ...]:
    terms = (html_to_text(tag.get("term") or "") for tag in entry.get("tags", []))
    return tuple(dict.fromkeys(t for t in terms if t))


def _body(entry: Any) -> str | None:
    for content in entry.get("content", []):
        text = html_to_text(content.get("value", ""))
        if text:
            return text
    return None
