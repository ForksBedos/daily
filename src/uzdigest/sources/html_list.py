"""Общий адаптер HTML-списков: со страницы-списка берутся ссылки по шаблону.

Заголовок — текст ссылки (если есть), дата — первое совпадение date_pattern между этой
ссылкой и следующей. Полный текст и недостающий заголовок заполняет стадия enrich.
Даты в списках известны с точностью до дня, поэтому published_at = конец того дня.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape
from urllib.parse import urljoin

from uzdigest.config import HtmlListParams, Source
from uzdigest.extract.normalize import canonical_url, html_to_text
from uzdigest.models import RawItem
from uzdigest.sources.base import FetchContext, FetchOutcome, SourceError
from uzdigest.timeutil import day_end_utc
from uzdigest.web.http import HttpClient

ANCHOR = re.compile(r"<a\s[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def fetch(http: HttpClient, source: Source, ctx: FetchContext) -> FetchOutcome:
    params = HtmlListParams.model_validate(source.params)
    return FetchOutcome(items=collect_links(http, source, params, ctx, strict=True))


def collect_links(
    http: HttpClient,
    source: Source,
    params: HtmlListParams,
    ctx: FetchContext,
    max_pages: int | None = None,
    *,
    strict: bool = False,
) -> list[RawItem]:
    """Обходит страницы-списки. Листает, пока на странице есть новые ссылки не старше ctx.since.

    strict — первая страница без единой ссылки на статью это сбой: сайт сменил вёрстку или отдал
    заглушку. Спокойный день выглядит иначе: ссылки есть, но все старые.
    """
    pages = max_pages or params.max_pages
    found: dict[str, RawItem] = {}
    for list_url in params.list_urls or [source.url]:
        for page in range(1, pages + 1):
            if page > 1 and not params.pages:
                break
            url = list_url if page == 1 else list_url + params.pages.format(n=page)
            fetched = http.fetch(url)
            links = parse(fetched.content.decode("utf-8", errors="replace"), fetched.url, params)
            if strict and page == 1 and not links:
                raise SourceError(f"на странице нет ссылок на статьи: {url}")
            fresh = [
                item
                for item in links
                if canonical_url(item.url) not in found
                and (item.published_at is None or item.published_at >= ctx.since)
            ]
            if not fresh:
                break  # дальше только старое или сайт вернул ту же страницу
            found.update((canonical_url(item.url), item) for item in fresh)
    return list(found.values())


def parse(html: str, base_url: str, params: HtmlListParams) -> list[RawItem]:
    link_pattern = re.compile(params.link_pattern)
    links: list[tuple[int, str, str]] = []  # позиция, адрес, текст ссылки
    for match in ANCHOR.finditer(html):
        href = unescape(match.group(1)).strip()
        if link_pattern.search(href):
            links.append((match.start(), urljoin(base_url, href), html_to_text(match.group(2))))

    # Одна статья может встречаться в списке несколько раз (картинка, заголовок, «читать далее»)
    first: dict[str, tuple[int, str, str]] = {}
    for position, url, text in links:
        key = canonical_url(url)
        if key not in first:
            first[key] = (position, url, text)
        elif text and not first[key][2]:
            first[key] = (first[key][0], first[key][1], text)

    ordered = sorted(first.values())
    items = []
    for index, (position, url, title) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else len(html)
        day = _date(html[position:end], params)
        items.append(
            RawItem(
                url=url,
                title=title,
                guid=None,
                lead=None,
                body=None,
                published_at=day_end_utc(day) if day else None,
                published_precision="day",
            )
        )
    return items


def _date(fragment: str, params: HtmlListParams) -> date | None:
    if not (params.date_pattern and params.date_format):
        return None
    match = re.search(params.date_pattern, fragment)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).strip(), params.date_format).date()
    except ValueError:
        return None
