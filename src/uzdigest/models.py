"""Общие структуры данных конвейера."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Precision = Literal["time", "day"]


@dataclass(frozen=True)
class RawItem:
    """Материал в том виде, в каком его отдал источник (текст уже без HTML).

    Пустой title — источник отдал только ссылку (HTML-список, sitemap); заголовок заполнит enrich.
    """

    url: str
    title: str
    guid: str | None
    lead: str | None
    body: str | None
    published_at: datetime | None  # UTC
    published_precision: Precision = "time"
    categories: tuple[str, ...] = ()  # рубрики RSS


@dataclass(frozen=True)
class Item:
    """Материал, готовый к записи в таблицу items."""

    url: str
    canonical_url: str
    guid: str | None
    title: str
    title_norm: str
    lead: str | None
    body: str | None
    lang: str
    published_at: datetime  # UTC
    fetched_at: datetime  # UTC
    published_precision: Precision = "time"
    categories: tuple[str, ...] = ()

    @property
    def text_status(self) -> str | None:
        return "feed" if self.body else None


@dataclass(frozen=True)
class LegalAct:
    """Реквизиты правового акта для справочника legal_acts."""

    doc_key: str  # UP-236-2026, MJ-1010-2 — см. legal/doc_key.py
    doc_type: str  # код вида: ZRU, UP, PP, RP, PKM, SENATE, MJ, COURT
    number: str
    issuer: str | None
    title: str | None
    adopted_on: date | None
    effective_on: date | None
    lex_id: int | None  # русская версия
    lex_url: str | None
    source: Literal["lex_rss", "lex_search", "lex_page"]
    uz_lex_id: int | None = None  # узбекская версия, кириллица
    uz_latn_lex_id: int | None = None  # узбекская версия, латиница
    en_lex_id: int | None = None  # перевод на английский


@dataclass(frozen=True)
class FxRate:
    date: date
    ccy: str
    nominal: int
    rate: float
    diff: float | None
