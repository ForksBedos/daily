"""Страница документа lex.uz (/ru/docs/{id}): шапка docHeader и версии на других языках.

Текст документа подгружается скриптом, он нам не нужен. Из шапки берём строку реквизитов
(по ней проверяем, что это тот самый акт) и дату вступления в силу, из переключателя языков —
id русской, узбекских (кириллица и латиница) и английской версий.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from uzdigest.legal.lex_search import BASE_URL, Label, LexLayoutError, doc_url, parse_label

# Подпись в переключателе языков → версия
_VERSION_TITLES = {"На русском": "ru", "Ўзбекча": "uz_cyrl", "O'zbekcha": "uz_latn", "In english": "en"}


@dataclass(frozen=True)
class DocHeader:
    label: str  # строка реквизитов: «Постановление Президента …, от 27.10.2026 г. № ПП-387»
    effective: str  # дата вступления в силу ДД.ММ.ГГГГ; пусто, если на странице её нет
    versions: dict[str, int]  # подпись в переключателе языков → id

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> DocHeader:
        return cls(**json.loads(text))

    def parsed_label(self) -> Label:
        return parse_label(self.label)

    def effective_on(self) -> date | None:
        return datetime.strptime(self.effective, "%d.%m.%Y").date() if self.effective else None

    def version_ids(self, own_id: int) -> dict[str, int]:
        """id версий по языкам (ru, uz_cyrl, uz_latn, en), включая саму страницу."""
        ids = {_VERSION_TITLES[t]: lex_id for t, lex_id in self.versions.items() if t in _VERSION_TITLES}
        own_lang = self.parsed_label().lang
        if own_lang != "other":
            ids.setdefault(own_lang, own_id)  # текущий язык в переключателе не ссылка
        return ids


_HEADER_START = '<div class="docHeader'
_HEADER_SIZE = 6000  # реквизиты и дата вступления в силу — в начале блока
_HEADER_LABEL = re.compile(r'<div class="docHeader__item-label">([^<]+)<')
_HEADER_VALUE = re.compile(r'<div class="docHeader__item-value[^"]*"[^>]*>([^<]*)<')
_VERSION = re.compile(r"""onclick="openUrl\('/ru/docs/(-?\d+)'\)"\s+title="([^"]+)\"""")


def parse_doc(page: str) -> DocHeader:
    start = page.find(_HEADER_START)
    if start < 0:
        raise LexLayoutError("на странице документа lex.uz нет блока docHeader")
    block = page[start : start + _HEADER_SIZE]
    label, value = _HEADER_LABEL.search(block), _HEADER_VALUE.search(block)
    if label is None or value is None:
        raise LexLayoutError("в docHeader lex.uz нет реквизитов или поля «Дата вступления в силу»")
    versions = {html.unescape(title): int(lex_id) for lex_id, title in _VERSION.findall(page)}
    return DocHeader(" ".join(html.unescape(label.group(1)).split()), value.group(1).strip(), versions)


def act_link(act: Mapping[str, Any], lang: str) -> tuple[str, str] | None:
    """Ссылка на акт для дайджеста на языке lang (ru, uz, en) и язык документа по этой ссылке.

    Если версии на языке дайджеста нет, берётся русская, затем узбекская: язык документа тогда
    не совпадает с lang, и дайджест ставит пометку, например «(in Russian)». Узбекская версия —
    латиница (вопрос 5 к команде), если её нет — кириллица.
    """
    ru = (doc_url(act["lex_id"]), "ru") if act["lex_id"] else None
    if act["uz_latn_lex_id"]:
        uz = (f"{BASE_URL}/uz/docs/{act['uz_latn_lex_id']}", "uz")
    elif act["uz_lex_id"]:
        uz = (f"{BASE_URL}/docs/{act['uz_lex_id']}", "uz")
    else:
        uz = None
    en = (f"{BASE_URL}/en/docs/{act['en_lex_id']}", "en") if act["en_lex_id"] else None
    order = {"ru": (ru, uz), "uz": (uz, ru), "en": (en, ru, uz)}[lang]
    return next((link for link in order if link), None)
