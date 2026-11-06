from datetime import UTC, datetime, timedelta

from conftest import FakeHttp

from uzdigest.config import EnrichSettings, Source, load_sources
from uzdigest.db import repo
from uzdigest.enrich import enrich
from uzdigest.extract.normalize import to_item
from uzdigest.models import RawItem
from uzdigest.timeutil import utc_now

SOURCES = {s.code: s for s in load_sources()}
SETTINGS = EnrichSettings(max_pages_per_run=50, max_attempts=3, min_text_chars=100)

ARTICLE_URL = "https://buxgalter.uz/ru/publish/doc/text217829_perepis_biznesa"
BROKEN_URL = "https://www.uzdaily.uz/ru/broken/"


def setup(raws):
    """raws: (код источника, RawItem). Возвращает соединение и id материалов по адресу."""
    conn = repo.connect(":memory:")
    ids = repo.sync_sources(conn, list(SOURCES.values()))
    run_id = repo.start_run(conn, "collect")
    fetched_at = utc_now()
    with conn:
        for code, raw in raws:
            repo.insert_item(conn, ids[code], run_id, to_item(raw, "ru", fetched_at))
    return conn


def row(conn, url):
    return conn.execute("SELECT * FROM items WHERE url = ?", (url,)).fetchone()


def test_link_from_list_gets_title_text_and_page_date():
    discovered = utc_now()
    raw = RawItem(ARTICLE_URL, "", None, None, None, None, "day")
    conn = setup([("buxgalter", raw)])
    http = FakeHttp({ARTICLE_URL: "buxgalter_article.html"})

    stats = enrich(conn, http, list(SOURCES.values()), SETTINGS)
    assert (stats.fetched, stats.failed) == (1, 0)

    item = row(conn, ARTICLE_URL)
    assert item["text_status"] == "fetched" and item["text_attempts"] == 1
    assert item["title"] == "Перепись бизнеса – 2026: кого и как будут регистрировать"
    assert item["title_norm"] == "перепись бизнеса 2026 кого и как будут регистрировать"
    assert item["body"].startswith("В Узбекистане с 15 октября") and "Закрываем" not in item["body"]
    # Дата со страницы (16.10.2026) раньше момента обнаружения
    assert item["published_at"] == "2026-10-16T18:59:59+00:00" < discovered.isoformat()


def test_failures_retry_in_later_runs_up_to_limit():
    raw = RawItem(BROKEN_URL, "Заголовок из RSS", None, "Анонс", None, utc_now() - timedelta(hours=1))
    conn = setup([("uzdaily", raw)])
    http = FakeHttp({})  # страница не отвечает

    for _ in range(4):
        enrich(conn, http, list(SOURCES.values()), SETTINGS)
    item = row(conn, BROKEN_URL)
    assert item["text_status"] == "failed" and "ConnectError" in item["text_error"]
    assert item["text_attempts"] == 3
    assert http.urls().count(BROKEN_URL) == 3
    assert item["title"] == "Заголовок из RSS"


def test_too_short_text_is_a_failure():
    raw = RawItem(BROKEN_URL, "Т", None, None, None, utc_now())
    conn = setup([("uzdaily", raw)])
    page = b"<html><body><article><p>" + "Коротко.".encode() + b"</p></article></body></html>"
    stats = enrich(conn, FakeHttp({BROKEN_URL: page}), list(SOURCES.values()), SETTINGS)
    assert stats.failed == 1
    assert row(conn, BROKEN_URL)["text_status"] == "failed"


def test_feed_body_and_old_items_are_left_alone_and_fulltext_false_is_skipped():
    old = utc_now() - timedelta(hours=120)
    raws = [
        ("daryo", RawItem("https://daryo.uz/ru/1", "С текстом", None, None, "Полный текст из RSS", None)),
        ("uzdaily", RawItem("https://www.uzdaily.uz/ru/old/", "Старый", None, None, None, old)),
        ("soliq", RawItem("https://gov.uz/ru/soliq/news/view/1", "Без текста в API", None, None, None, None)),
    ]
    conn = setup(raws)
    http = FakeHttp({})
    stats = enrich(conn, http, list(SOURCES.values()), SETTINGS)
    assert (stats.fetched, stats.failed, stats.skipped) == (0, 0, 1)
    assert http.calls == []
    assert row(conn, "https://daryo.uz/ru/1")["text_status"] == "feed"
    assert row(conn, "https://www.uzdaily.uz/ru/old/")["text_status"] is None
    assert row(conn, "https://gov.uz/ru/soliq/news/view/1")["text_status"] == "skipped"


def test_unknown_source_is_skipped():
    gone = Source(code="gone", name="gone", kind="rss", url="https://gone.uz/rss", tier="C")
    conn = repo.connect(":memory:")
    ids = repo.sync_sources(conn, [gone])
    run_id = repo.start_run(conn, "collect")
    raw = RawItem("https://gone.uz/1", "Т", None, None, None, datetime.now(UTC))
    with conn:
        repo.insert_item(conn, ids["gone"], run_id, to_item(raw, "ru", utc_now()))
    stats = enrich(conn, FakeHttp({}), [], SETTINGS)
    assert stats.skipped == 1
