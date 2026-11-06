"""Сборка дайджеста дня (этап 6): окно, папка дня, пересборка, повторы, «Обновления», rerender.

Claude подменён (FakeClaude из test_llm), lex.uz — FakeHttp без ответов: реальной сети нет.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeHttp
from test_llm import IMARAT, LLM, FakeClaude

from uzdigest.config import DigestSettings, HttpSettings, RatesSettings, Settings, Source
from uzdigest.db import repo
from uzdigest.extract.normalize import to_item
from uzdigest.llm.client import LlmBudget
from uzdigest.models import FxRate, LegalAct, RawItem
from uzdigest.pipeline import build_digest, digest_window, link_url, rerender, site_name
from uzdigest.timeutil import to_iso

NOW = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # понедельник, 09:30 по Ташкенту
DAY = timedelta(days=1)
SOURCES = [
    Source(code="kun", name="kun", kind="rss", url="https://kun.uz/news/rss", tier="C"),
    Source(code="daryo", name="daryo", kind="rss", url="https://daryo.uz/ru/rss", tier="C"),
    Source(code="soliq", name="soliq", kind="govuz_api", url="https://api-portal.gov.uz/news", tier="A",
           params={"authority": "soliq"}),
]  # fmt: skip
VAT = "Возврат НДС сельхозпроизводителям"
VAT_TEXT = (
    "В соответствии с Указом Президента от 4.09.2025 г. № УП-153 возврат НДС производится автоматически."
)
ROAD = "В Самарканде отремонтировали дорогу к вокзалу"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        timezone="Asia/Tashkent", db_path=tmp_path / "db.sqlite3", outputs_dir=tmp_path / "outputs",
        logs_dir=tmp_path, http=HttpSettings(user_agent="test"), llm=LLM,
        rates=RatesSettings(url="https://cbu.uz/json", currencies=["USD", "EUR"]),
    )  # fmt: skip


def _db():
    conn = repo.connect(":memory:")
    repo.sync_sources(conn, SOURCES)
    repo.upsert_legal_act(conn, _act("UP-153-2025", "153", date(2025, 9, 4)))
    repo.upsert_fx_rates(conn, [FxRate(date(2026, 10, 31), "USD", 1, 12023.46, -42.76),
                                FxRate(date(2026, 10, 31), "EUR", 1, 13461.47, 17.29)])  # fmt: skip
    _polled(conn, NOW)
    return conn


def _act(doc_key: str, number: str, adopted: date) -> LegalAct:
    """Акт с полными реквизитами: сверка находит его в справочнике и в сеть не идёт."""
    return LegalAct(
        doc_key=doc_key, doc_type="UP", number=number, issuer=None, title=None, adopted_on=adopted,
        effective_on=adopted + DAY, lex_id=1000 + int(number), lex_url=None, source="lex_rss",
        uz_lex_id=2000 + int(number), uz_latn_lex_id=3000 + int(number), en_lex_id=4000 + int(number),
    )  # fmt: skip


def _polled(conn, at: datetime) -> None:
    with conn:
        conn.execute("UPDATE sources SET last_ok_at = ?, last_error = NULL", (to_iso(at),))


def _add(conn, code: str, url: str, title: str, text: str, published_at: datetime) -> None:
    ids = {row["code"]: row["id"] for row in conn.execute("SELECT id, code FROM sources")}
    run_id = repo.start_run(conn, "collect")
    with conn:
        raw = RawItem(url, title, None, text, None, published_at)
        repo.insert_item(conn, ids[code], run_id, to_item(raw, "ru", published_at))


def _first_day(conn) -> None:
    _add(
        conn, "kun", "https://kun.uz/1", IMARAT, f"{IMARAT}. Дольщики пострадали.", NOW - timedelta(hours=10)
    )
    _add(
        conn, "daryo", "https://daryo.uz/1", IMARAT, f"{IMARAT}. Ведётся следствие.", NOW - timedelta(hours=9)
    )
    _add(conn, "soliq", "https://gov.uz/ru/soliq/news/view/1", VAT, VAT_TEXT, NOW - timedelta(hours=7))
    _add(conn, "kun", "https://kun.uz/2", ROAD, "Работы завершены досрочно.", NOW - timedelta(hours=6))


def _claude() -> FakeClaude:
    return FakeClaude(triage_answer=lambda item: ("дорогу" not in item["title"], 4))


def _build(conn, settings, now, claude=None, **kwargs):
    http = FakeHttp({})
    budget = LlmBudget(LLM, ask_fn=claude or _claude())
    result = build_digest(conn, settings, http, SOURCES, now, budget=budget, **kwargs)
    return result, http


# --- Окно ---


def _digest_run(conn, since: datetime, until: datetime, status: str = "ok") -> None:
    run_id = repo.start_run(conn, "digest")
    repo.finish_run(conn, run_id, status, {}, (since, until))


@pytest.mark.parametrize(
    ("last_end", "expected_hours", "trimmed"),
    [
        (None, 24, False),  # прошлых дайджестов нет
        (NOW - DAY, 24, False),  # обычный день
        (NOW - 3 * DAY, 72, False),  # понедельник: с пятничного дайджеста
        (NOW - 5 * DAY, 96, True),  # после долгого простоя — не больше 96 ч
        (NOW - timedelta(hours=3), 24, False),  # недавняя сборка — окно всё равно сутки
    ],
)
def test_window(last_end, expected_hours, trimmed):
    conn = repo.connect(":memory:")
    if last_end is not None:
        _digest_run(conn, last_end - DAY, last_end)
    window = digest_window(conn, DigestSettings(), NOW)
    assert (window.until, window.until - window.since, window.trimmed) == (
        NOW, timedelta(hours=expected_hours), trimmed
    )  # fmt: skip


def test_window_ignores_failed_and_draft_runs_and_rebuild_keeps_start():
    conn = repo.connect(":memory:")
    _digest_run(conn, NOW - 4 * DAY, NOW - 3 * DAY, "partial")
    _digest_run(conn, NOW - 2 * DAY, NOW - DAY, "failed")
    _digest_run(conn, NOW - 2 * DAY, NOW - DAY, "draft")
    assert digest_window(conn, DigestSettings(), NOW).since == NOW - 3 * DAY
    morning = {"window_from": to_iso(NOW - 3 * DAY)}
    assert digest_window(conn, DigestSettings(), NOW + timedelta(hours=6), morning).since == NOW - 3 * DAY


def test_site_names_and_links():
    assert site_name("https://www.gazeta.uz/ru/2026/11/02/x/", SOURCES[0]) == "gazeta.uz"
    assert site_name("https://gov.uz/ru/soliq/news/view/1", SOURCES[2]) == "gov.uz/soliq"
    url = "https://uz.kursiv.media/2026-11-03/gaz/?utm_source=rss&utm_medium=rss&id=5"
    assert link_url(url) == "https://uz.kursiv.media/2026-11-03/gaz/?id=5"
    assert link_url("https://www.spot.uz/ru/2026/11/02/x/") == "https://www.spot.uz/ru/2026/11/02/x/"


# --- Сборка дня ---


def test_build_day_folder_then_exists_then_force_with_cache(tmp_path):
    settings = _settings(tmp_path)
    conn = _db()
    _first_day(conn)
    claude = _claude()
    result, http = _build(conn, settings, NOW, claude)
    assert (result.status, http.urls()) == ("ok", [])  # акт в справочнике: lex.uz не спрашивали
    out = tmp_path / "outputs" / "2026-11-02"
    assert sorted(p.name for p in out.iterdir()) == [
        "digest.en.md",
        "digest.json",
        "digest.ru.md",
        "digest.uz.md",
    ]
    data = json.loads((out / "digest.json").read_text(encoding="utf-8"))
    titles = [a["texts"]["ru"]["headline"] for a in data["articles"]]
    # Оба сюжета в разделе «Налоги» с важностью 4: выше тот, о котором писало больше изданий
    assert titles == [f"Кратко: {IMARAT}", f"Кратко: {VAT}"]
    [imarat, vat] = data["articles"]
    assert vat["id"] == "2026-11-02-002" and vat["doc"]["status"] == "verified"
    assert vat["doc"]["lex"] == {
        "ru": {"url": "https://lex.uz/ru/docs/1153", "lang": "ru"},
        "uz": {"url": "https://lex.uz/uz/docs/3153", "lang": "uz"},
        "en": {"url": "https://lex.uz/en/docs/4153", "lang": "en"},
    }
    assert vat["source"]["name"] == "gov.uz/soliq"
    assert [a["name"] for a in imarat["also"]] == ["daryo.uz"]
    assert [r["ccy"] for r in data["rates"]] == ["USD", "EUR"]
    assert data["failures"] == {"sources": [], "no_llm": 0, "no_translation": 0}
    assert conn.execute("SELECT COUNT(*) FROM digest_entries").fetchone()[0] == 2
    run = conn.execute("SELECT * FROM runs WHERE kind = 'digest'").fetchone()
    assert (run["status"], run["window_from"], run["window_to"]) == ("ok", to_iso(NOW - DAY), to_iso(NOW))

    # Повторный запуск в тот же день ничего не делает
    again, _ = _build(conn, settings, NOW + timedelta(hours=1))
    assert again.status == "exists"

    # Пересборка: новость, вышедшая после утра, добавляется; уже готовые тексты из кэша
    _add(conn, "soliq", "https://gov.uz/ru/soliq/news/view/2", "Новые сроки сдачи отчётности по НДС",
         "Отчётность по НДС сдаётся до 20 числа.", NOW + timedelta(hours=2))  # fmt: skip
    _polled(conn, NOW + timedelta(hours=5))
    later = _claude()
    rebuilt, _ = _build(conn, settings, NOW + timedelta(hours=5), later, force=True)
    assert rebuilt.window.since == NOW - DAY
    assert len(rebuilt.digest.articles) == 3  # утренние сюжеты не считаются повторами своего же дня
    assert later.calls == ["TriageResponse", "SummarizeResponse", "TranslateResponse", "TranslateResponse"]
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM digest_entries").fetchone()[0] == 3


def test_next_day_skips_repeats_and_reports_silent_source(tmp_path):
    settings = _settings(tmp_path)
    conn = _db()
    _first_day(conn)
    _build(conn, settings, NOW)

    tomorrow = NOW + DAY
    _add(conn, "kun", "https://kun.uz/3", "Возврат НДС аграриям станет автоматическим", VAT_TEXT,
         tomorrow - timedelta(hours=3))  # fmt: skip
    _polled(conn, tomorrow)
    with conn:
        conn.execute("UPDATE sources SET last_error = 'ConnectTimeout: timed out' WHERE code = 'daryo'")
    result, _ = _build(conn, settings, tomorrow)
    assert result.window.since == NOW
    assert result.digest.articles == []  # тот же УП-153 уже был вчера
    assert [s.code for s in result.digest.failures.sources] == ["daryo"]
    assert result.digest.sources_ok == 2
    ru = (tmp_path / "outputs" / "2026-11-03" / "digest.ru.md").read_text(encoding="utf-8")
    assert "Не ответили: daryo.uz (ConnectTimeout: timed out)." in ru


def test_pending_act_becomes_update_once(tmp_path):
    settings = _settings(tmp_path)
    conn = _db()
    text = "Указом Президента от 30.10.2026 г. № УП-240 налоговые льготы продлены."
    _add(conn, "soliq", "https://gov.uz/ru/soliq/news/view/5", "Налоговые льготы продлены", text,
         NOW - timedelta(hours=5))  # fmt: skip
    first, http = _build(conn, settings, NOW)
    assert http.urls()  # акта нет в справочнике — искали на lex.uz, сеть «упала»
    assert first.status == "partial"  # ошибка lex.uz видна в статусе запуска
    [article] = first.digest.articles
    assert article.doc.status == "pending" and article.doc.lex == {}

    repo.upsert_legal_act(conn, _act("UP-240-2026", "240", date(2026, 10, 30)))  # появился в RSS lex.uz
    second, _ = _build(conn, settings, NOW + DAY)
    [update] = second.digest.updates
    assert (update.doc.doc_key, update.doc.status, update.previous_date) == (
        "UP-240-2026",
        "verified",
        NOW.date(),
    )
    assert update.headlines["ru"] == "Кратко: Налоговые льготы продлены"
    ru = (tmp_path / "outputs" / "2026-11-03" / "digest.ru.md").read_text(encoding="utf-8")
    assert "## Обновления\n\n- Указ Президента № УП-240 от 30.10.2026 — опубликован на lex.uz: " in ru

    third, _ = _build(conn, settings, NOW + 2 * DAY)
    assert third.digest.updates == []  # обновление выходит один раз


def test_without_claude_is_partial_and_rerender_restores_files_offline(tmp_path):
    settings = _settings(tmp_path)
    conn = _db()
    _first_day(conn)
    budget = LlmBudget(LLM.model_copy(update={"max_calls_per_run": 0}), ask_fn=FakeClaude())
    result = build_digest(conn, settings, FakeHttp({}), SOURCES, NOW, budget=budget)
    assert result.status == "partial"
    # Без Claude отбора нет: входит правовой сюжет, текст — начало материала, перевода нет
    [article] = result.digest.articles
    assert (article.doc.doc_key, article.by_llm, article.texts["en"]) == ("UP-153-2025", False, None)
    assert result.digest.failures.no_llm == result.digest.failures.no_translation == 1

    out = tmp_path / "outputs" / "2026-11-02"
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    (out / "digest.en.md").write_text("испорчен", encoding="utf-8")
    assert rerender(conn, settings, date(2026, 11, 2)) == out
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    with pytest.raises(LookupError):
        rerender(conn, settings, date(2026, 11, 5))
