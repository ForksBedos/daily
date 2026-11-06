import json
from datetime import UTC, datetime

from conftest import FakeHttp

from uzdigest.collect import collect, collect_rates
from uzdigest.config import BackfillSettings, RatesSettings, Source, load_sources
from uzdigest.db import repo

ROUTES = {
    "https://kun.uz/news/rss?lang=ru": "kun.xml",
    "https://www.uzdaily.uz/ru/rss/": "uzdaily.xml",
    "https://president.uz/ru/lists/news": "president_list.html",
}

SOURCES = [
    Source(code="kun", name="Kun.uz", kind="rss", url="https://kun.uz/news/rss?lang=ru", tier="C"),
    Source(code="uzdaily", name="UzDaily", kind="rss", url="https://www.uzdaily.uz/ru/rss/", tier="C"),
    Source(code="down", name="Недоступный", kind="rss", url="https://down.uz/rss", tier="C"),
    Source(
        code="president",
        name="Президент",
        kind="html_list",
        url="https://president.uz/ru/lists/news",
        tier="A",
        params={
            "link_pattern": r"^/ru/lists/view/\d+$",
            "date_pattern": r'<span class="date_text">\s*([\d-]+)',
            "date_format": "%d-%m-%Y",
        },
    ),
    Source(code="off", name="Выключенный", kind="rss", url="https://off.uz/rss", tier="C", enabled=False),
]


def by_code(results):
    return {r.code: r for r in results}


