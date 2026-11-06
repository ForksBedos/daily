"""Слой управления (этап 8): блокировка, бэкап, уведомители, run, статус и его текст, замечания.

Сеть и Claude подменены: lex.uz и источники — FakeHttp без ответов, Claude — FakeClaude.
"""

import csv
import json
import os
import time
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import FakeHttp
from test_llm import LLM, FakeClaude
from test_pipeline import NOW, SOURCES, _act, _claude, _first_day, _polled, _settings

from uzdigest import service
from uzdigest.db import repo
from uzdigest.llm.client import LlmBudget
from uzdigest.models import FxRate
from uzdigest.ops import Busy, backup_db, notify, run_lock


def _ready_db(settings):
    conn = repo.connect(settings.db_path)
    repo.sync_sources(conn, SOURCES)
    repo.upsert_legal_act(conn, _act("UP-153-2025", "153", date(2025, 9, 4)))
    repo.upsert_fx_rates(conn, [FxRate(date(2026, 10, 31), "USD", 1, 12023.46, -42.76)])
    _first_day(conn)
    _polled(conn, NOW)
    return conn


def _run(settings, kind="digest", claude=None, now=NOW, **kwargs):
    return service.run(
        kind, settings=settings, sources=SOURCES, http_factory=lambda: nullcontext(FakeHttp({})),
        budget=LlmBudget(LLM, ask_fn=claude or _claude()), now=now, **kwargs,
    )  # fmt: skip


# --- Блокировка, бэкап, уведомители ---


