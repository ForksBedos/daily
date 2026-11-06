"""Показатели: что считается из журнала запусков, постов, замечаний и логов. Claude и сеть подменены."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from test_llm import _default_summary
from test_pipeline import NOW, SOURCES, _add, _claude, _polled, _settings
from test_service import _ready_db, _run

from uzdigest import cli, service
from uzdigest.config import ScheduleSettings
from uzdigest.db import repo
from uzdigest.metrics import MAX_PROBLEMS, collect_metrics, render_brief, render_text
from uzdigest.timeutil import to_iso

MONDAY = datetime(2026, 11, 2, tzinfo=UTC)  # 05:00 по Ташкенту
TO_0930 = timedelta(hours=4, minutes=30)  # 09:30 по Ташкенту от полуночи UTC


def _journal(conn, kind, started, status="ok", minutes=8, stats=None, window=None):
    """Строка журнала с заданным временем; status=None — процесс оборвался."""
    finished = to_iso(started + timedelta(minutes=minutes)) if status else None
    window = window or (None, None)
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (kind, started_at, finished_at, status, stats_json, window_from, window_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, to_iso(started), finished, status, json.dumps(stats or {}), window[0], window[1]),
        )
    return cur.lastrowid


def _built(conn, day: date, started: datetime, status="ok", stats=None):
    """Дайджест дня: запись в журнале и строка digests."""
    window = (to_iso(started - timedelta(days=1)), to_iso(started))
    run_id = _journal(conn, "digest", started, status, stats=stats, window=window)
    repo.save_digest(conn, day.isoformat(), "x", run_id, "{}", [])


def _metrics(settings, conn, now, days=30):
    return collect_metrics(conn, settings, days, now)


@pytest.fixture
def setup(tmp_path):
    settings = _settings(tmp_path)
    conn = repo.connect(settings.db_path)
    repo.sync_sources(conn, SOURCES)
    return settings, conn


# --- Надёжность ---


def test_every_weekday_morning_is_on_time_late_or_missed(setup):
    settings, conn = setup
    _built(conn, date(2026, 11, 2), MONDAY + TO_0930)  # готов в 09:38
    _built(conn, date(2026, 11, 4), MONDAY + 2 * timedelta(days=1) + TO_0930 + timedelta(hours=2))  # в 11:38

    reliability = _metrics(settings, conn, MONDAY + timedelta(days=3, hours=8)).reliability

    assert [(d.date.day, d.status) for d in reliability.days] == [
        (2, "on_time"),
        (3, "missed"),
        (4, "late"),
        (5, "missed"),
    ]
    assert reliability.count("missed") == 2 and reliability.count("late") == 1


def test_morning_is_not_missed_before_its_deadline(setup):
    settings, conn = setup
    _built(conn, date(2026, 11, 2), MONDAY + TO_0930)
    before = MONDAY + timedelta(days=1, hours=4, minutes=45)  # вторник, 09:45: сборка ещё идёт

    assert [d.date.day for d in _metrics(settings, conn, before).reliability.days] == [2]


def test_weekend_is_not_a_missed_morning(setup):
    settings, conn = setup
    _built(conn, date(2026, 11, 6), datetime(2026, 11, 6, 4, 30, tzinfo=UTC))  # пятница

    days = _metrics(settings, conn, datetime(2026, 11, 8, 12, tzinfo=UTC)).reliability.days

    assert [d.date.day for d in days] == [6]


def test_rebuild_later_in_the_day_does_not_turn_a_good_morning_late(setup):
    settings, conn = setup
    _built(conn, date(2026, 11, 2), MONDAY + TO_0930)
    _journal(conn, "digest", MONDAY + timedelta(hours=9))

    assert _metrics(settings, conn, MONDAY + timedelta(hours=12)).reliability.days[0].status == "on_time"


def test_failed_and_broken_off_runs_are_counted_separately(setup):
    settings, conn = setup
    now = MONDAY + timedelta(hours=12)
    _journal(conn, "digest", MONDAY + TO_0930, "partial")
    _journal(conn, "digest", MONDAY + timedelta(hours=5), "failed", stats={"error": "ValueError: x"})
    _journal(conn, "digest", MONDAY + timedelta(hours=6), None)
    _journal(conn, "digest", now - timedelta(minutes=10), None)  # идёт прямо сейчас: ещё не оборвался
    _journal(conn, "digest", MONDAY + timedelta(hours=7), "draft")

    reliability = _metrics(settings, conn, now).reliability

    assert (reliability.digest_runs, reliability.digest_partial) == (4, 1)
    assert (reliability.digest_failed, reliability.digest_unfinished) == (1, 1)


def test_collect_slots_follow_the_schedule():
    slots = ScheduleSettings().collect_slots()
    assert slots == ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00"]


def test_skipped_collects_and_the_longest_gap(setup):
    settings, conn = setup
    _built(conn, date(2026, 11, 2), MONDAY + TO_0930)
    tuesday = MONDAY + timedelta(days=1)
    for hours in (3, 5):  # 08:00 и 10:00 вторника
        _journal(conn, "collect", tuesday + timedelta(hours=hours), minutes=1)
    # 12:00, 14:00, 16:00 пропущены: ноутбук был закрыт; 18:30 — запуск после пробуждения
    _journal(conn, "collect", tuesday + timedelta(hours=13, minutes=30), minutes=1)
    _journal(conn, "collect", tuesday + timedelta(hours=15), minutes=1)  # 20:00

    reliability = _metrics(settings, conn, tuesday + timedelta(hours=16)).reliability

    assert reliability.collect_runs == 4
    assert reliability.missed_collects == 3
    assert reliability.longest_gap_hours == 8.5


def test_night_and_weekend_are_not_gaps(setup):
    settings, conn = setup
    friday = datetime(2026, 11, 6, tzinfo=UTC)
    _built(conn, date(2026, 11, 5), friday - timedelta(days=1) + TO_0930)
    slots = (3, 5, 7, 9, 11, 13, 15)
    for hours in slots:
        _journal(conn, "collect", friday + timedelta(hours=hours), minutes=1)
    monday = friday + timedelta(days=3)
    _journal(conn, "collect", monday + timedelta(hours=3), minutes=1)

    reliability = _metrics(settings, conn, monday + timedelta(hours=4)).reliability

    assert (reliability.missed_collects, reliability.longest_gap_hours) == (0, None)


# --- Путь материалов ---


def test_funnel_follows_the_news_from_collecting_to_the_digest(tmp_path):
    settings = _settings(tmp_path)
    conn = _ready_db(settings)
    _add(conn, "kun", "https://kun.uz/spam", "Новый фильм вышел в прокат", "Кино.", NOW - timedelta(hours=3))

    result = _run(settings)
    assert result.status == "ok"
    with conn:
        conn.execute("UPDATE items SET prefilter = 'filtered' WHERE title LIKE 'Новый фильм%'")

    day = _metrics(settings, conn, NOW + timedelta(minutes=10)).funnel.days[0]
    assert (day.date, day.collected) == (date(2026, 11, 2), 5)
    assert day.candidates == result.digest.stats["items"] > 0
    assert day.relevant == result.digest.stats["relevant"] > 0
    assert day.articles == len(service.digest(settings=settings).articles) > 0
    assert day.relevant_stories == day.articles and day.repeats == 0


def test_item_inside_a_window_that_no_build_saw_is_shown(tmp_path):
    settings = _settings(tmp_path)
    conn = _ready_db(settings)
    _run(settings)
    # Вышел до конца окна, а в базу попал после сборки: сборка его не видела
    _add(conn, "kun", "https://kun.uz/late", "Опоздавшая новость", "Текст.", NOW - timedelta(minutes=2))

    funnel = _metrics(settings, conn, NOW + timedelta(hours=2)).funnel

    assert funnel.unseen == 1
    assert funnel.unseen_examples == ["kun: Опоздавшая новость"]


# --- Источники ---


def _poll(conn, started, **results):
    sources = [{"code": code, **result} for code, result in results.items()]
    _journal(conn, "collect", started, stats={"sources": sources}, minutes=1)


def test_failure_and_silence_are_different_things(setup):
    settings, conn = setup
    start = MONDAY + timedelta(hours=3)
    _poll(
        conn,
        start,
        kun={"status": "ok", "new": 2},
        daryo={"status": "ok", "new": 4},
        soliq={"status": "ok", "new": 1},
    )
    later = start + timedelta(days=3)
    _poll(
        conn,
        later,
        kun={"status": "error", "new": 0, "error": "ConnectError: нет сети"},
        daryo={"status": "ok", "new": 3},
        soliq={"status": "ok", "new": 0},
    )

    sources = {s.code: s for s in _metrics(settings, conn, later + timedelta(hours=1)).sources}

    assert (sources["kun"].errors, sources["kun"].polls) == (1, 2)
    assert sources["kun"].last_error == "ConnectError: нет сети"
    assert sources["soliq"].errors == 0 and sources["soliq"].silent_days == 3.0
    assert sources["daryo"].silent_days == 0.0 and sources["daryo"].new == 7


def test_report_names_failing_and_silent_sources_in_plain_words(setup):
    settings, conn = setup
    _built(conn, date(2026, 11, 2), MONDAY + TO_0930)
    start = MONDAY + timedelta(hours=3)
    _poll(conn, start, kun={"status": "ok", "new": 2}, soliq={"status": "ok", "new": 1})
    later = start + timedelta(days=3)
    _poll(
        conn,
        later,
        kun={"status": "error", "new": 0, "error": "ConnectError: нет сети"},
        soliq={"status": "ok", "new": 0},
    )

    text = render_text(_metrics(settings, conn, later + timedelta(hours=1)))

    assert "сбой, kun: 1 из 2" in text and "ConnectError: нет сети" in text
    assert "молчит, soliq: нового нет 3 дн." in text


def test_source_removed_from_the_registry_is_not_reported(setup):
    settings, conn = setup
    _poll(conn, MONDAY, old={"status": "error", "new": 0, "error": "x"})

    assert "old" not in {s.code for s in _metrics(settings, conn, MONDAY + timedelta(hours=1)).sources}


# --- Claude ---


def test_claude_requests_and_tokens_of_digests_and_posts(setup):
    settings, conn = setup
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 1000,
        "output_tokens": 5,
    }
    llm = {
        "calls": 3,
        "failed": 1,
        "duration_ms": 9000,
        "usage": usage,
        "stopped": "лимит подписки Claude исчерпан",
        "by_purpose": {"triage": {"calls": 1, "tokens": 1110}, "translate": {"calls": 2, "failed": 1}},
    }
    _journal(conn, "digest", MONDAY + TO_0930, stats={"llm": llm})
    _journal(conn, "digest", MONDAY + TO_0930 + timedelta(hours=1), "draft", stats={"llm": llm})
    post = {
        "digest_date": "2026-11-02", "article_id": "2026-11-02-001", "platform": "telegram", "lang": "ru",
        "mode": "new", "text": "т", "chars": 1, "hashtags": [], "notes": "", "qa_flags": [],
        "prompt_version": "post_v1", "input_hash": "h", "model": "m", "author": None, "path": None,
    }  # fmt: skip
    usage = json.dumps({"input_tokens": 7, "output_tokens": 3})
    repo.save_post(conn, {**post, "created_at": MONDAY + timedelta(hours=6), "calls": 1,
                          "usage_json": usage, "duration_ms": 2000})  # fmt: skip
    repo.save_post(conn, {**post, "created_at": MONDAY + timedelta(hours=7), "calls": 2})

    claude = _metrics(settings, conn, MONDAY + timedelta(hours=8)).claude

    assert (claude.digest_calls, claude.digest_failed, claude.post_calls) == (6, 2, 3)
    assert claude.tokens_in == 2 * 1110 + 7
    assert claude.tokens_cached == 2000 and claude.tokens_out == 2 * 5 + 3
    assert claude.seconds == 20
    assert claude.posts_without_usage == 1
    assert claude.by_purpose["translate"] == {"calls": 4, "failed": 2}
    assert claude.stops[0].endswith("лимит подписки Claude исчерпан")


def test_collect_journal_keeps_texts_and_rates(tmp_path):
    settings = _settings(tmp_path)
    conn = _ready_db(settings)

    _run(settings, "collect", now=NOW)

    stats = repo.json_field(repo.last_run(conn, "collect"), "stats_json")
    assert stats["texts"]["failed"] == sum(stats["texts"]["failed_by_source"].values()) > 0
    assert stats["rates"]["error"].startswith("ConnectError")
    assert _metrics(settings, conn, NOW + timedelta(hours=1)).reliability.rates_failed == 1


# --- Проверка фактов, посты, замечания, что повторяется ---


def test_fact_check_hits_are_counted_by_kind(tmp_path):
    settings = _settings(tmp_path)
    conn = _ready_db(settings)

    def invented(payload):
        return {**_default_summary(payload), "details": "Штраф составит 7777 сумов."}

    claude = _claude()
    claude.summary_answer = invented
    _run(settings, claude=claude)

    facts = _metrics(settings, conn, NOW + timedelta(hours=1)).facts

    assert facts.articles == facts.articles_flagged > 0
    assert facts.article_kinds["число или дата не из источника"] == facts.articles


def test_posts_show_platforms_and_how_many_were_redone(setup):
    settings, conn = setup
    post = {
        "created_at": MONDAY + timedelta(hours=6), "digest_date": "2026-11-02", "lang": "ru", "text": "т",
        "chars": 1, "hashtags": [], "notes": "", "prompt_version": "post_v1", "input_hash": "h", "model": "m",
        "calls": 1, "path": None,
    }  # fmt: skip
    rows = [
        ("001", "telegram", "new", "Админ 1", []),
        ("001", "telegram", "shorter", "Админ 1", []),
        ("002", "linkedin", "new", None, ["длина 3200 больше лимита 3000", "нет в статье: 5 млн"]),
    ]
    for number, platform, mode, author, flags in rows:
        article = f"2026-11-02-{number}"
        repo.save_post(conn, {**post, "article_id": article, "platform": platform, "mode": mode,
                              "author": author, "qa_flags": flags})  # fmt: skip

    m = _metrics(settings, conn, MONDAY + timedelta(hours=8))

    assert (m.posts.total, m.posts.articles, m.posts.redone) == (3, 2, 1)
    assert m.posts.by_platform == {"telegram": 2, "linkedin": 1}
    assert m.posts.by_author == {"Админ 1": 2, "командная строка": 1}
    assert (m.facts.posts, m.facts.posts_flagged) == (3, 1)
    assert m.facts.post_kinds == {"длиннее лимита площадки": 1, "число или дата не из статьи": 1}


def test_team_feedback_by_kind_with_the_latest_first(tmp_path):
    settings = _settings(tmp_path)
    repo.connect(settings.db_path)
    service.add_feedback("duplicate", "Цена газа — три темы", settings=settings, now=NOW)
    service.add_feedback("duplicate", "Снова одна и та же\nновость", settings=settings, now=NOW)
    service.add_feedback("error", "Не тот номер указа", settings=settings, now=NOW)

    feedback = service.metrics(settings=settings, now=NOW + timedelta(hours=1)).feedback

    assert feedback.by_kind == {"duplicate": 2, "error": 1}
    assert feedback.recent[:2] == ["Не тот номер указа", "Снова одна и та же новость"]


def test_repeated_problems_come_from_the_journal_logs_and_people(setup):
    settings, conn = setup
    for hours in (3, 5):
        _poll(
            conn,
            MONDAY + timedelta(hours=hours),
            kun={"status": "error", "new": 0, "error": "ConnectError: нет"},
        )
    log = settings.logs_dir / "bot.log"
    log.write_text(
        "2026-11-02 09:00:01,000 ERROR uzdigest.bot.app: Бот: не удалось отправить в чат 111: таймаут\n"
        "Traceback (most recent call last):\n"
        "2026-11-02 09:05:01,000 ERROR uzdigest.bot.app: Бот: не удалось отправить в чат 222: таймаут\n"
        "2026-11-02 09:06:01,000 WARNING uzdigest.collect: Источник kun: ConnectError: нет\n"
        "2026-11-02 09:07:01,000 ERROR uzdigest.bot.app: случилось один раз\n"
        "2026-10-01 09:07:01,000 ERROR uzdigest.bot.app: Бот: не удалось отправить в чат 333: таймаут\n",
        encoding="utf-8",
    )
    service.add_feedback("duplicate", "Дубль 1", settings=settings, now=MONDAY)
    service.add_feedback("duplicate", "Дубль 2", settings=settings, now=MONDAY)

    problems = {p.what: p for p in _metrics(settings, conn, MONDAY + timedelta(hours=8), days=7).problems}

    assert problems["источник kun: ConnectError"].count == 2  # из журнала, лог не добавил третий раз
    assert problems["Бот: не удалось отправить в чат N: таймаут"].count == 2  # старый за окном не считается
    assert problems["замечание коллег: дубль"].count == 2
    assert not any("один раз" in what for what in problems)


def test_list_of_repeats_is_short(setup):
    settings, conn = setup
    lines = [f"2026-11-02 09:00:01,000 ERROR x: проблема {'а' * n}\n" for n in range(1, 20) for _ in range(2)]
    (settings.logs_dir / "uzdigest.log").write_text("".join(lines), encoding="utf-8")

    assert len(_metrics(settings, conn, MONDAY + timedelta(hours=8)).problems) == MAX_PROBLEMS


# --- Тексты ---


def test_empty_project_has_a_clear_report(setup):
    settings, conn = setup

    m = _metrics(settings, conn, MONDAY)

    assert "Дайджестов ещё не было." in render_text(m)
    assert "Постов пока нет." in render_brief(m)


def test_report_of_a_full_day_is_in_plain_words(tmp_path):
    settings = _settings(tmp_path)
    conn = _ready_db(settings)
    _polled(conn, NOW)
    _run(settings)
    service.add_feedback("post", "Слишком длинно", settings=settings, now=NOW)
    with conn:  # журнал пишет настоящие часы, а день в тесте свой
        conn.execute("UPDATE runs SET started_at = ?, finished_at = ?", (to_iso(NOW), to_iso(NOW)))

    m = service.metrics(settings=settings, now=NOW + timedelta(minutes=20))
    text, brief = render_text(m), render_brief(m)

    for line in (
        "НАДЁЖНОСТЬ",
        "ПУТЬ МАТЕРИАЛОВ",
        "ИСТОЧНИКИ",
        "CLAUDE",
        "ПРОВЕРКА ФАКТОВ",
        "ЗАМЕЧАНИЯ",
        "ПОВТОРЯЕТСЯ",
    ):
        assert line in text
    assert "Будних утр: 1. Дайджест вовремя: 1" in brief
    assert "в дайджесте" in brief and "Замечаний: 1 (пост 1)." in brief
    assert "stats_json" not in brief and "None" not in brief


def test_cli_prints_the_report_and_the_numbers(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    repo.connect(settings.db_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "setup_logging", lambda folder, name: None)

    assert cli.main(["metrics", "--days", "7"]) == 0
    assert "Показатели UzDigest за 7 дней" in capsys.readouterr().out

    assert cli.main(["metrics", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["days"] == 30
