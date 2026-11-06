from datetime import UTC, datetime, timedelta

import httpx
from conftest import FakeHttp

from uzdigest.config import BackfillSettings, load_sources
from uzdigest.models import RawItem
from uzdigest.sources import backfill
from uzdigest.sources.base import FetchContext

SOURCES = {s.code: s for s in load_sources()}
SETTINGS = BackfillSettings(max_urls=150, max_pages=5)


def item(published_at):
    return RawItem("https://a.uz/x", "Т", None, None, None, published_at)


def ctx(now, gap_start, known=frozenset()):
    return FetchContext(now=now, since=now - timedelta(hours=96), gap_start=gap_start, known_urls=known)


def test_rule_backfills_only_when_feed_starts_after_gap():
    gap_start = datetime(2026, 10, 30, 15, tzinfo=UTC)
    covered = [item(gap_start + timedelta(hours=5)), item(gap_start - timedelta(hours=1))]
    short = [item(gap_start + timedelta(hours=5)), item(gap_start + timedelta(hours=1))]
    assert not backfill.needs_backfill(covered, gap_start)
    assert backfill.needs_backfill(short, gap_start)
    assert backfill.needs_backfill([], gap_start)


def test_kun_daily_sitemaps_for_every_day_of_gap():
    now = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # пн 09:30 Ташкент
    gap_start = datetime(2026, 10, 31, 4, 30, tzinfo=UTC)  # сб
    url = "https://kun.uz/sitemap/site-map-news_day_{}_year_2026_month_{}.xml"
    http = FakeHttp({
        url.format(31, 10): "kun_sitemap_day.xml",
        url.format(1, 11): b'<?xml version="1.0"?><urlset></urlset>',
        url.format(2, 11): b'<?xml version="1.0"?><urlset></urlset>',
    })  # fmt: skip
    items = backfill.fetch(http, SOURCES["kun"], ctx(now, gap_start), SETTINGS)
    assert http.urls() == [url.format(31, 10), url.format(1, 11), url.format(2, 11)]
    assert len(items) == 16  # из 102 адресов на всех языках русских 16
    assert all(i.url.startswith("https://kun.uz/ru/news/2026/10/31/") for i in items)
    assert all(i.title == "" and i.published_precision == "day" for i in items)
    assert all(i.published_at == datetime(2026, 10, 31, 18, 59, 59, tzinfo=UTC) for i in items)


def test_missing_sitemap_for_today_is_not_an_error():
    class Http404(FakeHttp):
        def fetch(self, url, **kwargs):
            if url.endswith("day_2_year_2026_month_11.xml"):
                request = httpx.Request("GET", url)
                raise httpx.HTTPStatusError(
                    "404", request=request, response=httpx.Response(404, request=request)
                )
            return super().fetch(url, **kwargs)

    url = "https://kun.uz/sitemap/site-map-news_day_{}_year_2026_month_{}.xml"
    http = Http404({url.format(1, 11): "kun_sitemap_day.xml"})
    now = datetime(2026, 11, 2, 3, tzinfo=UTC)
    items = backfill.fetch(http, SOURCES["kun"], ctx(now, datetime(2026, 11, 1, 3, tzinfo=UTC)), SETTINGS)
    assert len(items) == 16


def test_uza_monthly_sitemap_uses_lastmod_as_publication_time():
    now = datetime(2026, 11, 2, 15, tzinfo=UTC)
    gap_start = datetime(2026, 11, 2, 10, tzinfo=UTC)
    http = FakeHttp({"https://uza.uz/sitemap/posts-ru-2026-11.xml": "uza_sitemap_month.xml"})
    items = backfill.fetch(http, SOURCES["uza"], ctx(now, gap_start), SETTINGS)
    assert items and all(i.published_precision == "time" and i.published_at >= gap_start for i in items)
    assert items[0].published_at == datetime(2026, 11, 2, 14, 19, 43, tzinfo=UTC)


def test_months_across_year_boundary():
    http = FakeHttp({
        "https://uza.uz/sitemap/posts-ru-2026-12.xml": b"<urlset></urlset>",
        "https://uza.uz/sitemap/posts-ru-2027-01.xml": b"<urlset></urlset>",
    })  # fmt: skip
    now = datetime(2027, 1, 2, tzinfo=UTC)
    backfill.fetch(http, SOURCES["uza"], ctx(now, datetime(2026, 12, 30, tzinfo=UTC)), SETTINGS)
    assert len(http.calls) == 2


def test_uzdaily_sitemap_filtered_by_day_known_urls_and_limit():
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    gap_start = datetime(2026, 11, 1, 3, tzinfo=UTC)  # 1 ноября по Ташкенту
    http = FakeHttp({"https://www.uzdaily.uz/sitemap-ru-articles-1.xml": "uzdaily_sitemap.xml"})
    items = backfill.fetch(http, SOURCES["uzdaily"], ctx(now, gap_start), SETTINGS)
    assert len(items) == 33 + 9  # в фикстуре 33 адреса за 2 ноября и 9 за 1 ноября

    known = frozenset({"https://uzdaily.uz/ru/chislo-trudovykh-migrantov-iz-uzbekistana-sokratilos-vdvoe"})
    limited = backfill.fetch(
        http, SOURCES["uzdaily"], ctx(now, gap_start, known), BackfillSettings(max_urls=10)
    )
    assert len(limited) == 10
    assert all("chislo-trudovykh-migrantov" not in i.url for i in limited)


def test_nuz_feed_pages_until_gap_is_covered():
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    gap_start = datetime(2026, 10, 30, 12, tzinfo=UTC)  # вторая страница начинается раньше
    http = FakeHttp({"https://nuz.uz/feed/?paged=2": "nuz_feed_page2.xml"})
    items = backfill.fetch(http, SOURCES["nuz"], ctx(now, gap_start), SETTINGS)
    assert http.urls() == ["https://nuz.uz/feed/?paged=2"]
    assert len(items) == 20 and all(i.title for i in items)


def test_podrobno_list_pages_and_exclude():
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    empty = b"<html><body>nothing</body></html>"
    http = FakeHttp({
        "https://podrobno.uz/cat/economic/": "podrobno_list.html",
        "https://podrobno.uz/cat/economic/?PAGEN_1=2": empty,
        "https://podrobno.uz/cat/uzbekistan/": empty,
    })  # fmt: skip
    in_feed = frozenset({
        "https://podrobno.uz/cat/economic/v-uzbekistane-vydali-tysyachi-kreditov-pochti-na-36-milliardov-"
        "sumov-dlya-vykhoda-iz-bednosti",
    })  # fmt: skip
    items = backfill.fetch(http, SOURCES["podrobno"], ctx(now, now), SETTINGS, exclude=in_feed)
    assert len(items) == 18
