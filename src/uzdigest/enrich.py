"""Стадия enrich: полный текст для материалов, которые пришли без него (анонс в RSS, ссылка из списка).

Страницы скачиваются параллельно по доменам и последовательно внутри домена: паузы между
запросами к одному сайту соблюдает HttpClient. В БД пишет только основной поток.
Неудачная страница повторяется в следующих запусках, но не больше enrich.max_attempts раз.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from uzdigest.config import EnrichSettings, Source
from uzdigest.db import repo
from uzdigest.extract.fulltext import ExtractError, clean_text, fetch_article
from uzdigest.extract.normalize import normalize_title
from uzdigest.timeutil import day_end_utc, from_iso, utc_now
from uzdigest.web.http import HttpClient, RobotsDisallowed

log = logging.getLogger(__name__)

PAGE_ERRORS = (httpx.HTTPError, RobotsDisallowed, ExtractError)


@dataclass
class EnrichStats:
    fetched: int = 0
    failed: int = 0
    skipped: int = 0
    failed_by_source: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Result:
    item_id: int
    source: str
    title: str = ""
    body: str = ""
    published_at: datetime | None = None
    error: str | None = None


def enrich(
    conn: sqlite3.Connection,
    http: HttpClient,
    sources: list[Source],
    settings: EnrichSettings,
    *,
    lookback_h: int = 96,
    max_workers: int = 6,
    only: set[str] | None = None,
) -> EnrichStats:
    """only — сбор части источников: материалы остальных дождутся полного сбора."""
    by_code = {s.code: s for s in sources}
    rows = repo.items_to_enrich(
        conn, utc_now() - timedelta(hours=lookback_h), settings.max_attempts, settings.max_pages_per_run
    )
    stats = EnrichStats()

    by_domain: dict[str, list[sqlite3.Row]] = defaultdict(list)
    skipped = []
    for row in rows:
        if only is not None and row["source"] not in only:
            continue
        source = by_code.get(row["source"])
        if source is None or not source.fulltext:
            skipped.append(row["id"])
        else:
            by_domain[urlsplit(row["url"]).netloc].append(row)
    repo.mark_text_skipped(conn, skipped)
    stats.skipped = len(skipped)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_domain, http, batch, by_code, settings) for batch in by_domain.values()]
        for future in as_completed(futures):
            with conn:
                for result in future.result():
                    _save(conn, result, stats)

    log.info(
        "enrich: скачано=%d неудач=%d пропущено=%d %s",
        stats.fetched,
        stats.failed,
        stats.skipped,
        stats.failed_by_source or "",
    )
    return stats


def _fetch_domain(
    http: HttpClient, rows: list[sqlite3.Row], by_code: dict[str, Source], settings: EnrichSettings
) -> list[_Result]:
    return [_fetch_one(http, row, by_code[row["source"]], settings) for row in rows]


def _fetch_one(http: HttpClient, row: sqlite3.Row, source: Source, settings: EnrichSettings) -> _Result:
    item_id, code = row["id"], row["source"]
    try:
        article = fetch_article(http, row["url"])
    except PAGE_ERRORS as exc:
        return _Result(item_id, code, error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        # Ошибка разбора одной страницы не должна останавливать весь сбор и утренний дайджест
        log.exception("Страница %s не разобрана", row["url"])
        return _Result(item_id, code, error=f"{type(exc).__name__}: {exc}")

    title = row["title"] or article.title or ""
    if not title:
        return _Result(item_id, code, error="на странице нет заголовка")
    body = clean_text(
        article.text, title=title, drop_lines=source.text_drop_lines, stop_lines=source.text_stop_lines
    )
    if len(body) < settings.min_text_chars:
        return _Result(item_id, code, error=f"текст короче {settings.min_text_chars} знаков ({len(body)})")

    published_at = from_iso(row["published_at"])
    if row["published_precision"] == "day" and article.day:
        # Ссылка из списка или sitemap: дата со страницы точнее, чем момент обнаружения
        published_at = min(published_at, day_end_utc(article.day))
    return _Result(item_id, code, title=title, body=body, published_at=published_at)


def _save(conn: sqlite3.Connection, result: _Result, stats: EnrichStats) -> None:
    if result.error:
        repo.mark_text_failed(conn, result.item_id, result.error)
        stats.failed += 1
        stats.failed_by_source[result.source] = stats.failed_by_source.get(result.source, 0) + 1
        log.info("Нет текста: item %s (%s): %s", result.item_id, result.source, result.error)
        return
    repo.save_text(
        conn, result.item_id, result.title, normalize_title(result.title), result.body, result.published_at
    )
    stats.fetched += 1
