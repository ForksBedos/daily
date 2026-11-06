"""HTTP-клиент сборщиков.

Честный User-Agent, проверка robots.txt (RFC 9309), пауза между запросами к одному домену
(не меньше Crawl-delay), повторы с нарастающей паузой и условные GET (ETag / Last-Modified).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

from uzdigest.config import HttpSettings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class RobotsDisallowed(Exception):
    """robots.txt сайта запрещает этот адрес."""


@dataclass(frozen=True)
class FetchResult:
    url: str  # итоговый адрес после редиректов
    status: int  # 200 или 304
    content: bytes
    etag: str | None
    last_modified: str | None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class HttpClient:
    def __init__(
        self,
        settings: HttpSettings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._sleep = sleep
        self._lock = threading.Lock()
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._next_slot: dict[str, float] = {}

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        check_robots: bool = True,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        if check_robots and not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt запрещает {url}")
        headers = dict(headers or {})
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        response = self._get(url, headers)
        if response.status_code == 304:
            return FetchResult(str(response.url), 304, b"", etag, last_modified)
        response.raise_for_status()
        return FetchResult(
            url=str(response.url),
            status=response.status_code,
            content=response.content,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self._settings.user_agent, url)

    def _robots_for(self, url: str) -> robotparser.RobotFileParser:
        origin = _origin(url)
        with self._lock:
            cached = self._robots.get(origin)
        if cached is not None:
            return cached
        parser = robotparser.RobotFileParser()
        try:
            response = self._get(f"{origin}/robots.txt", {})
        except httpx.TransportError as exc:
            # RFC 9309: robots.txt недоступен — считаем, что запрещено всё
            log.warning("robots.txt недоступен для %s: %s", origin, exc)
            parser.disallow_all = True
        else:
            if response.status_code >= 500:
                parser.disallow_all = True
            elif response.status_code >= 400:
                parser.allow_all = True
            else:
                parser.parse(response.text.splitlines())
        with self._lock:
            self._robots[origin] = parser
        return parser

    def _get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        retries = self._settings.retries
        for attempt in range(1, retries + 1):
            self._wait_turn(url)
            try:
                response = self._client.get(url, headers=headers)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise
                log.info("Повтор %s (%s): %s", url, attempt, exc)
                self._sleep(2.0**attempt)
                continue
            if response.status_code in RETRY_STATUSES and attempt < retries:
                log.info("Повтор %s (%s): HTTP %s", url, attempt, response.status_code)
                self._sleep(_retry_after(response) or 2.0**attempt)
                continue
            return response
        raise AssertionError("unreachable")

    def _wait_turn(self, url: str) -> None:
        """Резервирует ближайшее свободное окно для запроса к домену и ждёт его."""
        origin = _origin(url)
        with self._lock:
            robots = self._robots.get(origin)
            crawl_delay = robots.crawl_delay(self._settings.user_agent) if robots else None
            delay = max(self._settings.min_delay_s, float(crawl_delay or 0))
            now = time.monotonic()
            slot = max(now, self._next_slot.get(origin, now))
            self._next_slot[origin] = slot + delay
        if slot > now:
            self._sleep(slot - now)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    return min(float(value), 60.0) if value.isdigit() else None
