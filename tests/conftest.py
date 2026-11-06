from pathlib import Path

import httpx
import pytest

from uzdigest.config import HttpSettings
from uzdigest.web.http import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeHttp:
    """Отдаёт сохранённые ответы по точному адресу; неизвестный адрес — сетевая ошибка.

    routes: адрес → имя файла в fixtures/ или bytes. Все запросы с заголовками пишутся в calls.
    """

    def __init__(self, routes: dict[str, str | bytes]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch(self, url, *, etag=None, last_modified=None, check_robots=True, headers=None):
        self.calls.append((url, dict(headers or {})))
        if url not in self.routes:
            raise httpx.ConnectError(f"нет фикстуры для {url}")
        body = self.routes[url]
        return FetchResult(url, 200, body if isinstance(body, bytes) else fixture_bytes(body), None, None)

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


@pytest.fixture
def http_settings() -> HttpSettings:
    return HttpSettings(
        user_agent="UzDigest/0.1 (test)", timeout_s=5, retries=3, min_delay_s=0, max_workers=2
    )
