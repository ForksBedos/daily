"""Полный текст статьи со страницы: trafilatura + чистка мусорных строк по правилам источника.

HTML скачивается через наш HttpClient, чтобы соблюдались robots.txt и паузы между запросами.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import trafilatura

from uzdigest.web.http import HttpClient


class ExtractError(Exception):
    """Страница скачана, но статью на ней найти не удалось."""


@dataclass(frozen=True)
class Article:
    title: str | None
    day: date | None  # trafilatura знает дату с точностью до дня
    text: str


def fetch_article(http: HttpClient, url: str) -> Article:
    fetched = http.fetch(url)
    return extract(fetched.content, fetched.url)


def extract(html: bytes | str, url: str) -> Article:
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False)
    if doc is None or not (doc.text or "").strip():
        raise ExtractError("trafilatura не нашла текст статьи")
    return Article(title=_squash(doc.title) or None, day=_day(doc.date), text=doc.text)


def clean_text(text: str, *, title: str | None, drop_lines: list[str], stop_lines: list[str]) -> str:
    """Убирает повтор заголовка, строки по drop_lines и всё начиная со строки по stop_lines."""
    drop = [re.compile(p) for p in drop_lines]
    stop = [re.compile(p) for p in stop_lines]
    title_key = _squash(title or "").casefold()
    lines = []
    for raw in text.split("\n"):
        line = _squash(raw)
        if not line:
            continue
        if any(p.search(line) for p in stop):
            break
        if (title_key and line.casefold() == title_key) or any(p.search(line) for p in drop):
            continue
        lines.append(line)
    return "\n".join(lines)


def _squash(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None
