"""Модель дайджеста — она же схема digest.json (docs/plan/stage-6-render.md).

Модель не зависит от языка файла: разделы, статусы и виды актов хранятся кодами, даты — датами.
Подписи на ru, en и uz подставляет render/markdown.py. Генератор постов (этап 7) и бот (этап 10)
читают только digest.json. Та же модель лежит в digests.model_json: по ней rerender пересобирает
файлы без сети, а следующие дайджесты находят акты для блока «Обновления».
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LANGS = ("ru", "en", "uz")
Lang = Literal["ru", "en", "uz"]
DocStatus = Literal["verified", "pending", "draft", "not_found"]


class Text(BaseModel):
    headline: str
    summary: str
    details: str  # пусто у статьи без ИИ-обработки


class LexLink(BaseModel):
    url: str
    lang: Lang  # язык документа по ссылке; не совпадает с языком файла, если перевода на lex.uz нет


class Doc(BaseModel):
    doc_key: str  # UP-236-2026
    doc_type: str  # UP, PP, ZRU, PKM, MJ, …
    number: str  # 236, 1010-2
    adopted_on: date | None
    effective_on: date | None
    status: DocStatus | None
    lex: dict[Lang, LexLink] = Field(default_factory=dict)  # ссылка для файла на каждом языке


class SourceRef(BaseModel):
    code: str  # код источника из sources.yaml
    name: str  # домен: norma.uz, gazeta.uz
    url: str
    published_at: datetime
    time_known: bool = True  # False — источник знает только дату публикации


class Article(BaseModel):
    id: str  # 2026-11-02-001, одинаков во всех языках
    cluster_id: int
    category: str  # legislation, taxes, accounting, finance, business, macro
    importance: int
    doc: Doc | None  # только у правовых
    source: SourceRef
    also: list[SourceRef]
    texts: dict[Lang, Text | None]  # ru есть всегда; en и uz — None, если перевод не готов
    by_llm: bool  # False — «без ИИ-обработки»: заголовок и начало материала
    flags: list[str]  # замечания проверки фактов: «⚠️ проверить»


class Rate(BaseModel):
    date: date  # дата курса ЦБ
    ccy: str
    nominal: int
    rate: float
    diff: float | None


class Update(BaseModel):
    """Акт выходил в прошлом дайджесте как «ожидает публикации», а теперь есть на lex.uz."""

    doc: Doc
    previous_date: date
    headlines: dict[Lang, str]  # заголовок той статьи на каждом языке (русский, если перевода не было)


class SourceFailure(BaseModel):
    code: str
    name: str
    error: str | None  # текст ошибки последнего опроса
    last_ok_at: datetime | None  # если ошибки нет: опроса не было с начала окна


class Failures(BaseModel):
    sources: list[SourceFailure]
    no_llm: int
    no_translation: int  # статей, у которых нет хотя бы одного перевода


class Window(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    since: datetime = Field(alias="from")
    until: datetime = Field(alias="to")


class Generator(BaseModel):
    version: str  # uzdigest
    model: str  # Claude
    prompts: list[str]


class Digest(BaseModel):
    date: date
    window: Window
    generator: Generator
    sources_ok: int
    sources_total: int
    rates: list[Rate]
    articles: list[Article]
    updates: list[Update]
    failures: Failures

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Digest:
        return cls.model_validate_json(text)
