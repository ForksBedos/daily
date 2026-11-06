"""Проходы Claude (этап 5) с подменённым клиентом: реальных вызовов нет."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from uzdigest.config import HttpSettings, LlmSettings, Settings, Source
from uzdigest.db import repo
from uzdigest.dedup.clustering import Candidate, Cluster, Pair
from uzdigest.dedup.stage import prepare_window
from uzdigest.extract.normalize import to_item
from uzdigest.filter.prefilter import Keywords
from uzdigest.legal.doc_key import find_refs
from uzdigest.llm.client import LlmBudget, LlmError, LlmResult, LlmUnavailable
from uzdigest.llm.prompt import load_prompt
from uzdigest.llm.schemas import (
    SummarizeResponse,
    TriageItem,
    TriageResponse,
)
from uzdigest.llm.stage import process_window, select
from uzdigest.llm.summarize import CUT_MARK, Job, cut_text, summarize
from uzdigest.llm.translate import Glossary, translate
from uzdigest.llm.triage import Verdict, _batches, triage
from uzdigest.models import LegalAct, RawItem

T0 = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
LLM = LlmSettings(
    model="test-model", max_calls_per_run=60, triage_batch=80, summarize_batch=5, translate_batch=10
)


def _json_after(prompt: str, header: str):
    """JSON-блок запроса после строки «Заголовок:»."""
    start = prompt.index(header) + len(header)
    block = prompt[start:].split("\n\n", 1)[0]
    return json.loads(block)


class FakeClaude:
    """Отвечает по содержимому запроса. calls — имена схем по порядку вызовов.

    triage_answer(item) → (relevant, importance); summary_answer(payload) → поля Summary;
    translation_answer(payload, lang) → поля перевода.
    """

    def __init__(self, triage_answer=None, summary_answer=None, translation_answer=None, same_pairs=()):
        self.triage_answer = triage_answer or (lambda item: (True, 3))
        self.summary_answer = summary_answer or _default_summary
        self.translation_answer = translation_answer or (lambda payload, lang: payload)
        self.same_pairs = set(same_pairs)
        self.calls: list[str] = []

    def __call__(self, prompt, schema, *, settings, system_prompt):
        self.calls.append(schema.__name__)
        if schema is TriageResponse:
            items = _json_after(prompt, "Материалы:\n")
            pairs = _json_after(prompt, "Пары для сравнения:\n") if "Пары для сравнения:\n" in prompt else []
            data = {
                "items": [
                    {"id": item["id"], "relevant": rel, "category": "taxes" if rel else None,
                     "importance": imp, "reason": "тест"}
                    for item in items
                    for rel, imp in [self.triage_answer(item)]
                ],
                "pairs": [{"a": a, "b": b, "same": (a, b) in self.same_pairs} for a, b in pairs],
            }  # fmt: skip
        elif schema is SummarizeResponse:
            data = {"items": [self.summary_answer(p) for p in _json_after(prompt, "Сюжеты:\n")]}
        else:
            lang = "en" if "английский" in system_prompt else "uz"
            data = {"items": [self.translation_answer(p, lang) for p in _json_after(prompt, "Статьи:\n")]}
        return LlmResult(schema.model_validate(data), 1000, {"input_tokens": 10, "output_tokens": 5})


def _default_summary(payload):
    return {
        "cluster_id": payload["cluster_id"],
        "headline": f"Кратко: {payload['title']}",
        "summary": "Суть новости.",
        "details": "Подробности новости.",
        "legal_refs": [],
        "act_matches": None if payload["act"] is None else True,
    }


def cand(id, title, text, tier="A", source="soliq", hours=0.0):
    published_at = T0 + timedelta(hours=hours)
    refs = tuple(find_refs(f"{title}\n{text}", published_at.year))
    return Candidate(id, source, tier, title, text, f"https://{source}.uz/{id}", published_at, refs)


# --- Схемы и промпты ---


def test_schema_rejects_extra_missing_and_out_of_range_fields():
    valid = {"id": 1, "relevant": True, "category": "taxes", "importance": 4, "reason": "НДС"}
    assert TriageItem.model_validate(valid).importance == 4
    for broken in (
        {**valid, "extra": 1},
        {k: v for k, v in valid.items() if k != "reason"},
        {**valid, "importance": 7},
        {**valid, "category": "sport"},
    ):
        with pytest.raises(ValidationError):
            TriageItem.model_validate(broken)
    assert TriageResponse.model_json_schema()["additionalProperties"] is False


def test_prompts_and_glossary_fill_all_placeholders():
    for version in ("triage_v4", "summarize_v1", "translate_v1", "post_v2"):
        assert load_prompt(version).text
    glossary = Glossary.load()
    for lang in ("en", "uz"):
        system = glossary.system_prompt(load_prompt("translate_v1"), lang)
        assert "{{" not in system and "НДС → " + ("VAT" if lang == "en" else "QQS") in system


# --- Счётчик вызовов и резервный режим ---


def test_budget_stops_at_call_limit_and_sums_usage():
    fake = FakeClaude()
    budget = LlmBudget(LLM.model_copy(update={"max_calls_per_run": 2}), ask_fn=fake)
    for _ in range(3):
        budget.ask("Материалы:\n[]\n\nПар для сравнения нет", TriageResponse, "")
    assert (budget.calls, len(fake.calls)) == (2, 2)
    assert budget.stopped.startswith("исчерпан лимит вызовов")
    assert budget.usage["input_tokens"] == 20 and budget.stats()["calls"] == 2


def test_budget_unavailable_stops_but_single_failure_does_not():
    def failing(error):
        def ask(*args, **kwargs):
            raise error

        return ask

    budget = LlmBudget(LLM, ask_fn=failing(LlmError("схема")))
    assert budget.ask("x", TriageResponse, "") is None
    assert (budget.failed, budget.stopped) == (1, None)
    budget = LlmBudget(LLM, ask_fn=failing(LlmUnavailable("лимит подписки Claude исчерпан")))
    assert budget.ask("x", TriageResponse, "") is None
    assert budget.ask("x", TriageResponse, "") is None
    assert (budget.calls, budget.stopped) == (1, "лимит подписки Claude исчерпан")


def test_budget_counts_calls_failures_and_tokens_by_purpose():
    answers = iter([None, LlmError("схема")])

    def ask(prompt, schema, *, settings, system_prompt):
        answer = next(answers)
        if answer is not None:
            raise answer
        return FakeClaude()("Материалы:\n[]", schema, settings=settings, system_prompt=system_prompt)

    budget = LlmBudget(LLM, ask_fn=ask)
    budget.ask("x", TriageResponse, "", "triage")
    budget.ask("x", TriageResponse, "", "summarize")

    assert budget.stats()["by_purpose"] == {
        "triage": {"calls": 1, "tokens": 15},
        "summarize": {"calls": 1, "failed": 1},
    }


# --- Отбор ---


def test_batches_keep_pairs_together():
    items = [cand(i, f"Новость {i}", "") for i in range(1, 6)]
    by_id = {c.id: c for c in items}
    batches = list(_batches(items, [Pair(2, 5, None, 0.3, "gray")], by_id, 2))
    assert [[c.id for c in batch] for batch, _ in batches] == [[1, 2, 5], [3, 4], [5]]
    assert [len(pairs) for _, pairs in batches] == [1, 0, 0]


def test_triage_saves_verdicts_and_pair_decisions_and_uses_cache():
    conn, ids = _db_with_items(3)
    candidates = [cand(i, f"Новость {i}", "Текст") for i in ids]
    fake = FakeClaude(triage_answer=lambda item: (item["id"] != ids[2], 4), same_pairs={(ids[0], ids[1])})
    gray = [Pair(ids[0], ids[1], None, 0.3, "gray")]
    verdicts = triage(conn, LlmBudget(LLM, ask_fn=fake), candidates, gray, 80)
    assert [verdicts[i].relevant for i in ids] == [True, True, False]
    assert verdicts[ids[0]] == Verdict(True, "taxes", 4, "тест", "llm")
    assert repo.pair_decisions(conn, ids) == {(ids[0], ids[1]): "same"}

    again = FakeClaude()
    assert triage(conn, LlmBudget(LLM, ask_fn=again), candidates, gray, 80) == verdicts
    assert again.calls == []  # оценки и решение по паре — из БД


def test_triage_fallback_without_claude():
    conn, ids = _db_with_items(3)
    candidates = [
        cand(ids[0], "Разъяснения", "Указом Президента от 4.09.2025 г. № УП-153 изменён порядок.", tier="A"),
        cand(ids[1], "Новость", "Текст", tier="B"),
        cand(ids[2], "Новость", "Текст", tier="C"),
    ]
    budget = LlmBudget(LLM.model_copy(update={"max_calls_per_run": 0}), ask_fn=FakeClaude())
    verdicts = triage(conn, budget, candidates, [], 80)
    assert [(v.relevant, v.category, v.origin) for v in verdicts.values()] == [
        (True, "legislation", "fallback"), (True, "business", "fallback"), (False, None, "fallback"),
    ]  # fmt: skip
    assert repo.triage_rows(conn, ids, "triage_v4") == {}  # резервные оценки не кэшируются


def test_select_business_legal_first_then_by_importance_up_to_limit():
    clusters = [
        Cluster([1], "news", None), Cluster([2], "legal", "PKM-594-2026"), Cluster([3, 4], "news", None),
        Cluster([5], "news", None), Cluster([6], "legal", "PP-1-2026"), Cluster([7], "legal", "UP-236-2026"),
    ]  # fmt: skip
    verdicts = {
        1: Verdict(True, "taxes", 3, "", "llm"), 2: Verdict(True, "legislation", 2, "", "llm"),
        3: Verdict(True, "finance", 5, "", "llm"), 4: Verdict(True, "finance", 4, "", "llm"),
        5: Verdict(True, "business", 2, "", "llm"), 6: Verdict(True, "legislation", 5, "", "llm"),
        7: Verdict(False, None, 1, "помощь населению", "llm"),
    }  # fmt: skip
    statuses = {"PKM-594-2026": "pending", "PP-1-2026": "not_found", "UP-236-2026": "verified"}
    settings = Settings.model_fields["digest"].default_factory().model_copy(update={"max_articles": 3})
    # ПКМ-594 полезен бизнесу и pending — входит при любой важности; PP-1 с not_found соревнуется
    # на общих основаниях; УП-236 о помощи населению не входит, хотя подтверждён на lex.uz
    assert select(clusters, verdicts, statuses, settings) == [1, 2, 4]
    assert select(clusters, verdicts, statuses, settings, skip={2}) == [1, 4, 0]


# --- Тексты и перевод ---


def _job(conn, n, text, *, doc_key=None, act=None):
    """Сюжет n из одного материала; кластер пишется в БД (на него ссылается summaries)."""
    item_id = conn.execute("SELECT id FROM items ORDER BY id LIMIT 1 OFFSET ?", (n - 1,)).fetchone()[0]
    cluster_id = repo.upsert_cluster(
        conn, None, "legal" if doc_key else "news", item_id, doc_key, T0.isoformat()
    )
    primary = cand(item_id, "Принято постановление о кибербезопасности", text)
    return Job(cluster_id, primary, [], doc_key, act, "legislation", 4)


def test_summary_qa_flags_and_cache():
    conn, _ = _db_with_items(3)
    act = LegalAct("PKM-594-2026", "PKM", "594", "ПКМ", "О кадрах", date(2026, 10, 28), None, None, None,
                   "lex_search")  # fmt: skip
    repo.upsert_legal_act(conn, act)
    act = repo.legal_act(conn, "PKM-594-2026")
    text = "Постановлением Кабинета Министров от 28.10.2026 г. № 594 доля 50 % или более."
    jobs = [_job(conn, 1, text, doc_key="PKM-594-2026", act=act), _job(conn, 2, text)]

    first = jobs[0].cluster_id

    def answer(payload):
        base = _default_summary(payload)
        if payload["cluster_id"] == first:  # акт назван, факты из источника
            return {
                **base,
                "details": "ПКМ № 594 от 28.10.2026: доля 50 %.",
                "legal_refs": [{"kind": "PKM", "number": "№ 594", "date": "28.10.2026"}],
            }
        return {**base, "details": "Штраф 2 млн сумов с 1 декабря."}  # выдумано  # fmt: skip

    fake = FakeClaude(summary_answer=answer)
    written = summarize(conn, LlmBudget(LLM, ask_fn=fake), jobs, LLM)
    assert written[jobs[0].cluster_id].flags == []
    assert written[jobs[1].cluster_id].flags == [
        "факт не из источника: 2 млн",
        "факт не из источника: 1 декабря",
    ]

    mismatch = FakeClaude(summary_answer=lambda p: {**_default_summary(p), "act_matches": False})
    other = _job(conn, 3, text, doc_key="PKM-594-2026", act=act)
    flags = summarize(conn, LlmBudget(LLM, ask_fn=mismatch), [other], LLM)[other.cluster_id].flags
    assert flags == [
        "в тексте не назван акт PKM-594-2026",
        "реквизиты акта на lex.uz не совпадают с темой новости",
    ]

    again = FakeClaude()
    assert summarize(conn, LlmBudget(LLM, ask_fn=again), jobs, LLM) == written
    assert again.calls == []
    changed = [Job(jobs[1].cluster_id, cand(jobs[1].primary.id, jobs[1].primary.title, text + " Дополнено."),
                   [], None, None, "legislation", 4)]  # fmt: skip
    summarize(conn, LlmBudget(LLM, ask_fn=again), changed, LLM)
    assert again.calls == ["SummarizeResponse"]  # текст материала изменился — кэш устарел


def test_long_text_is_cut_at_paragraph_with_mark():
    text = "Первый абзац.\n" + "а" * 50 + "\nТретий абзац " + "б" * 50
    cut = cut_text(text, 80)
    assert cut == "Первый абзац.\n" + "а" * 50 + f"\n{CUT_MARK}"
    assert cut_text("Короткий", 80) == "Короткий"


def test_translation_checks_numbers_and_uses_cache():
    conn, _ = _db_with_items(1)
    jobs = [_job(conn, 1, "Ставка 14 % с 1 ноября 2026 года.")]
    russian = summarize(conn, LlmBudget(LLM, ask_fn=FakeClaude(summary_answer=lambda p: {
        **_default_summary(p), "details": "Ставка 14 % с 1 ноября 2026 года."})), jobs, LLM)  # fmt: skip

    def answer(payload, lang):
        details = "The rate is 15% from 1 November 2026." if lang == "en" else "1-noyabrdan stavka 14 foiz."
        return {**payload, "details": details}

    fake = FakeClaude(translation_answer=answer)
    result = translate(conn, LlmBudget(LLM, ask_fn=fake), jobs, russian, LLM, Glossary.load())
    assert fake.calls == ["TranslateResponse", "TranslateResponse"]
    cluster_id = jobs[0].cluster_id
    assert result["en"][cluster_id].flags == ["en: нет 14", "en: лишнее 15"]
    assert result["uz"][cluster_id].flags == []
    again = FakeClaude()
    assert translate(conn, LlmBudget(LLM, ask_fn=again), jobs, russian, LLM, Glossary.load()) == result
    assert again.calls == []


# --- Окно целиком ---

IMARAT = "Против застройщика Imarat Development возбудили дело о мошенничестве на 120 млрд сумов"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        timezone="Asia/Tashkent", db_path=tmp_path / "db.sqlite3", outputs_dir=tmp_path, logs_dir=tmp_path,
        http=HttpSettings(user_agent="test"), llm=LLM,
    )  # fmt: skip


def _window_db():
    sources = [
        Source(code="kun", name="kun", kind="rss", url="https://kun.uz/rss", tier="C"),
        Source(code="daryo", name="daryo", kind="rss", url="https://daryo.uz/rss", tier="C"),
        Source(code="soliq", name="soliq", kind="rss", url="https://soliq.uz/rss", tier="A"),
    ]
    rows = [
        ("kun", RawItem("https://kun.uz/1", IMARAT, None, f"{IMARAT}. Дольщики пострадали.", None, T0)),
        ("daryo", RawItem("https://daryo.uz/1", IMARAT, None, f"{IMARAT}. Ведётся следствие.", None,
                          T0 + timedelta(hours=1))),
        ("daryo", RawItem("https://daryo.uz/2", "Хусанов отыграл весь матч за Манчестер Сити", None,
                          "Матч завершился поражением.", None, T0)),
        ("soliq", RawItem("https://soliq.uz/1", "Возврат НДС сельхозпроизводителям", None,
                          "В соответствии с Указом Президента от 4.09.2025 г. № УП-153 возврат НДС "
                          "производится автоматически.", None, T0)),
        ("kun", RawItem("https://kun.uz/2", "В Самарканде отремонтировали дорогу к вокзалу", None,
                        "Работы завершены досрочно.", None, T0)),
    ]  # fmt: skip
    conn = repo.connect(":memory:")
    ids = repo.sync_sources(conn, sources)
    run_id = repo.start_run(conn, "collect")
    with conn:
        for code, raw in rows:
            repo.insert_item(conn, ids[code], run_id, to_item(raw, "ru", T0 + timedelta(hours=10)))
    window = prepare_window(conn, sources, Keywords.load(), Settings.model_fields["dedup"].default_factory(),
                            T0 - timedelta(hours=1), T0 + timedelta(hours=9))  # fmt: skip
    with conn:
        conn.execute("UPDATE item_legal_refs SET verify_status = 'verified'")  # этап 4
    return conn, window


def test_window_end_to_end_with_cache_and_fallback(tmp_path):
    settings = _settings(tmp_path)
    conn, window = _window_db()
    road = next(c.id for c in window.candidates if "дорогу" in c.title)
    fake = FakeClaude(triage_answer=lambda item: (item["id"] != road, 4))
    outcome = process_window(conn, settings, window, LlmBudget(LLM, ask_fn=fake), T0 + timedelta(hours=10))
    assert fake.calls == ["TriageResponse", "SummarizeResponse", "TranslateResponse", "TranslateResponse"]
    titles = {a.primary.title: a for a in outcome.articles}
    assert set(titles) == {IMARAT, "Возврат НДС сельхозпроизводителям"}
    vat = titles["Возврат НДС сельхозпроизводителям"]
    assert (vat.doc_key, vat.doc_status, vat.by_llm) == ("UP-153-2025", "verified", True)
    assert len(titles[IMARAT].others) == 1  # два издания об одном деле — один сюжет
    assert all(a.texts["en"] and a.texts["uz"] for a in outcome.articles)
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 6

    again = FakeClaude()
    process_window(conn, settings, window, LlmBudget(LLM, ask_fn=again), T0 + timedelta(hours=11))
    assert again.calls == []  # пересборка того же дня лимит не тратит

    conn, window = _window_db()
    no_llm = LlmBudget(LLM.model_copy(update={"max_calls_per_run": 0}), ask_fn=FakeClaude())
    outcome = process_window(conn, settings, window, no_llm, T0 + timedelta(hours=10))
    assert [a.primary.title for a in outcome.articles] == ["Возврат НДС сельхозпроизводителям"]
    [vat] = outcome.articles
    assert not vat.by_llm and vat.texts["en"] is None and vat.texts["uz"] is None
    assert vat.texts["ru"].headline == "Возврат НДС сельхозпроизводителям"
    assert vat.texts["ru"].summary.startswith("В соответствии с Указом Президента")


def _db_with_items(n):
    """БД с n материалами источника soliq; возвращает id материалов."""
    conn = repo.connect(":memory:")
    source_id = repo.sync_sources(conn, [Source(code="soliq", name="soliq", kind="rss", url="https://soliq.uz/rss",
                                                tier="A")])["soliq"]  # fmt: skip
    run_id = repo.start_run(conn, "collect")
    with conn:
        for i in range(n):
            raw = RawItem(f"https://soliq.uz/{i}", f"Новость {i}", None, "Текст", None, T0)
            repo.insert_item(conn, source_id, run_id, to_item(raw, "ru", T0))
    return conn, [row[0] for row in conn.execute("SELECT id FROM items ORDER BY id")]


def test_select_limits_context_sections_but_never_taxes_or_acts():
    """Финансы, бизнес и макро — фон: не больше лимита раздела, важные первыми. Налоги и акты без лимита."""
    clusters = [Cluster([n], "news", None) for n in range(1, 7)] + [Cluster([7], "legal", "PKM-1-2026")]
    verdicts = {
        1: Verdict(True, "finance", 3, "", "llm"), 2: Verdict(True, "finance", 5, "", "llm"),
        3: Verdict(True, "finance", 4, "", "llm"), 4: Verdict(True, "taxes", 3, "", "llm"),
        5: Verdict(True, "taxes", 3, "", "llm"), 6: Verdict(True, "taxes", 3, "", "llm"),
        7: Verdict(True, "finance", 3, "", "llm"),
    }  # fmt: skip
    settings = (
        Settings.model_fields["digest"]
        .default_factory()
        .model_copy(update={"section_limits": {"finance": 2, "taxes": 9}})
    )
    chosen = select(clusters, verdicts, {"PKM-1-2026": "verified"}, settings)
    assert chosen == [6, 1, 2, 3, 4, 5]  # акт вне лимита; из финансов две важнейшие


def test_digest_does_not_fill_up_to_the_limit_when_news_are_few():
    clusters = [Cluster([1], "news", None), Cluster([2], "news", None)]
    verdicts = {1: Verdict(True, "taxes", 3, "", "llm"), 2: Verdict(True, "business", 2, "", "llm")}
    settings = Settings.model_fields["digest"].default_factory()
    assert select(clusters, verdicts, {}, settings) == [0]


def test_triage_prompt_keeps_context_news_below_the_digest_bar():
    text = load_prompt("triage_v4").text
    importance = text.split("## Важность")[1].split("## Пары")[0]
    assert "отчётность и прибыль отдельных компаний" in importance
    assert "инвестпроекты" in importance and "2 —" in importance
    assert "Crowe" in text
