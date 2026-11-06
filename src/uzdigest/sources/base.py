"""Общий интерфейс адаптеров источников.

Адаптер работает в потоке сборщика и в БД не ходит: всё, что ему нужно знать о прошлых
запусках, передаётся в FetchContext. Результат пишет в БД основной поток (collect.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from uzdigest.models import LegalAct, RawItem


class SourceError(Exception):
    """Источник ответил, но не в том виде, который ждёт адаптер (лента пуста, API сменил формат)."""


@dataclass(frozen=True)
class FetchContext:
    now: datetime  # UTC
    since: datetime  # раньше этого момента материалы не нужны (потолок окна)
    gap_start: datetime  # с какого момента лента должна покрывать время без пробела
    known_urls: frozenset[str] = frozenset()  # канонические адреса материалов источника, уже лежащих в БД
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class FetchOutcome:
    items: list[RawItem] = field(default_factory=list)
    acts: list[LegalAct] = field(default_factory=list)
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    warnings: list[str] = field(default_factory=list)  # частичные сбои, которые не роняют источник
