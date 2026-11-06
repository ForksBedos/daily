"""Адаптер JSON API порталов ведомств на gov.uz (api-portal.gov.uz).

Список: GET {url}&page=N с заголовками `code: <ведомство>` и `language: ru|uz`, 12 записей на
страницу, свежие сверху. Полный текст: GET …/authorities/news/view?id=ID, поле body в HTML.
Время в API — ташкентское, без пояса.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ValidationError

from uzdigest.config import GovuzParams, Source
from uzdigest.extract.normalize import canonical_url, html_to_text
from uzdigest.models import RawItem
from uzdigest.sources.base import FetchContext, FetchOutcome, SourceError
from uzdigest.timeutil import tashkent_to_utc
from uzdigest.web.http import HttpClient

log = logging.getLogger(__name__)

API_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _Entry(BaseModel):
    id: int
    date: str
    title: str
    anons: str | None = None


class _ListPage(BaseModel):
    data: list[_Entry]
    total_page: int | None = None


class _Article(BaseModel):
    id: int
    body: str | None = None


class _ViewPage(BaseModel):
    data: _Article


def fetch(http: HttpClient, source: Source, ctx: FetchContext) -> FetchOutcome:
    params = GovuzParams.model_validate(source.params)
    language = params.language or source.lang
    headers = {"code": params.authority, "language": language}
    outcome = FetchOutcome()

    for page in range(1, params.max_pages + 1):
        entries = _list_page(http, f"{source.url}&page={page}", headers)
        if page == 1 and not entries:
            # Лента ведомства не бывает пустой: спокойный день — это старые записи, а не их отсутствие
            raise SourceError(f"API gov.uz вернул пустой список новостей {params.authority}")
        reached_since = False
        for entry in entries:
            published_at = _parse_date(entry.date)
            if published_at < ctx.since:
                reached_since = True
                continue
            url = f"https://gov.uz/{language}/{params.authority}/news/view/{entry.id}"
            if canonical_url(url) in ctx.known_urls:
                continue
            try:
                body = _view_body(http, _view_url(source.url, entry.id), headers)
            except (httpx.HTTPError, SourceError) as exc:
                # Материал не записываем: в следующем запуске он снова будет новым и скачается
                outcome.warnings.append(f"id={entry.id}: {type(exc).__name__}: {exc}")
                continue
            outcome.items.append(
                RawItem(
                    url=url,
                    title=html_to_text(entry.title),
                    guid=str(entry.id),
                    lead=html_to_text(entry.anons or "") or None,
                    body=body or None,
                    published_at=published_at,
                )
            )
        if reached_since or not entries:
            break
    return outcome


def _list_page(http: HttpClient, url: str, headers: dict[str, str]) -> list[_Entry]:
    payload = _json(http, url, headers)
    try:
        return _ListPage.model_validate(payload).data
    except ValidationError as exc:
        raise SourceError(f"API gov.uz сменил формат списка: {exc.error_count()} ошибок, {url}") from exc


def _view_body(http: HttpClient, url: str, headers: dict[str, str]) -> str:
    payload = _json(http, url, headers)
    try:
        article = _ViewPage.model_validate(payload).data
    except ValidationError as exc:
        raise SourceError(f"API gov.uz сменил формат статьи: {url}") from exc
    return html_to_text(article.body or "")


def _json(http: HttpClient, url: str, headers: dict[str, str]) -> dict:
    fetched = http.fetch(url, headers=headers)
    try:
        payload = json.loads(fetched.content)
    except ValueError as exc:
        raise SourceError(f"API gov.uz вернул не JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise SourceError(f"API gov.uz вернул не объект: {url}")
    return payload


def _view_url(list_url: str, news_id: int) -> str:
    parts = urlsplit(list_url)
    path = parts.path.rsplit("/", 1)[0] + "/view"
    return urlunsplit((parts.scheme, parts.netloc, path, f"id={news_id}", ""))


def _parse_date(value: str) -> datetime:
    try:
        return tashkent_to_utc(datetime.strptime(value, API_DATE_FORMAT))
    except ValueError as exc:
        raise SourceError(f"API gov.uz: неожиданный формат даты {value!r}") from exc
