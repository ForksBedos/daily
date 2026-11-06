"""Стадия collect: опрос включённых источников, догрузка пробелов и запись новых материалов в items.

Сеть опрашивается параллельно, в БД пишет только основной поток. Сбой одного источника
не останавливает остальные: он записывается в sources.last_error и в статистику запуска.
lex.uz пишет не в items, а в справочник legal_acts.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import httpx

from uzdigest.config import BackfillSettings, RatesSettings, Source
from uzdigest.db import repo
from uzdigest.extract.normalize import canonical_url, to_item
from uzdigest.redact import mask
from uzdigest.sources import backfill, cbu_rates, govuz_api, html_list, lex_rss, rss
from uzdigest.sources.base import FetchContext, FetchOutcome, SourceError
from uzdigest.timeutil import from_iso, utc_now
from uzdigest.web.http import HttpClient, RobotsDisallowed

log = logging.getLogger(__name__)

Adapter = Callable[[HttpClient, Source, FetchContext], FetchOutcome]
ADAPTERS: dict[str, Adapter] = {
    "rss": rss.fetch,
    "govuz_api": govuz_api.fetch,
    "html_list": html_list.fetch,
    "lex_rss": lex_rss.fetch,
}
FETCH_ERRORS = (httpx.HTTPError, RobotsDisallowed, SourceError)
KNOWN_URLS_DAYS = 30  # насколько глубоко помнить адреса, чтобы не скачивать известное повторно


@dataclass
class SourceResult:
    code: str
    status: str  # ok | not_modified | error
    fetched: int = 0
    new: int = 0
    backfilled: int = 0  # из них пришло догрузкой
    error: str | None = None
    warnings: int = 0
    gap_h: float | None = None  # лента без догрузки короче пробела: столько часов могло пройти мимо


@dataclass
class _Fetched:
    outcome: FetchOutcome
    backfilled: int = 0
    backfill_failed: bool = False
    gap_h: float | None = None


def collect(
    conn: sqlite3.Connection,
    http: HttpClient,
    sources: list[Source],
    *,
    lookback_h: int = 96,
    backfill_settings: BackfillSettings | None = None,
    max_workers: int = 6,
    only: set[str] | None = None,
    gap_from: datetime | None = None,
) -> list[SourceResult]:
    """backfill_settings=None — без догрузки.

    gap_from — считать пробелом всё время с этого момента, а не с последнего успешного опроса
    (после простоя ноутбука, для проверки понедельничного окна). Не глубже lookback_h.
    """
    source_ids = repo.sync_sources(conn, sources)
    targets = [s for s in sources if s.enabled and (only is None or s.code in only)]
    run_id = repo.start_run(conn, "collect")

    now = utc_now()
    since = now - timedelta(hours=lookback_h)
    results: list[SourceResult] = []
    try:
        contexts = {s.code: _context(conn, source_ids[s.code], now, since, gap_from) for s in targets}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch, http, s, contexts[s.code], backfill_settings): s for s in targets}
            for future in as_completed(futures):
                source = futures[future]
                results.append(_store(conn, source, source_ids[source.code], run_id, future))
    except BaseException as exc:
        repo.finish_run(conn, run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise

    failed = sum(r.status == "error" for r in results)
    status = "ok" if not failed else ("failed" if failed == len(targets) else "partial")
    repo.finish_run(conn, run_id, status, {"sources": [asdict(r) for r in results]})
    for r in results:
        log.info(
            "%-10s %-12s получено=%-3d новых=%-3d догружено=%-3d %s",
            r.code, r.status, r.fetched, r.new, r.backfilled, r.error or "",
        )  # fmt: skip
    return sorted(results, key=lambda r: r.code)


def collect_rates(conn: sqlite3.Connection, http: HttpClient, settings: RatesSettings) -> str:
    """Курсы ЦБ на дату последнего курса. Возвращает эту дату (ГГГГ-ММ-ДД)."""
    rates = cbu_rates.fetch(http, settings.url)
    repo.upsert_fx_rates(conn, rates)
    return max(r.date for r in rates).isoformat()


def _context(
    conn: sqlite3.Connection, source_id: int, now: datetime, since: datetime, gap_from: datetime | None
) -> FetchContext:
    state = repo.source_state(conn, source_id)
    gap_start = gap_from or (from_iso(state["last_ok_at"]) if state["last_ok_at"] else since)
    return FetchContext(
        now=now,
        since=since,
        gap_start=max(gap_start, since),
        known_urls=repo.known_urls(conn, source_id, now - timedelta(days=KNOWN_URLS_DAYS)),
        etag=state["etag"],
        last_modified=state["last_modified"],
    )


def _fetch(
    http: HttpClient, source: Source, ctx: FetchContext, backfill_settings: BackfillSettings | None
) -> _Fetched:
    """Работает в потоке: опрос источника и, если в ленте пробел, догрузка. В БД не ходит."""
    outcome = ADAPTERS[source.kind](http, source, ctx)
    if outcome.not_modified or not backfill.needs_backfill(outcome.items, ctx.gap_start):
        return _Fetched(outcome)
    if source.backfill is None:
        return _Fetched(outcome, gap_h=_feed_gap(source, outcome, ctx))
    if backfill_settings is None:
        return _Fetched(outcome)
    in_feed = frozenset(canonical_url(i.url) for i in outcome.items)
    try:
        extra = backfill.fetch(http, source, ctx, backfill_settings, exclude=in_feed)
    except FETCH_ERRORS as exc:
        # Лента уже получена: сбой догрузки не должен её терять
        outcome.warnings.append(f"догрузка: {type(exc).__name__}: {exc}")
        return _Fetched(outcome, backfill_failed=True)
    outcome.items.extend(extra)
    return _Fetched(outcome, backfilled=len(extra))


def _feed_gap(source: Source, outcome: FetchOutcome, ctx: FetchContext) -> float | None:
    """Лента не достаёт до прошлого опроса, а догрузки у источника нет: часть новостей могла пройти мимо."""
    dated = [item.published_at for item in outcome.items if item.published_at]
    if source.kind != "rss" or not dated:
        return None
    hours = round((min(dated) - ctx.gap_start).total_seconds() / 3600, 1)
    outcome.warnings.append(
        f"лента не достаёт до прошлого опроса на {hours} ч: часть новостей могла пройти мимо"
    )
    return hours


def _store(
    conn: sqlite3.Connection, source: Source, source_id: int, run_id: int, future: Future[_Fetched]
) -> SourceResult:
    try:
        fetched = future.result()
    except FETCH_ERRORS as exc:
        return _fail(conn, source, source_id, f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        # Ошибка в разборе одного источника не должна оставить без новостей остальные
        log.exception("Источник %s: неожиданная ошибка", source.code)
        return _fail(conn, source, source_id, f"{type(exc).__name__}: {exc}")

    outcome = fetched.outcome
    for warning in outcome.warnings:
        log.warning("Источник %s: %s", source.code, warning)
    if outcome.not_modified:
        with conn:
            repo.mark_source_ok(conn, source_id, outcome.etag, outcome.last_modified)
        return SourceResult(source.code, "not_modified")

    fetched_at = utc_now()
    with conn:
        new = sum(
            repo.insert_item(conn, source_id, run_id, to_item(raw, source.lang, fetched_at))
            for raw in outcome.items
        )
        new += sum(repo.upsert_legal_act(conn, act) for act in outcome.acts)
        # Сбой догрузки: пробел не закрыт, следующий опрос должен увидеть его снова и не получить 304
        if not fetched.backfill_failed:
            repo.mark_source_ok(conn, source_id, outcome.etag, outcome.last_modified)
    return SourceResult(
        source.code,
        "ok",
        fetched=len(outcome.items) + len(outcome.acts),
        new=new,
        backfilled=fetched.backfilled,
        warnings=len(outcome.warnings),
        gap_h=fetched.gap_h,
    )


def _fail(conn: sqlite3.Connection, source: Source, source_id: int, error: str) -> SourceResult:
    error = mask(error)
    log.warning("Источник %s: %s", source.code, error)
    repo.mark_source_error(conn, source_id, error)
    return SourceResult(source.code, "error", error=error)
