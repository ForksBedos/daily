"""Догрузка материалов, которые выпали из короткой ленты (docs/plan/stage-2-sources.md, задача 2.6).

Правило запуска: самая старая запись ленты новее начала пробела (ctx.gap_start =
max(последний успешный опрос, сейчас − 96 ч)). Тогда недостающее добирается из sitemap,
следующих страниц ленты или страниц рубрик. Новые адреса пишутся в items без текста,
дальше их заполняет enrich.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape

import httpx

from uzdigest.config import Backfill, BackfillSettings, Source
from uzdigest.extract.normalize import canonical_url
from uzdigest.models import Precision, RawItem
from uzdigest.sources import html_list, rss
from uzdigest.sources.base import FetchContext, SourceError
from uzdigest.timeutil import TASHKENT, day_end_utc, tashkent_date, tashkent_days
from uzdigest.web.http import HttpClient

SITEMAP_URL = re.compile(r"<url>(.*?)</url>", re.S)
SITEMAP_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
SITEMAP_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S)


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: datetime | date | None  # date — если sitemap даёт только день


def needs_backfill(items: Iterable[RawItem], gap_start: datetime) -> bool:
    dated = [item.published_at for item in items if item.published_at]
    return not dated or min(dated) > gap_start


def fetch(
    http: HttpClient,
    source: Source,
    ctx: FetchContext,
    settings: BackfillSettings,
    exclude: frozenset[str] = frozenset(),
) -> list[RawItem]:
    """Новые материалы за пробел. exclude — канонические адреса, которые уже есть (БД и текущая лента)."""
    config = source.backfill
    if config is None:
        return []
    match config.strategy:
        case "sitemap_daily":
            items = []
            for day in tashkent_days(ctx.gap_start, ctx.now):
                entries = _dated_sitemap(http, _fill(config.url, day))
                # lastmod у kun — время правки, а не публикации; день публикации — день файла
                items += [_item(e.url, day_end_utc(day), "day") for e in entries]
        case "sitemap_monthly":
            entries = []
            for month in _months(ctx.gap_start, ctx.now):
                entries += _dated_sitemap(http, _fill(config.url, month))
            items = _from_sitemap(entries, config, ctx)
        case "sitemap":
            entries = parse_sitemap(http.fetch(config.url).content)
            items = _from_sitemap(entries, config, ctx)
        case "feed_paged":
            items = _feed_pages(http, source, config, ctx, settings.max_pages)
        case "list_paged":
            items = html_list.collect_links(http, source, config.html, ctx, max_pages=settings.max_pages)

    fresh: dict[str, RawItem] = {}
    for item in items:
        key = canonical_url(item.url)
        if key in exclude or key in ctx.known_urls or key in fresh:
            continue
        if config.url_filter and config.url_filter not in item.url:
            continue
        fresh[key] = item
    # Свежие первыми: если адресов больше лимита, отбрасываются самые старые
    ordered = sorted(fresh.values(), key=lambda i: i.published_at or ctx.now, reverse=True)
    return ordered[: settings.max_urls]


def parse_sitemap(content: bytes) -> list[SitemapEntry]:
    text = content.decode("utf-8", errors="replace")
    if "<urlset" not in text:
        raise SourceError("sitemap не разобран: нет <urlset>")
    entries = []
    for block in SITEMAP_URL.findall(text):
        loc = SITEMAP_LOC.search(block)
        if not loc:
            continue
        lastmod = SITEMAP_LASTMOD.search(block)
        entries.append(SitemapEntry(unescape(loc.group(1)), _lastmod(lastmod.group(1)) if lastmod else None))
    return entries


def _dated_sitemap(http: HttpClient, url: str) -> list[SitemapEntry]:
    """Sitemap за день или месяц. Файла за сегодня или новый месяц может ещё не быть — это не ошибка."""
    try:
        return parse_sitemap(http.fetch(url).content)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise


def _from_sitemap(entries: list[SitemapEntry], config: Backfill, ctx: FetchContext) -> list[RawItem]:
    gap_day = tashkent_date(ctx.gap_start)
    items = []
    for entry in entries:
        if entry.lastmod is None:
            continue
        if isinstance(entry.lastmod, datetime):
            if config.lastmod_is_published:
                if entry.lastmod >= ctx.gap_start:
                    items.append(_item(entry.url, entry.lastmod, "time"))
                continue
            day = tashkent_date(entry.lastmod)
        else:
            day = entry.lastmod
        # lastmod не раньше публикации, так что всё опубликованное в пробеле проходит фильтр
        if day >= gap_day:
            items.append(_item(entry.url, day_end_utc(day), "day"))
    return items


def _feed_pages(
    http: HttpClient, source: Source, config: Backfill, ctx: FetchContext, max_pages: int
) -> list[RawItem]:
    items: list[RawItem] = []
    for page in range(2, max_pages + 1):
        fetched = http.fetch(config.url.format(n=page))
        page_items = rss.parse(fetched.content, fetched.url)
        items += [i for i in page_items if i.published_at is None or i.published_at >= ctx.since]
        if not needs_backfill(page_items, ctx.gap_start):
            break  # эта страница уже дошла до начала пробела
    return items


def _item(url: str, published_at: datetime, precision: Precision) -> RawItem:
    return RawItem(url, "", None, None, None, published_at, precision)


def _fill(template: str, day: date) -> str:
    return template.format(Y=day.year, m=day.month, d=day.day, MM=f"{day.month:02d}", DD=f"{day.day:02d}")


def _months(since: datetime, until: datetime) -> list[date]:
    first, last = tashkent_date(since).replace(day=1), tashkent_date(until).replace(day=1)
    months = []
    while first <= last:
        months.append(first)
        first = first.replace(year=first.year + first.month // 12, month=first.month % 12 + 1)
    return months


def _lastmod(value: str) -> datetime | date | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if len(value) <= 10:
        return parsed.date()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TASHKENT)