def test_collect_is_idempotent_and_isolates_failures():
    conn = repo.connect(":memory:")

    # Окно 10 лет: сохранённые фикстуры не устаревают
    first = by_code(collect(conn, FakeHttp(ROUTES), SOURCES, max_workers=2, lookback_h=24 * 3650))
    assert first["kun"].status == "ok" and first["kun"].new == first["kun"].fetched > 0
    assert first["uzdaily"].new > 0
    assert first["down"].status == "error" and "ConnectError" in first["down"].error
    assert first["president"].status == "ok" and first["president"].new == 15
    assert "off" not in first

    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert total == first["kun"].new + first["uzdaily"].new + first["president"].new

    second = by_code(collect(conn, FakeHttp(ROUTES), SOURCES, max_workers=2, lookback_h=24 * 3650))
    assert second["kun"].new == 0 and second["uzdaily"].new == 0 and second["president"].new == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == total

    run = conn.execute("SELECT status, stats_json FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "partial"
    assert {s["code"] for s in json.loads(run["stats_json"])["sources"]} == {
        "kun",
        "uzdaily",
        "down",
        "president",
    }

    down = conn.execute("SELECT last_error, last_ok_at FROM sources WHERE code = 'down'").fetchone()
    assert "ConnectError" in down["last_error"] and down["last_ok_at"] is None


def test_items_store_text_status_and_date_precision():
    conn = repo.connect(":memory:")
    collect(conn, FakeHttp(ROUTES), SOURCES, lookback_h=24 * 3650)
    rows = conn.execute(
        "SELECT s.code, i.text_status, i.published_precision"
        " FROM items i JOIN sources s ON s.id = i.source_id"
    ).fetchall()
    assert {(r["code"], r["text_status"], r["published_precision"]) for r in rows} == {
        ("kun", None, "time"),  # в ленте kun только анонсы
        ("uzdaily", None, "time"),
        ("president", None, "day"),
    }


def test_short_feed_triggers_backfill_from_sitemap():
    uzdaily = next(s for s in load_sources() if s.code == "uzdaily")
    routes = {
        "https://www.uzdaily.uz/ru/rss/": "uzdaily.xml",
        "https://www.uzdaily.uz/sitemap-ru-articles-1.xml": "uzdaily_sitemap.xml",
    }
    conn = repo.connect(":memory:")
    http = FakeHttp(routes)
    [result] = collect(
        conn, http, [uzdaily], lookback_h=24 * 3650, backfill_settings=BackfillSettings(max_urls=150)
    )
    assert result.status == "ok" and result.backfilled > 0
    assert result.new == result.fetched  # адреса из ленты и из sitemap не повторяются
    assert "https://www.uzdaily.uz/sitemap-ru-articles-1.xml" in http.urls()
    backfilled = conn.execute("SELECT COUNT(*) FROM items WHERE title = ''").fetchone()[0]
    assert backfilled == result.backfilled

    # Без догрузки (флаг --no-backfill) sitemap не запрашивается
    http = FakeHttp(routes)
    collect(repo.connect(":memory:"), http, [uzdaily], lookback_h=24 * 3650, backfill_settings=None)
    assert http.urls() == ["https://www.uzdaily.uz/ru/rss/"]


def test_gap_starts_at_last_successful_poll_unless_overridden():
    uzdaily = next(s for s in load_sources() if s.code == "uzdaily")
    feed_only = {"https://www.uzdaily.uz/ru/rss/": "uzdaily.xml"}
    conn = repo.connect(":memory:")
    collect(conn, FakeHttp(feed_only), [uzdaily], lookback_h=24 * 3650)  # первый опрос, без догрузки

    # Лента сохранена утром, последний опрос — только что: пробела нет, sitemap не нужен
    http = FakeHttp(feed_only)
    collect(conn, http, [uzdaily], lookback_h=24 * 3650, backfill_settings=BackfillSettings())
    assert http.urls() == ["https://www.uzdaily.uz/ru/rss/"]

    # --since: пробел считается с пятницы, лента его не покрывает
    http = FakeHttp(feed_only | {"https://www.uzdaily.uz/sitemap-ru-articles-1.xml": "uzdaily_sitemap.xml"})
    [result] = collect(
        conn,
        http,
        [uzdaily],
        lookback_h=24 * 3650,
        backfill_settings=BackfillSettings(),
        gap_from=datetime(2026, 10, 30, 4, 30, tzinfo=UTC),
    )
    assert "https://www.uzdaily.uz/sitemap-ru-articles-1.xml" in http.urls()
    assert result.backfilled > 0


def test_failed_backfill_keeps_feed_items():
    uzdaily = next(s for s in load_sources() if s.code == "uzdaily")
    conn = repo.connect(":memory:")
    [result] = collect(
        conn,
        FakeHttp({"https://www.uzdaily.uz/ru/rss/": "uzdaily.xml"}),
        [uzdaily],
        lookback_h=24 * 3650,
        backfill_settings=BackfillSettings(),
    )
    assert result.status == "ok" and result.new > 0 and result.warnings == 1


def test_rates_are_upserted():
    conn = repo.connect(":memory:")
    settings = RatesSettings(url="https://cbu.uz/ru/arkhiv-kursov-valyut/json/")
    http = FakeHttp({settings.url: "cbu_rates.json"})
    assert collect_rates(conn, http, settings) == "2026-10-31"
    assert collect_rates(conn, http, settings) == "2026-10-31"
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 74
    usd = conn.execute("SELECT rate, diff FROM fx_rates WHERE ccy = 'USD'").fetchone()
    assert (usd["rate"], usd["diff"]) == (12023.46, -42.76)


def test_only_filter_limits_sources():
    conn = repo.connect(":memory:")
    results = collect(conn, FakeHttp(ROUTES), SOURCES, only={"kun"})
    assert [r.code for r in results] == ["kun"]


def test_sources_removed_from_registry_are_disabled():
    conn = repo.connect(":memory:")
    repo.sync_sources(conn, SOURCES)
    repo.sync_sources(conn, SOURCES[:1])
    enabled = {row["code"]: row["enabled"] for row in conn.execute("SELECT code, enabled FROM sources")}
    assert enabled == {"kun": 1, "uzdaily": 0, "down": 0, "president": 0, "off": 0}


def test_registry_loads():
    sources = {s.code: s for s in load_sources()}
    assert sources["lex"].enabled and sources["soliq"].enabled and sources["president"].enabled
    assert not sources["miit"].enabled  # ждёт ответа команды
    assert {code for code, s in sources.items() if s.backfill} == {"kun", "uza", "uzdaily", "podrobno", "nuz"}


def test_migrations_apply_once(tmp_path):
    db = tmp_path / "test.sqlite3"
    repo.connect(db).close()
    conn = repo.connect(db)
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]
