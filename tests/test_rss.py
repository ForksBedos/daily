import email.utils
import re

import pytest
from conftest import fixture_bytes

from uzdigest.sources.rss import FeedError, parse


def test_kun_feed():
    items = parse(fixture_bytes("kun.xml"), "https://kun.uz/news/rss?lang=ru")
    assert len(items) >= 10
    for item in items:
        assert item.url.startswith("https://kun.uz/ru/news/")
        assert item.title
        assert item.published_at is not None and item.published_at.tzinfo is not None


def test_daryo_full_text_feed_has_body_separate_from_lead():
    items = parse(fixture_bytes("daryo.xml"), "https://daryo.uz/ru/rss-full/")
    with_body = [i for i in items if i.body]
    assert len(with_body) >= len(items) // 2
    assert all(i.lead != i.body for i in with_body)
    assert all("<" not in i.body for i in with_body)


def test_lex_relative_links_become_absolute():
    items = parse(fixture_bytes("lex.xml"), "https://lex.uz/ru/rss")
    assert items
    assert all(re.fullmatch(r"https://lex\.uz/ru/docs/\d+", i.url) for i in items)


def test_norma_dates_converted_to_utc():
    content = fixture_bytes("norma.xml")
    raw_dates = re.findall(rb"<pubDate>(.*?)</pubDate>", content)
    items = parse(content, "https://www.norma.uz/ru/scripts/rss/x")
    assert len(items) == len(raw_dates)
    for item, raw in zip(items, raw_dates, strict=True):
        assert item.published_at == email.utils.parsedate_to_datetime(raw.decode())


def test_feed_without_items_is_an_error():
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    with pytest.raises(FeedError):
        parse(empty, "https://example.uz/rss")


def test_html_page_instead_of_feed_is_an_error():
    with pytest.raises(FeedError):
        parse(b"<html><body>403 Forbidden</body></html>", "https://example.uz/rss")
