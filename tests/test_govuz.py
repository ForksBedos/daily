import json
from datetime import UTC, datetime

import pytest
from conftest import FakeHttp, fixture_bytes

from uzdigest.config import Source
from uzdigest.sources import govuz_api
from uzdigest.sources.base import FetchContext, SourceError

LIST_URL = "https://api-portal.gov.uz/authorities/news/category?code_name=news"
VIEW_URL = "https://api-portal.gov.uz/authorities/news/view?id={}"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def source(authority, language):
    return Source(
        code=authority,
        name=authority,
        kind="govuz_api",
        url=LIST_URL,
        tier="A",
        lang=language,
        params={"authority": authority, "language": language, "max_pages": 2},
    )


def view(news_id, body):
    return json.dumps({"data": {"id": news_id, "body": body}}).encode()


def routes(fixture_name, view_fixture, view_id):
    entries = json.loads(fixture_bytes(fixture_name))["data"]
    result = {f"{LIST_URL}&page=1": fixture_name}
    result.update({VIEW_URL.format(e["id"]): view(e["id"], f"<p>Текст {e['id']}</p>") for e in entries})
    result[VIEW_URL.format(view_id)] = view_fixture
    return result, entries


def test_soliq_list_and_view():
    http_routes, entries = routes("govuz_soliq_list.json", "govuz_soliq_view.json", 240893)
    http = FakeHttp(http_routes)
    since = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    outcome = govuz_api.fetch(http, source("soliq", "ru"), FetchContext(NOW, since, since))

    first = outcome.items[0]
    assert first.url == "https://gov.uz/ru/soliq/news/view/240893"
    assert first.guid == "240893"
    assert first.title.startswith("Взыскание задолженности за счёт дебиторов")
    # "2026-11-01 16:30:00" по Ташкенту (UTC+5)
    assert first.published_at == datetime(2026, 11, 1, 11, 30, tzinfo=UTC)
    assert "статье 121" in first.body and "<" not in first.body

    # Список отсортирован по дате: всё, что старше since, отброшено, и вторая страница не нужна
    expected = [e for e in entries if e["date"] >= "2026-10-30 05:00:00"]
    assert len(outcome.items) == len(expected) < len(entries)
    assert f"{LIST_URL}&page=2" not in http.urls()
    assert all(headers == {"code": "soliq", "language": "ru"} for _, headers in http.calls)


def test_imv_in_uzbek_and_known_items_are_not_downloaded_again():
    http_routes, entries = routes("govuz_imv_list.json", "govuz_imv_view.json", 240824)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    known = frozenset({"https://gov.uz/uz/imv/news/view/241075"})
    http = FakeHttp(http_routes | {f"{LIST_URL}&page=2": b'{"data": []}'})
    outcome = govuz_api.fetch(http, source("imv", "uz"), FetchContext(NOW, since, since, known_urls=known))

    urls = [i.url for i in outcome.items]
    assert "https://gov.uz/uz/imv/news/view/241075" not in urls
    assert VIEW_URL.format(241075) not in http.urls()
    item = next(i for i in outcome.items if i.guid == "240824")
    assert item.title == "Янги ўзбек алифбоси: ислоҳотлар ва амалиёт уйғунлиги"
    assert item.lead.startswith("Мамлакатимизда")
    assert "Лотин ёзувига асосланган" in item.body
    assert len(outcome.items) == len(entries) - 1


def test_failed_view_skips_only_that_item():
    http_routes, entries = routes("govuz_soliq_list.json", "govuz_soliq_view.json", 240893)
    del http_routes[VIEW_URL.format(240893)]
    since = datetime(2026, 10, 1, tzinfo=UTC)
    http = FakeHttp(http_routes | {f"{LIST_URL}&page=2": b'{"data": []}'})
    outcome = govuz_api.fetch(http, source("soliq", "ru"), FetchContext(NOW, since, since))
    assert len(outcome.items) == len(entries) - 1
    assert len(outcome.warnings) == 1 and "240893" in outcome.warnings[0]


def test_changed_api_format_is_a_loud_error():
    http = FakeHttp({f"{LIST_URL}&page=1": b'{"items": []}'})
    with pytest.raises(SourceError):
        govuz_api.fetch(http, source("soliq", "ru"), FetchContext(NOW, NOW, NOW))
