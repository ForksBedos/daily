"""Сверка упоминаний актов с lex.uz (docs/plan/stage-4-lex.md, «Процесс сверки»).

Статусы item_legal_refs.verify_status:
  verified  — акт есть на lex.uz: в справочнике legal_acts или найден поиском, хотя бы на узбекском;
  draft     — в тексте сказано, что это проект, а в справочнике акта нет: не ищем;
  pending   — на lex.uz пока нет; ищется в каждом запуске, пока материалу меньше pending_days дней;
  not_found — так и не появился: проверить вручную.
Сетевая ошибка или непонятная вёрстка lex.uz — ошибка в логе и pending, но никогда не ложный verified.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import TypeVar

import httpx

from uzdigest.config import LexSettings
from uzdigest.db import repo
from uzdigest.legal.doc_key import split_doc_key
from uzdigest.legal.lex_doc import DocHeader, parse_doc
from uzdigest.legal.lex_search import (
    LexLayoutError,
    SearchResult,
    doc_url,
    find_act,
    parse_search,
    search_url,
)
from uzdigest.models import LegalAct
from uzdigest.timeutil import from_iso, tashkent_date, utc_now
from uzdigest.web.http import HttpClient, RobotsDisallowed

log = logging.getLogger(__name__)

LEX_ERRORS = (httpx.HTTPError, RobotsDisallowed, LexLayoutError)

T = TypeVar("T", SearchResult, DocHeader)


@dataclass
class VerifyStats:
    by_key: dict[str, Counter[str]] = field(default_factory=dict)  # doc_key → статусы его упоминаний
    requests: int = 0  # запросов к lex.uz
    cached: int = 0  # ответов из кэша lex_lookups
    errors: list[str] = field(default_factory=list)
    became_verified: list[str] = field(default_factory=list)  # были pending, появились на lex.uz

    @property
    def statuses(self) -> Counter[str]:
        return sum(self.by_key.values(), Counter())


class LexVerifier:
    def __init__(
        self, conn: sqlite3.Connection, http: HttpClient, settings: LexSettings, now: datetime | None = None
    ) -> None:
        self.conn = conn
        self.http = http
        self.settings = settings
        self.now = now or utc_now()
        self.stats = VerifyStats()

    def verify(self, refs: list[sqlite3.Row]) -> VerifyStats:
        """Ставит статусы упоминаниям (repo.refs_to_verify: отсортированы по doc_key)."""
        expired_before = self.now - timedelta(days=self.settings.pending_days)
        for doc_key, rows in groupby(refs, key=lambda r: r["doc_key"]):
            group = list(rows)
            counter = self.stats.by_key.setdefault(doc_key, Counter())
            with self.conn:
                act = self.resolve(doc_key, search=any(not r["is_draft"] for r in group))
                for ref in group:
                    if act is not None and not (ref["year_guessed"] and self._too_old_for_guess(act, ref)):
                        status = "verified"
                    elif ref["is_draft"]:
                        status = "draft"
                    elif from_iso(ref["published_at"]) < expired_before:
                        status = "not_found"
                    else:
                        status = "pending"
                    if ref["verify_status"] == "pending" and status == "verified":
                        if doc_key not in self.stats.became_verified:
                            self.stats.became_verified.append(doc_key)
                    repo.save_verify_status(self.conn, ref["item_id"], doc_key, status)
                    counter[status] += 1
        return self.stats

    def _too_old_for_guess(self, act: sqlite3.Row, ref: sqlite3.Row) -> bool:
        """Рядом с номером нет даты, год взят по дате новости: верим ему, только если акт свежий.

        Иначе «ПКМ № 440 от 2019 года» без распознанной даты подтвердился бы чужим ПКМ № 440 этого года.
        """
        if act["adopted_on"] is None:
            return False
        published = tashkent_date(from_iso(ref["published_at"]))
        return date.fromisoformat(act["adopted_on"]) < published - timedelta(
            days=self.settings.guessed_year_days
        )

    def resolve(self, doc_key: str, *, search: bool = True) -> sqlite3.Row | None:
        """Акт из справочника, а если его там нет — найденный поиском на lex.uz. None — на lex.uz нет.

        Найденный акт дополняется со страницы документа: дата вступления в силу и версии на других языках.
        """
        act = repo.legal_act(self.conn, doc_key)
        if act is None and search:
            act = self._search(doc_key)
        if act is not None and _incomplete(act):
            act = self._complete(act)
        if act is not None and search and act["uz_lex_id"] is None and act["uz_latn_lex_id"] is None:
            # Акт из RSS: страница свежего акта первые дни не ссылается на узбекский оригинал, поиск видит
            act = self._search(doc_key) or act
        return act

    def _search(self, doc_key: str) -> sqlite3.Row | None:
        url = search_url(*split_doc_key(doc_key))
        if url is None:
            return None  # Сенат и суды поиском не ищутся, только по справочнику из RSS
        result = self._fetch(url, parse_search, SearchResult.from_json)
        if result is None:
            return None
        act = find_act(result, doc_key)
        if act is None:
            if result.total > len(result.rows):
                log.warning(
                    "%s: из %s результатов просмотрены первые %s (%s)",
                    doc_key,
                    result.total,
                    len(result.rows),
                    url,
                )
            return None
        log.info(
            "%s найден на lex.uz: ru %s, uz %s / %s", doc_key, act.lex_id, act.uz_lex_id, act.uz_latn_lex_id
        )
        repo.upsert_legal_act(self.conn, act)
        return repo.legal_act(self.conn, doc_key)

    def _complete(self, act: sqlite3.Row) -> sqlite3.Row:
        page_id = act["lex_id"] or act["uz_lex_id"] or act["uz_latn_lex_id"]
        if page_id is None:
            return act
        header = self._fetch(doc_url(page_id), parse_doc, DocHeader.from_json)
        if header is None:
            return act
        if header.parsed_label().doc_key != act["doc_key"]:
            self._error(f"страница {doc_url(page_id)} не про {act['doc_key']}: «{header.label}»")
            return act
        ids = header.version_ids(page_id)
        repo.upsert_legal_act(
            self.conn,
            LegalAct(
                doc_key=act["doc_key"],
                doc_type=act["doc_type"],
                number=act["number"],
                issuer=None,
                title=None,
                adopted_on=None,
                effective_on=header.effective_on(),
                lex_id=ids.get("ru"),
                lex_url=doc_url(ids["ru"]) if "ru" in ids else None,
                source="lex_page",
                uz_lex_id=ids.get("uz_cyrl"),
                uz_latn_lex_id=ids.get("uz_latn"),
                en_lex_id=ids.get("en"),
            ),
        )
        return repo.legal_act(self.conn, act["doc_key"])

    def _fetch(self, url: str, parse: Callable[[str], T], load: Callable[[str], T]) -> T | None:
        cached = repo.lex_lookup(self.conn, url, self.now - timedelta(hours=self.settings.cache_h))
        if cached is not None:
            self.stats.cached += 1
            return load(cached)
        self.stats.requests += 1
        try:
            result = parse(self.http.fetch(url).content.decode("utf-8", errors="replace"))
        except LEX_ERRORS as exc:
            self._error(f"{url}: {type(exc).__name__}: {exc}")
            return None
        repo.save_lex_lookup(self.conn, url, result.to_json(), self.now)
        return result

    def _error(self, message: str) -> None:
        log.error("lex.uz: %s", message)
        self.stats.errors.append(message)


def _incomplete(act: sqlite3.Row) -> bool:
    """Нет даты вступления в силу или какой-то из версий: стоит заглянуть на страницу документа.

    Английский перевод и русская версия появляются через несколько дней после оригинала,
    поэтому страница перечитывается, пока акт попадает в сверку (не чаще, чем живёт кэш).
    """
    return (
        act["effective_on"] is None
        or act["lex_id"] is None
        or act["en_lex_id"] is None
        or (act["uz_lex_id"] is None and act["uz_latn_lex_id"] is None)
    )


def verify_window(
    conn: sqlite3.Connection,
    http: HttpClient,
    settings: LexSettings,
    since: datetime,
    until: datetime,
    late_from: datetime | None = None,
) -> VerifyStats:
    """Сверка упоминаний актов в материалах окна и всех ещё не найденных (pending)."""
    refs = repo.refs_to_verify(conn, since, until, late_from=late_from)
    return LexVerifier(conn, http, settings).verify(refs)
