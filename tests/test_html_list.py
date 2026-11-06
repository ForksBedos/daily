from datetime import UTC, datetime

from conftest import FakeHttp, fixture_bytes

from uzdigest.config import HtmlListParams, Source, load_sources
from uzdigest.sources import html_list
from uzdigest.sources.base import FetchContext

SOURCES = {s.code: s for s in load_sources()}
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def parse(code, fixture, base_url, params=None):
    params = params or HtmlListParams.model_validate(SOURCES[code].params)
    return html_list.parse(fixture_bytes(fixture).decode("utf-8"), base_url, params)


def test_president_list():
    items = parse("president", "president_list.html", "https://president.uz/ru/lists/news")
    assert len(items) == 15
    assert all(i.url.startswith("https://president.uz/ru/lists/view/") for i in items)
    assert items[0].title == "Рабочий визит в Турцию завершился"
    # «30-10-2026» → конец ташкентских суток в UTC
    assert items[0].published_at == datetime(2026, 10, 30, 18, 59, 59, tzinfo=UTC)
    assert all(i.published_precision == "day" and i.published_at for i in items)


def test_buxgalter_list_takes_only_articles():
    items = parse("buxgalter", "buxgalter_list.html", "https://buxgalter.uz/newssite/")
    assert len(items) == 20
    assert all("/ru/publish/doc/text" in i.url for i in items)
    assert all(i.title for i in items)
    last = items[-1]
    assert last.title == "Перепись бизнеса – 2026: кого и как будут регистрировать"
    assert last.published_at == datetime(2026, 10, 16, 18, 59, 59, tzinfo=UTC)


def test_podrobno_category_has_links_without_dates():
    params = SOURCES["podrobno"].backfill.html
    items = parse("podrobno", "podrobno_list.html", "https://podrobno.uz/cat/economic/", params)
    assert len(items) == 19
    assert all(i.url.startswith("https://podrobno.uz/cat/") and i.title for i in items)
    assert all(i.published_at is None for i in items)


def test_fetch_drops_items_older_than_window():
    source = SOURCES["buxgalter"]
    http = FakeHttp({source.url: "buxgalter_list.html"})
    since = datetime(2026, 10, 29, tzinfo=UTC)
    outcome = html_list.fetch(http, source, FetchContext(NOW, since, since))
    assert outcome.items and all(i.published_at >= since for i in outcome.items)
    assert len(outcome.items) < 20


def test_paging_stops_when_page_repeats():
    params = HtmlListParams(
        list_urls=["https://podrobno.uz/cat/economic/"],
        link_pattern=SOURCES["podrobno"].backfill.html.link_pattern,
        pages="?PAGEN_1={n}",
        max_pages=5,
    )
    source = Source(code="p", name="p", kind="html_list", url="https://podrobno.uz/", tier="C", params={
        "link_pattern": params.link_pattern,
    })  # fmt: skip
    http = FakeHttp({
        "https://podrobno.uz/cat/economic/": "podrobno_list.html",
        "https://podrobno.uz/cat/economic/?PAGEN_1=2": "podrobno_list.html",  # сайт вернул ту же страницу
    })  # fmt: skip
    items = html_list.collect_links(http, source, params, FetchContext(NOW, NOW, NOW))
    assert len(items) == 19
    assert len(http.calls) == 2
