"""Префильтр (docs/plan/stage-3-dedup.md, задача 3.1): дешёвый первый барьер «на полноту».

Источники уровней A и B проходят всегда. Уровень C отсекается в двух случаях:
  - стоп-рубрика (категория RSS или адрес по exclude_url_patterns источника), и в заголовке
    нет тематического слова;
  - стоп-тема в заголовке («футбол», «ДТП», «погода»), и ни в заголовке, ни в начале текста
    (LEAD_CHARS знаков) нет тематического слова.
Всё остальное проходит дальше; пограничное решает Claude на этапе 5.

Отличия от плана (проверено на 589 материалах 30.10–02.11.2026):
  - по стоп-рубрике план отсекал без условий; здесь тематическое слово в заголовке спасает
    материал: в «Происшествиях» бывают дела о мошенничестве застройщиков и банков;
  - тематическое слово для стоп-темы ищется не во всём тексте, а в начале: в длинной заметке
    о футболе почти всегда есть «сумов» или «компания», и фильтр снимал всего 7 % уровня C.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from uzdigest.config import CONFIG_DIR, Source

_WORD = re.compile(r"[a-zа-яёўқғҳ0-9]+", re.I)
LEAD_CHARS = 300


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str  # tier | rubric:<рубрика> | topic:<слово> | pass


class Keywords:
    """Словари из config/keywords.yaml. «слово$» — совпадение целиком, иначе по началу слова."""

    def __init__(self, topic: list[str], stop_topics: list[str], stop_categories: list[str]):
        self.topic = _Matcher(topic)
        self.stop_topics = _Matcher(stop_topics)
        self.stop_categories = {c.casefold() for c in stop_categories}

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "keywords.yaml") -> Keywords:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(data["topic"], data["stop_topics"], data.get("stop_categories", []))


class _Matcher:
    def __init__(self, entries: list[str]):
        self.exact = {e[:-1].casefold() for e in entries if e.endswith("$")}
        self.prefixes = tuple(e.casefold() for e in entries if not e.endswith("$"))

    def first(self, text: str) -> str | None:
        """Первое слово текста, которое есть в словаре."""
        for word in _WORD.findall(text.casefold().replace("ё", "е")):
            if word in self.exact or word.startswith(self.prefixes):
                return word
        return None


def prefilter(
    *, title: str, text: str | None, url: str, categories: list[str], source: Source, keywords: Keywords
) -> Verdict:
    if source.tier in ("A", "B"):
        return Verdict(True, "tier")

    rubric = next((c for c in categories if c.casefold() in keywords.stop_categories), None)
    if rubric is None:
        rubric = next((p for p in source.exclude_url_patterns if p in url), None)
    if rubric is not None and keywords.topic.first(title) is None:
        return Verdict(False, f"rubric:{rubric}")

    stop = keywords.stop_topics.first(title)
    if stop is not None and keywords.topic.first(f"{title}\n{(text or '')[:LEAD_CHARS]}") is None:
        return Verdict(False, f"topic:{stop}")
    return Verdict(True, "pass")