def test_lock_busy_then_released_and_stale_lock_removed(tmp_path):
    path = tmp_path / "run.lock"
    with run_lock(path, "run", NOW):
        assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "run"
        with pytest.raises(Busy) as busy:
            with run_lock(path, "collect", NOW + timedelta(minutes=5)):
                pass
        assert busy.value.info.kind == "run"
    assert not path.exists()
    # Зависшая: старше 2 ч — снимается
    path.write_text(json.dumps({"kind": "run", "pid": 1, "started_at": NOW.isoformat()}), encoding="utf-8")
    with run_lock(path, "digest", NOW + timedelta(hours=3)):
        assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "digest"
    # Испорченный файл: возраст по дате файла
    path.write_text("{обрыв", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(path, (old, old))
    with run_lock(path, "run"):
        pass


def test_backup_keeps_only_daily_copies_when_asked(tmp_path):
    conn = repo.connect(tmp_path / "db.sqlite3")
    folder = tmp_path / "backup"
    folder.mkdir()
    manual = folder / "uzdigest-before-006.sqlite3"
    manual.write_text("ручная копия", encoding="utf-8")
    for day in range(1, 5):
        backup_db(conn, folder, date(2026, 11, day), keep_last=None)
    assert len(list(folder.iterdir())) == 5  # без keep_last ничего не удаляется
    backup_db(conn, folder, date(2026, 11, 5), keep_last=2)
    assert sorted(p.name for p in folder.iterdir()) == [
        "uzdigest-2026-11-04.sqlite3", "uzdigest-2026-11-05.sqlite3", "uzdigest-before-006.sqlite3"
    ]  # fmt: skip


def test_failing_notifier_does_not_break_run(caplog):
    called = []

    def broken(result):
        raise RuntimeError("Telegram недоступен")

    notify(["broken", "ok", "nope"], "итог", {"broken": broken, "ok": called.append})
    assert called == ["итог"]
    assert "Уведомитель broken не сработал" in caplog.text and "Неизвестный уведомитель nope" in caplog.text


# --- run и get_or_build_digest ---


def test_run_digest_backup_then_exists_busy_and_failed(tmp_path):
    settings = _settings(tmp_path)
    _ready_db(settings)
    result = _run(settings)
    assert (result.status, result.digest.articles, result.digest.legal) == ("ok", 2, 1)
    assert result.backup.endswith("uzdigest-2026-11-02.sqlite3") and os.path.exists(result.backup)
    assert result.digest.files()["json"].exists()
    assert not (tmp_path / "run.lock").exists()

    # Уже собран: get_or_build_digest не запускает ничего
    again = service.get_or_build_digest(settings=settings, now=NOW + timedelta(hours=1))
    assert (again.status, again.digest.articles) == ("exists", 2)
    assert service.describe_run(again) == "Дайджест за 02.11.2026 уже готов: 2 статьи."

    # Занято другим запуском
    with run_lock(tmp_path / "run.lock", "collect", NOW):
        busy = _run(settings, now=NOW + timedelta(minutes=10))
    assert busy.status == "busy"
    assert service.describe_run(busy, NOW + timedelta(minutes=10)) == (
        "Сбор уже идёт (начат сегодня в 09:30), пришлю, как будет готово."
    )

    def broken_http():
        raise OSError("нет сети")

    failed = service.run("digest", settings=settings, sources=SOURCES, http_factory=broken_http, now=NOW)
    assert (failed.status, failed.error) == ("failed", "OSError: нет сети")
    assert not (tmp_path / "run.lock").exists()  # блокировка снята и после сбоя


def test_force_rebuild_collects_and_reuses_claude_cache(tmp_path):
    settings = _settings(tmp_path)
    _ready_db(settings)
    _run(settings)
    claude = FakeClaude()
    rebuilt = service.get_or_build_digest(
        force=True, settings=settings, sources=SOURCES, http_factory=lambda: nullcontext(FakeHttp({})),
        budget=LlmBudget(LLM, ask_fn=claude), now=NOW + timedelta(hours=2),
    )  # fmt: skip
    # Источники без сети не ответили — сбор частичный, но дайджест пересобран без новых вызовов Claude
    assert rebuilt.status == "partial" and rebuilt.collect.status == "partial"
    assert (rebuilt.digest.status, rebuilt.digest.articles, claude.calls) == ("ok", 2, [])


# --- Статус ---


def test_status_and_its_text(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    conn = repo.connect(settings.db_path)
    repo.sync_sources(conn, SOURCES)
    empty = service.status(settings=settings, now=NOW)
    assert empty.last_digest is None and empty.running is None
    # Источники ещё ни разу не опрашивались — тоже «не отвечают»
    assert service.describe_status(empty).startswith(
        "Дайджестов ещё нет.\nНе отвечают: Налоговый комитет (gov.uz), Daryo, Kun.uz."
    )

    _ready_db(settings)
    _run(settings)
    with conn:
        conn.execute("UPDATE sources SET last_error = 'ConnectTimeout' WHERE code = 'daryo'")
    collect_run = repo.start_run(conn, "collect")
    repo.finish_run(conn, collect_run, "partial", {})
    with conn:
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (NOW.isoformat(), collect_run))
    state = service.status(settings=settings, now=NOW + timedelta(minutes=30))  # пн 10:00
    assert state.failing_sources == [("daryo", "ConnectTimeout")]
    assert service.describe_status(state) == (
        "Сегодняшний дайджест — за 02.11.2026: 2 статьи, правовых 1.\n"
        "В нём статьи с пометкой «проверить» — 1.\n"  # замечание проверки к тексту тестового Claude
        "Последний сбор новостей — сегодня в 09:30, с ошибками части источников.\n"
        "Не отвечают: Daryo.\n"
        "В последней сборке Claude отвечал без сбоев.\n"
        "Постов сегодня: 0 из 40.\n"
        "Следующий дайджест — завтра в 09:30, следующий сбор — сегодня в 12:00."
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    assert "Claude не подключён: нет токена подписки" in service.describe_status(
        service.status(settings=settings)
    )


def test_status_says_when_the_last_build_failed_instead_of_showing_the_old_digest_as_is(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    conn = _ready_db(settings)
    _run(settings)
    failed = repo.start_run(conn, "digest")
    repo.finish_run(conn, failed, "failed", {"error": "OSError: диск заполнен"})

    state = service.status(settings=settings, now=NOW + timedelta(hours=1))

    assert "Последняя сборка не удалась" in service.describe_status(state)
    assert "OSError: диск заполнен" in service.describe_status(state)
    new = repo.start_run(conn, "digest")
    repo.finish_run(conn, new, "ok", {})
    assert "не удалась" not in service.describe_status(service.status(settings=settings))


def test_status_does_not_claim_claude_works_when_its_calls_failed(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    conn = _ready_db(settings)
    run_id = repo.start_run(conn, "digest")
    repo.finish_run(conn, run_id, "partial", {"llm": {"calls": 5, "failed": 2}})

    text = service.describe_status(service.status(settings=settings, now=NOW))

    assert "Claude в последней сборке: неудачных вызовов 2." in text
    assert "без сбоев" not in text


def test_status_tells_a_collect_where_nothing_answered_from_a_partial_one(tmp_path):
    settings = _settings(tmp_path)
    conn = repo.connect(settings.db_path)
    run_id = repo.start_run(conn, "collect")
    repo.finish_run(conn, run_id, "failed", {})

    assert "ни один источник не ответил" in service.describe_status(service.status(settings=settings))


def test_message_about_a_collect_counts_backfilled_items_once():
    run = service.SourceRun(code="kun", status="ok", fetched=12, new=7, backfilled=5, error=None, warnings=0)
    result = service.RunResult(
        kind="collect", status="ok", started_at=NOW, finished_at=NOW,
        collect=service.CollectSummary(status="ok", sources=[run]),
    )  # fmt: skip

    assert service.describe_run(result) == "Сбор новостей: 7 новых материалов."


def test_message_about_an_empty_digest_does_not_say_ready(tmp_path):
    settings = _settings(tmp_path)
    repo.sync_sources(repo.connect(settings.db_path), SOURCES)

    result = _run(settings)

    assert result.status == "partial"
    assert service.describe_run(result).startswith("Дайджест за 02.11.2026 собран, но статей в нём нет.")


def test_error_of_a_run_is_masked_before_it_reaches_people(tmp_path):
    settings = _settings(tmp_path)

    def broken_http():
        raise OSError("ошибка на /bot1234567890:AAExampleExampleExampleExampleExample1/sendMessage")

    failed = service.run("digest", settings=settings, sources=SOURCES, http_factory=broken_http, now=NOW)

    assert "AAExample" not in failed.error and "***" in failed.error


def test_next_runs_skip_weekends(tmp_path):
    settings = _settings(tmp_path)
    friday_evening = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)  # пт 21:00 по Ташкенту
    assert service.next_digest_at(settings, friday_evening).strftime("%a %H:%M") == "Mon 09:30"
    assert service.next_collect_at(settings, friday_evening).strftime("%a %H:%M") == "Mon 08:00"
    monday = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)  # пн 08:00 — этот слот уже идёт
    assert service.next_collect_at(settings, monday).strftime("%H:%M") == "10:00"
    assert service.next_digest_at(settings, monday).strftime("%d %H:%M") == "02 09:30"


# --- Замечания ---


def test_feedback_add_list_export(tmp_path):
    settings = _settings(tmp_path)
    item = service.add_feedback(
        "duplicate", "  Газ — три одинаковые темы  ", article_id="2026-11-03-015", author="СММ",
        settings=settings, now=NOW,
    )  # fmt: skip
    assert (item.digest_date, item.text, item.source) == (
        date(2026, 11, 3),
        "Газ — три одинаковые темы",
        "cli",
    )
    service.add_feedback("other", "Общее замечание", settings=settings, now=NOW)
    assert [f.kind for f in service.list_feedback(settings=settings)] == ["duplicate", "other"]
    assert [f.id for f in service.list_feedback(date(2026, 11, 3), settings=settings)] == [item.id]
    with pytest.raises(ValueError):
        service.add_feedback("спам", "x", settings=settings)
    with pytest.raises(ValueError):
        service.add_feedback("other", "   ", settings=settings)

    path = service.export_feedback(tmp_path / "feedback.csv", settings=settings)
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file, delimiter=";"))
    assert rows[0][:5] == ["id", "создано", "дайджест", "статья", "тип"]
    assert rows[1][1:7] == ["02.11.2026 09:30", "2026-11-03", "2026-11-03-015", "дубль",
                            "Газ — три одинаковые темы", "СММ"]  # fmt: skip
