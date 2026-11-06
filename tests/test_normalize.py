from datetime import UTC, datetime, timedelta

from uzdigest.extract.normalize import canonical_url, html_to_text, normalize_title, to_item
from uzdigest.models import RawItem


def test_canonical_url_drops_tracking_www_fragment_and_trailing_slash():
    url = "http://www.Gazeta.uz/ru/2026/11/02/x/?utm_source=tg&b=2&a=1#top"
    assert canonical_url(url) == "https://gazeta.uz/ru/2026/11/02/x?a=1&b=2"


def test_canonical_url_keeps_meaningful_query():
    assert canonical_url("https://kun.uz/news/rss?lang=ru") == "https://kun.uz/news/rss?lang=ru"


def test_canonical_url_drops_site_navigation_param():
    url = "https://buxgalter.uz/publish/doc/text214519_chek-list?from=main_menu"
    assert canonical_url(url) == "https://buxgalter.uz/publish/doc/text214519_chek-list"


def test_html_to_text_keeps_paragraphs_and_drops_captions():
    html = (
        "<p>Один&nbsp;два</p><p>три <b>четыре</b></p>"
        "<figure><img src='x.jpg'/><figcaption>Фото: пресс-служба</figcaption></figure><script>x()</script>"
    )
    assert html_to_text(html) == "Один два\nтри четыре"


def test_html_to_text_decodes_entities_left_after_double_escaping():
    assert html_to_text("январь&ndash;сентябрь") == "январь–сентябрь"


def test_normalize_title():
    assert normalize_title("«O‘zbekiston» — ЁЛКА, 2026!") == "ozbekiston елка 2026"


def test_to_item_replaces_future_or_missing_date_with_fetch_time():
    fetched_at = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
    future = RawItem("https://a.uz/x", "Т", None, None, None, fetched_at + timedelta(hours=8))
    missing = RawItem("https://a.uz/y", "Т", None, None, None, None)
    past = RawItem("https://a.uz/z", "Т", None, None, None, fetched_at - timedelta(hours=3))

    assert to_item(future, "ru", fetched_at).published_at == fetched_at
    assert to_item(missing, "ru", fetched_at).published_at == fetched_at
    assert to_item(past, "ru", fetched_at).published_at == fetched_at - timedelta(hours=3)
