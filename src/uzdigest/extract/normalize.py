"""Нормализация: канонический URL, текст из HTML, заголовок для сравнения, сборка Item."""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from uzdigest.models import Item, RawItem

TRACKING_PARAMS = {"from", "ref", "fbclid", "gclid", "yclid", "ysclid", "_openstat"}
BLOCK_TAGS = {
    "p",
    "br",
    "div",
    "li",
    "ul",
    "ol",
    "tr",
    "blockquote",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
SKIP_TAGS = {"script", "style", "figcaption"}
APOSTROPHES = re.compile(r"[‘’ʻʼ'`]")


def canonical_url(url: str) -> str:
    """Один и тот же материал → одна строка: https, без www, без трекинговых параметров и якоря."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    )
    return urlunsplit(("https", host, path, urlencode(query), ""))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    """Текст без тегов и сущностей; абзацы разделены переводом строки."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ").replace("­", "")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def normalize_title(title: str) -> str:
    """Заголовок для сравнения: нижний регистр, ё→е, без апострофов и пунктуации."""
    text = APOSTROPHES.sub("", title.lower().replace("ё", "е"))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_item(raw: RawItem, lang: str, fetched_at: datetime) -> Item:
    # Дата из будущего или её отсутствие — берём момент сбора. Для дат с точностью до дня
    # источник передаёт конец суток, и так получается «момент обнаружения, но не позже того дня»
    published_at = raw.published_at if raw.published_at and raw.published_at <= fetched_at else fetched_at
    return Item(
        url=raw.url,
        canonical_url=canonical_url(raw.url),
        guid=raw.guid,
        title=raw.title,
        title_norm=normalize_title(raw.title),
        lead=raw.lead,
        body=raw.body,
        lang=lang,
        published_at=published_at,
        fetched_at=fetched_at,
        published_precision=raw.published_precision,
        categories=raw.categories,
    )
