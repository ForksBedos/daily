"""Новости не должны пропадать молча: поздний сбор, сбой Claude, пустой день, сбой источника и догрузки.

Каждый тест воспроизводит ошибку того вида, что нашлись в контент-заводе (docs/ISSUES.md, «Исправленные»).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeHttp, fixture_bytes
from test_collect import ROUTES, by_code
from test_collect import SOURCES as COLLECT_SOURCES
from test_enrich import (
    ARTICLE_URL,
    BROKEN_URL,
    row,
    setup,
)
from test_enrich import (
    SETTINGS as ENRICH_SETTINGS,
)
from test_enrich import (
    SOURCES as ALL_SOURCES,
)
from test_govuz import LIST_URL
from test_govuz import source as govuz_source
from test_llm import LLM
from test_pipeline import (
    DAY,
    IMARAT,
    NOW,
    SOURCES,
    VAT,
    VAT_TEXT,
    _build,
    _claude,
    _db,
    _digest_run,
    _first_day,
    _polled,
    _settings,
)
from test_service import _ready_db, _run

import uzdigest.enrich as enrich_module
from uzdigest import collect as collect_module
from uzdigest.collect import ADAPTERS, collect
from uzdigest.config import BackfillSettings, Source, load_sources
from uzdigest.db import repo
from uzdigest.enrich import enrich
from uzdigest.extract.normalize import to_item
from uzdigest.llm.client import LlmBudget
from uzdigest.models import RawItem
from uzdigest.pipeline import build_digest, digest_window
from uzdigest.sources import govuz_api, html_list, lex_rss
from uzdigest.sources.base import FetchContext, SourceError
from uzdigest.timeutil import to_iso, utc_now


def _collected(conn, code, url, title, text, published_at, fetched_at):
    """Материал, собранный не в момент публикации: источник отставал или сборка его опередила."""
    ids = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM sources")}
    run_id = repo.start_run(conn, "collect")
    with conn:
        raw = RawItem(url, title, None, text, None, published_at)
        repo.insert_item(conn, ids[code], run_id, to_item(raw, "ru", fetched_at))


def _headlines(result):
    return [a.texts["ru"].headline for a in result.digest.articles]


# --- Окно дайджеста: что вышло до конца окна, а собрано после ---


def test_news_collected_right_after_the_digest_gets_into_the_next_one(tmp_path):
    """Как три материала 04.11.2026: вышли в 09:28–09:30, собраны в 10:00, а окно следующего дня с 09:30."""
    settings, conn = _settings(tmp_path), _db()
    _first_day(conn)
    _build(conn, settings, NOW)
    _collected(
        conn, "kun", "https://kun.uz/late", "Ужесточили наказание за налоговые схемы", "Текст новости.",
        NOW - timedelta(minutes=2), NOW + timedelta(minutes=30),
    )  # fmt: skip

    _polled(conn, NOW + DAY)
    result, _ = _build(conn, settings, NOW + DAY)

    assert "Кратко: Ужесточили наказание за налоговые схемы" in _headlines(result)


def test_news_of_a_source_that_was_down_for_two_days_is_not_lost(tmp_path):
    settings, conn = _settings(tmp_path), _db()
    _first_day(conn)
    _build(conn, settings, NOW)
    _collected(
        conn, "daryo", "https://daryo.uz/late", "Источник вернулся с новостью о НДС", "Текст новости.",
        NOW - 2 * DAY, NOW + timedelta(hours=20),
    )  # fmt: skip

    _polled(conn, NOW + DAY)
    result, _ = _build(conn, settings, NOW + DAY)

    assert "Кратко: Источник вернулся с новостью о НДС" in _headlines(result)


def test_archive_of_a_new_source_does_not_flood_the_digest(tmp_path):
    settings, conn = _settings(tmp_path), _db()
    _first_day(conn)
    _build(conn, settings, NOW)
    _collected(
        conn, "daryo", "https://daryo.uz/old", "Старая новость из архива ленты", "Текст новости.",
        NOW - 30 * DAY, NOW + timedelta(hours=20),
    )  # fmt: skip

    _polled(conn, NOW + DAY)
    result, _ = _build(conn, settings, NOW + DAY)

    assert "Кратко: Старая новость из архива ленты" not in _headlines(result)


def test_law_in_a_late_news_item_is_checked_against_lex(tmp_path):
    settings, conn = _settings(tmp_path), _db()
    _build(conn, settings, NOW)
    _collected(
        conn, "soliq", "https://gov.uz/ru/soliq/news/view/9", VAT, VAT_TEXT,
        NOW - timedelta(minutes=2), NOW + timedelta(minutes=30),
    )  # fmt: skip

    _polled(conn, NOW + DAY)
    result, _ = _build(conn, settings, NOW + DAY)

    assert [a.doc.status for a in result.digest.articles if a.doc] == ["verified"]


def test_news_that_never_saw_a_build_is_not_reported_while_the_next_build_will_take_it(tmp_path):
    settings, conn = _settings(tmp_path), _db()
    _first_day(conn)
    _build(conn, settings, NOW)
    _collected(
        conn, "kun", "https://kun.uz/late", "Опоздавшая новость", "Текст.",
        NOW - timedelta(minutes=2), NOW + timedelta(minutes=30),
    )  # fmt: skip

    assert repo.unseen_items(conn, NOW - DAY, NOW) == []


# --- Окно дайджеста: день, когда Claude не работал ---


def _down(claude=None):
    return LlmBudget(LLM.model_copy(update={"max_calls_per_run": 0}), ask_fn=claude or _claude())


def test_news_of_the_day_claude_was_down_reach_the_next_digest(tmp_path):
    settings, conn = _settings(tmp_path), _db()
    _digest_run(conn, NOW - 2 * DAY, NOW - DAY)
    _first_day(conn)
    outage = build_digest(conn, settings, FakeHttp({}), SOURCES, NOW, budget=_down())
    assert outage.status == "partial"
    assert f"Кратко: {IMARAT}" not in _headlines(outage)  # СМИ без Claude в дайджест не идут

    _polled(conn, NOW + DAY)
    result, _ = _build(conn, settings, NOW + DAY)

    assert result.status == "ok"
    assert f"Кратко: {IMARAT}" in _headlines(result)
    assert not any(VAT in headline for headline in _headlines(result))  # он уже вышел вчера без Claude


def test_digest_that_is_partial_for_another_reason_still_closes_its_window(tmp_path):
    settings, conn = _settings(tmp_path), _db()
    _first_day(conn)

    claude = _claude()
    claude.translation_answer = lambda payload, lang: {**payload, "cluster_id": 0}  # перевод потерялся
    result, _ = _build(conn, settings, NOW, claude)

    assert result.status == "partial" and result.digest.failures.no_translation
    assert digest_window(conn, settings.digest, NOW + DAY).since == NOW


def test_empty_digest_is_not_a_success_and_keeps_the_window_open(tmp_path):
    settings, conn = _settings(tmp_path), _db()
    _digest_run(conn, NOW - 2 * DAY, NOW - DAY)

    result, _ = _build(conn, settings, NOW)

    assert (result.status, len(result.digest.articles)) == ("partial", 0)
    assert digest_window(conn, settings.digest, NOW + DAY).since == NOW - DAY


# --- Материалы и источники не помечаются обработанными раньше времени ---


def test_collect_of_one_source_leaves_the_texts_of_the_others_alone():
    list_link = RawItem(ARTICLE_URL, "", None, None, None, None, "day")
    other = RawItem(BROKEN_URL, "Заголовок из RSS", None, "Анонс", None, utc_now() - timedelta(hours=1))
    conn = setup([("buxgalter", list_link), ("uzdaily", other)])

    http = FakeHttp({ARTICLE_URL: "buxgalter_article.html"})
    stats = enrich(conn, http, list(ALL_SOURCES.values()), ENRICH_SETTINGS, only={"buxgalter"})

    assert (stats.fetched, stats.skipped) == (1, 0)
    assert row(conn, BROKEN_URL)["text_status"] is None  # следующий полный сбор скачает его текст


def test_collect_command_for_one_source_keeps_the_rest_waiting_for_their_text(tmp_path):
    settings = _settings(tmp_path)
    conn = _ready_db(settings)

    _run(settings, "collect", only={"kun"})

    statuses = dict(
        conn.execute(
            "SELECT s.code, i.text_status FROM items i JOIN sources s ON s.id = i.source_id"
        ).fetchall()
    )
    assert statuses["daryo"] is None  # раньше такой материал получал skipped навсегда


class EtagHttp(FakeHttp):
    def fetch(self, url, **kwargs):
        result = super().fetch(url, **kwargs)
        return replace(result, etag='"v2"') if "rss" in url else result


def _gap_source() -> Source:
    return next(s for s in load_sources() if s.code == "kun")


def test_failed_backfill_keeps_the_gap_for_the_next_poll():
    """Лента получена, а sitemap не ответил: прежний код сдвигал «последний успех», и пробел терялся."""
    source = _gap_source()
    conn = repo.connect(":memory:")
    ids = repo.sync_sources(conn, [source])
    gap_start = "2026-10-01T00:00:00+00:00"
    with conn:
        repo.mark_source_ok(conn, ids["kun"], '"v1"', None)
        conn.execute("UPDATE sources SET last_ok_at = ?", (gap_start,))
    http = EtagHttp({source.url: "kun.xml"})  # адресов sitemap нет: ConnectError

    [result] = collect(
        conn, http, [source], lookback_h=24 * 3650, backfill_settings=BackfillSettings(), max_workers=1
    )

    state = repo.source_state(conn, ids["kun"])
    assert result.status == "ok" and result.new > 0 and result.warnings == 1
    assert state["last_ok_at"] == gap_start  # следующий опрос снова увидит пробел
    assert state["etag"] == '"v1"'  # и не получит от сервера «не изменилось»


def test_successful_backfill_closes_the_gap():
    source = _gap_source()
    conn = repo.connect(":memory:")
    ids = repo.sync_sources(conn, [source])
    now = utc_now()
    with conn:
        conn.execute("UPDATE sources SET last_ok_at = ?", (to_iso(now),))

    collect(
        conn, FakeHttp({source.url: "kun.xml"}), [source], lookback_h=24, backfill_settings=BackfillSettings()
    )

    assert repo.source_state(conn, ids["kun"])["last_ok_at"] >= to_iso(now)


def test_short_feed_without_backfill_warns_that_news_may_be_gone():
    """Лента gazeta короче суток: после пропуска часть новостей уже не вернуть, но это теперь видно."""
    source = Source(code="kun", name="Лента без догрузки", kind="rss", url="https://kun.uz/rss", tier="C")
    conn = repo.connect(":memory:")
    repo.sync_sources(conn, [source])
    with conn:
        conn.execute("UPDATE sources SET last_ok_at = '2026-10-01T00:00:00+00:00'")

    [result] = collect(
        conn, FakeHttp({source.url: "kun.xml"}), [source], lookback_h=24 * 3650, max_workers=1,
        backfill_settings=BackfillSettings(),
    )  # fmt: skip

    assert result.status == "ok" and result.gap_h > 0


# --- Сбой источника отличается от пустого дня ---

NOW_FETCH = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 29, tzinfo=UTC)


def test_list_page_without_article_links_is_a_failure_not_a_quiet_day():
    """Сайт сменил вёрстку или отдал заглушку: прежний код писал «ok, 0 материалов»."""
    source = ALL_SOURCES["buxgalter"]
    http = FakeHttp({source.url: "<html><body>Сайт на обслуживании</body></html>".encode()})

    with pytest.raises(SourceError):
        html_list.fetch(http, source, FetchContext(NOW_FETCH, SINCE, SINCE))


def test_list_page_with_only_old_links_is_a_quiet_day():
    source = ALL_SOURCES["buxgalter"]
    http = FakeHttp({source.url: "buxgalter_list.html"})
    later = NOW_FETCH + 30 * DAY

    assert html_list.fetch(http, source, FetchContext(later, later, later)).items == []


def test_gov_api_with_an_empty_list_is_a_failure_not_a_quiet_day():
    http = FakeHttp({f"{LIST_URL}&page=1": b'{"data": [], "total_page": 0}'})

    with pytest.raises(SourceError):
        govuz_api.fetch(http, govuz_source("soliq", "ru"), FetchContext(NOW_FETCH, SINCE, SINCE))


def test_broken_page_is_recorded_as_a_source_failure():
    source = ALL_SOURCES["president"]
    conn = repo.connect(":memory:")
    ids = repo.sync_sources(conn, [source])

    [result] = collect(conn, FakeHttp({source.url: b"<html></html>"}), [source], max_workers=1)

    state = conn.execute(
        "SELECT last_ok_at, last_error FROM sources WHERE id = ?", (ids["president"],)
    ).fetchone()
    assert result.status == "error"
    assert state["last_ok_at"] is None and "ссылок" in state["last_error"]


def test_bug_in_one_source_does_not_stop_the_others(monkeypatch):
    """Ошибка не из ожидаемого списка роняла весь сбор, а за ним и утренний дайджест."""

    def broken(http, source, ctx):
        raise KeyError("в ответе нет поля")

    monkeypatch.setitem(ADAPTERS, "html_list", broken)
    conn = repo.connect(":memory:")

    results = by_code(collect(conn, FakeHttp(ROUTES), COLLECT_SOURCES, max_workers=2, lookback_h=24 * 3650))

    assert results["kun"].status == "ok" and results["kun"].new > 0
    assert results["president"].status == "error" and "KeyError" in results["president"].error
    assert repo.last_run(conn, "collect")["status"] == "partial"


def test_broken_date_of_one_act_keeps_the_rest_of_the_feed():
    source = next(s for s in load_sources() if s.code == "lex")
    feed = fixture_bytes("lex.xml").replace(b"28.10.2026", b"31.02.2026", 1)
    http = FakeHttp({source.url: feed})

    outcome = lex_rss.fetch(http, source, FetchContext(NOW_FETCH, SINCE, SINCE))

    assert len(outcome.acts) == 88 and len(outcome.warnings) == 1


def test_crash_of_a_collect_stays_in_the_journal(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("диск заполнен")

    monkeypatch.setattr(collect_module.repo, "insert_item", broken)
    conn = repo.connect(":memory:")

    with pytest.raises(RuntimeError):
        collect(conn, FakeHttp(ROUTES), COLLECT_SOURCES, max_workers=2, lookback_h=24 * 3650)

    run = conn.execute("SELECT status, finished_at, stats_json FROM runs").fetchone()
    assert run["status"] == "failed" and run["finished_at"]
    assert "диск заполнен" in run["stats_json"]


def test_bug_on_one_page_does_not_stop_the_collect(monkeypatch):
    def broken(http, url):
        raise ValueError("страница сломала разбор")

    monkeypatch.setattr(enrich_module, "fetch_article", broken)
    raw = RawItem(BROKEN_URL, "Заголовок из RSS", None, "Анонс", None, utc_now() - timedelta(hours=1))
    conn = setup([("uzdaily", raw)])

    stats = enrich(conn, FakeHttp({}), list(ALL_SOURCES.values()), ENRICH_SETTINGS)

    assert (stats.failed, stats.failed_by_source) == (1, {"uzdaily": 1})
    assert "страница сломала разбор" in row(conn, BROKEN_URL)["text_error"]


def test_database_waits_for_another_writer_longer_than_a_slow_lex_answer(tmp_path):
    """Бот пишет пост, пока идёт сборка: при 5 секундах готовый пост терялся с «database is locked»."""
    conn = repo.connect(tmp_path / "db.sqlite3")

    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000
