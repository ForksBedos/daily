import httpx
import pytest

from uzdigest.web.http import HttpClient, RobotsDisallowed

ROBOTS = "User-agent: *\nDisallow: /ru/\nCrawl-delay: 10\n"


def make_client(http_settings, handler, sleeps=None):
    return HttpClient(
        http_settings,
        transport=httpx.MockTransport(handler),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_robots_disallow_blocks_unless_exempt(http_settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, content=b"<rss/>")

    with make_client(http_settings, handler) as http:
        with pytest.raises(RobotsDisallowed):
            http.fetch("https://norma.uz/ru/scripts/rss/1")
        assert http.fetch("https://norma.uz/ru/scripts/rss/1", check_robots=False).content == b"<rss/>"
        assert http.fetch("https://norma.uz/novoe_v_zakonodatelstve/x").status == 200


def test_crawl_delay_is_respected(http_settings):
    sleeps = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, content=b"ok")

    with make_client(http_settings, handler, sleeps) as http:
        http.fetch("https://norma.uz/a")
        http.fetch("https://norma.uz/b")
    assert sleeps and max(sleeps) > 9


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (503, False)])
def test_robots_status_handling(http_settings, status, allowed):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(status)
        return httpx.Response(200, content=b"ok")

    with make_client(http_settings, handler) as http:
        assert http.allowed("https://example.uz/page") is allowed


def test_conditional_get_returns_not_modified(http_settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"feed", headers={"ETag": '"v1"'})

    with make_client(http_settings, handler) as http:
        first = http.fetch("https://example.uz/rss")
        second = http.fetch("https://example.uz/rss", etag=first.etag)
    assert first.etag == '"v1"' and not first.not_modified
    assert second.not_modified and second.etag == '"v1"'


def test_retries_server_errors_then_succeeds(http_settings):
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, content=b"ok")

    with make_client(http_settings, handler) as http:
        assert http.fetch("https://example.uz/rss").content == b"ok"
    assert calls["n"] == 3


def test_gives_up_after_retries(http_settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503)

    with make_client(http_settings, handler) as http, pytest.raises(httpx.HTTPStatusError):
        http.fetch("https://example.uz/rss")
