"""Файлы дайджеста (этап 6): снимок, порядок полей, ссылки lex.uz по языкам, атомарная запись."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from uzdigest.render.markdown import Labels, load_labels, render_files, render_markdown, write_files
from uzdigest.render.model import (
    Article,
    Digest,
    Doc,
    Failures,
    Generator,
    LexLink,
    Rate,
    SourceFailure,
    SourceRef,
    Text,
    Update,
    Window,
)

LABELS = load_labels()
LEX = "https://lex.uz"


def _text(prefix: str, details: str = "Подробности.") -> Text:
    return Text(headline=f"{prefix}: заголовок", summary=f"{prefix}: краткое содержание.", details=details)


def sample_digest() -> Digest:
    """Дайджест со всеми случаями: подтверждённый и ожидающий акт, нет перевода, нет ИИ, обновление, сбой."""
    up236 = Doc(
        doc_key="UP-236-2026", doc_type="UP", number="236", adopted_on=date(2026, 10, 28),
        effective_on=date(2026, 10, 30), status="verified",
        lex={"ru": LexLink(url=f"{LEX}/ru/docs/8563561", lang="ru"),
             "uz": LexLink(url=f"{LEX}/uz/docs/-8563559", lang="uz"),
             "en": LexLink(url=f"{LEX}/ru/docs/8563561", lang="ru")},
    )  # fmt: skip
    pkm594 = Doc(
        doc_key="PKM-594-2026",
        doc_type="PKM",
        number="594",
        adopted_on=None,
        effective_on=None,
        status="pending",
    )
    pp387 = Doc(
        doc_key="PP-387-2026", doc_type="PP", number="387", adopted_on=date(2026, 10, 27), effective_on=None,
        status="verified",
        lex={"ru": LexLink(url=f"{LEX}/ru/docs/8560001", lang="ru"),
             "uz": LexLink(url=f"{LEX}/uz/docs/8560002", lang="uz"),
             "en": LexLink(url=f"{LEX}/en/docs/8560003", lang="en")},
    )  # fmt: skip
    at = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)  # 10:00 по Ташкенту

    def ref(code, name, url, **kwargs):
        return SourceRef(code=code, name=name, url=url, published_at=at, **kwargs)

    texts = {"ru": _text("RU"), "en": _text("EN"), "uz": _text("UZ")}
    since, until = datetime(2026, 10, 30, 4, 30, tzinfo=UTC), datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
    return Digest(
        date=date(2026, 11, 2),
        window=Window(since=since, until=until),
        generator=Generator(version="0.1.0", model="claude-sonnet-5-5",
                            prompts=["triage_v1", "summarize_v1", "translate_v1"]),
        sources_ok=16,
        sources_total=17,
        rates=[
            Rate(date=date(2026, 10, 31), ccy="USD", nominal=1, rate=12023.46, diff=-42.76),
            Rate(date=date(2026, 10, 31), ccy="EUR", nominal=1, rate=13461.47, diff=17.29),
            Rate(date=date(2026, 10, 31), ccy="RUB", nominal=1, rate=149.44, diff=None),
        ],
        articles=[
            Article(
                id="2026-11-02-001", cluster_id=32, category="legislation", importance=5, doc=up236,
                source=ref("norma", "norma.uz", "https://norma.uz/a1"),
                also=[ref("gazeta", "gazeta.uz", "https://gazeta.uz/a1")], texts=texts, by_llm=True, flags=[],
            ),
            Article(
                id="2026-11-02-002", cluster_id=40, category="legislation", importance=4, doc=pkm594,
                source=ref("nuz", "nuz.uz", "https://nuz.uz/a2"), also=[], texts=texts, by_llm=True, flags=[],
            ),
            Article(
                id="2026-11-02-003", cluster_id=41, category="taxes", importance=4, doc=None,
                source=ref("soliq", "gov.uz/soliq", "https://gov.uz/ru/soliq/news/view/3", time_known=False),
                also=[], texts={**texts, "en": None}, by_llm=True, flags=["факт не из источника: 15%"],
            ),
            Article(
                id="2026-11-02-004", cluster_id=42, category="finance", importance=3, doc=None,
                source=ref("kun", "kun.uz", "https://kun.uz/a4"), also=[],
                texts={"ru": _text("RU", details=""), "en": None, "uz": None}, by_llm=False, flags=[],
            ),
        ],
        updates=[
            Update(doc=pp387, previous_date=date(2026, 10, 30),
                   headlines={"ru": "Новые правила госзакупок", "en": "New procurement rules"}),
        ],
        failures=Failures(
            sources=[SourceFailure(code="uzdaily", name="uzdaily.uz", error="ConnectTimeout: timed out",
                                   last_ok_at=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))],
            no_llm=1,
            no_translation=2,
        ),
    )  # fmt: skip


# --- Снимок ---


@pytest.mark.parametrize("name", ["digest.ru.md", "digest.en.md", "digest.uz.md", "digest.json"])
def test_snapshot(name):
    """Эталоны в tests/fixtures/snapshot/: правка шаблона или подписей должна менять их осознанно."""
    files = render_files(sample_digest(), LABELS)
    assert files[name] == (FIXTURES / "snapshot" / name).read_text(encoding="utf-8")


def test_json_round_trip_and_aliases():
    digest = sample_digest()
    data = json.loads(digest.to_json())
    assert set(data["window"]) == {"from", "to"}
    assert data["articles"][2]["texts"]["en"] is None
    assert Digest.from_json(digest.to_json()) == digest


# --- Структура и подписи ---

FIELDS = {
    "ru": ["Краткое содержание", "Подробное содержание", "Источник", "Также писали"],
    "en": ["Summary", "Details", "Source", "Also reported by"],
    "uz": ["Qisqacha mazmuni", "Batafsil", "Manba", "Shuningdek yozdi"],
}


def _articles(markdown: str) -> list[tuple[str, str]]:
    """(id, текст статьи) по HTML-комментариям с id."""
    parts = re.split(r"<!-- id: (\S+)[^>]*-->", markdown.split("\n---\n\n")[-2])
    return list(zip(parts[1::2], parts[2::2], strict=True))


def test_every_article_has_four_fields_in_order_and_same_ids():
    files = render_files(sample_digest(), LABELS)
    ids = {}
    for lang, labels in FIELDS.items():
        articles = _articles(files[f"digest.{lang}.md"])
        ids[lang] = [article_id for article_id, _ in articles]
        for _, body in articles:
            positions = [body.index(f"**{label}:**") for label in labels]
            assert positions == sorted(positions), (lang, body)
    assert ids["ru"] == ids["en"] == ids["uz"] == [a.id for a in sample_digest().articles]


def test_lex_links_follow_file_language():
    files = render_files(sample_digest(), LABELS)
    ru, en, uz = (files[f"digest.{lang}.md"] for lang in ("ru", "en", "uz"))
    assert (
        "**Документ:** Указ Президента № УП-236 от 28.10.2026 — ✅ [lex.uz](https://lex.uz/ru/docs/8563561), "
        "дата вступления в силу: 30.10.2026" in ru
    )
    # Английской версии на lex.uz нет — русская ссылка с пометкой
    assert "— ✅ [lex.uz](https://lex.uz/ru/docs/8563561) (in Russian), effective date: 30 October 2026" in en
    assert (
        "Prezidentning PF-236-son Farmoni (28.10.2026) — ✅ [lex.uz](https://lex.uz/uz/docs/-8563559)," in uz
    )
    # Ожидающий акт — без ссылки и даты принятия
    assert "**Документ:** Постановление Кабинета Министров № 594 — ⏳ ожидает публикации на lex.uz" in ru


def test_missing_translation_no_llm_and_flags():
    files = render_files(sample_digest(), LABELS)
    en = dict(_articles(files["digest.en.md"]))
    ru = dict(_articles(files["digest.ru.md"]))
    # Нет английского перевода — русский текст с пометкой, порядок полей тот же
    assert "_Translation not ready: the Russian text is below._" in en["2026-11-02-003"]
    assert "**Summary:** RU: краткое содержание." in en["2026-11-02-003"]
    assert "**⚠️ Check (details are in the Russian version)**" in en["2026-11-02-003"]
    assert "**⚠️ Проверить: факт не из источника: 15%**" in ru["2026-11-02-003"]
    # Источник знает только дату — время не выводится
    assert (
        "**Источник:** [gov.uz/soliq](https://gov.uz/ru/soliq/news/view/3) · 02.11.2026\n"
        in ru["2026-11-02-003"]
    )
    # Без ИИ: пометка, подробного содержания нет
    assert "_Без ИИ-обработки: начало материала из источника._" in ru["2026-11-02-004"]
    assert "**Подробное содержание:**\n—\n" in ru["2026-11-02-004"]
    assert "_Translation not ready" not in en["2026-11-02-004"]  # не переводили то, чего Claude не писал


def test_numbers_dates_and_footer_by_language():
    digest = sample_digest()
    ru = render_markdown(digest, "ru", LABELS["ru"])
    en = render_markdown(digest, "en", LABELS["en"])
    uz = render_markdown(digest, "uz", LABELS["uz"])
    assert "# Бизнес, финансы, налоги — 2 ноября 2026" in ru
    assert "# Business, finance, taxes — 2 November 2026" in en
    assert "# Biznes, moliya, soliqlar — 2026-yil 2-noyabr" in uz
    assert "Курсы ЦБ на 31.10.2026: USD 12 023,46 (−42,76) · EUR 13 461,47 (+17,29) · RUB 149,44\n" in ru
    assert "USD 12,023.46 (−42.76)" in en
    assert "window: 2026-10-30T09:30+05:00/2026-11-02T09:30+05:00" in ru
    assert ru.rstrip().endswith("Не ответили: uzdaily.uz (ConnectTimeout: timed out). Без ИИ-обработки: 1.")
    assert en.rstrip().endswith("Not processed by AI: 1. Translation not ready: 2.")
    assert uz.rstrip().endswith("Tarjima tayyor emas: 1.")


def test_updates_block():
    files = render_files(sample_digest(), LABELS)
    assert ("- Постановление Президента № ПП-387 от 27.10.2026 — опубликован на lex.uz: "
            "[lex.uz](https://lex.uz/ru/docs/8560001). Ранее, в дайджесте за 30.10.2026, ожидал публикации: "
            "«Новые правила госзакупок»") in files["digest.ru.md"]  # fmt: skip
    # Узбекского заголовка не было — берётся русский
    assert "[lex.uz](https://lex.uz/uz/docs/8560002)" in files["digest.uz.md"]
    assert "“Новые правила госзакупок”" in files["digest.uz.md"]


def test_empty_sections_and_blocks_are_skipped():
    digest = sample_digest().model_copy(
        update={"articles": [], "updates": [], "rates": [],
                "failures": Failures(sources=[], no_llm=0, no_translation=0)}
    )  # fmt: skip
    ru = render_markdown(digest, "ru", LABELS["ru"])
    assert "## " not in ru and "Курсы ЦБ" not in ru
    assert "items: 0" in ru and "Все источники ответили. Без ИИ-обработки: 0." in ru


def test_labels_must_be_complete(tmp_path):
    data = {lang: labels.model_dump() for lang, labels in LABELS.items()}
    del data["uz"]["categories"]["macro"]
    with pytest.raises(ValidationError, match="macro"):
        Labels.model_validate(data["uz"])
    broken = tmp_path / "i18n.yaml"
    broken.write_text(json.dumps({"ru": data["ru"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="en, uz"):
        load_labels(broken)


# --- Атомарная запись ---


def test_render_error_leaves_previous_day_untouched(tmp_path):
    out = tmp_path / "2026-11-02"
    write_files(out, render_files(sample_digest(), LABELS))
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    broken = sample_digest().model_copy(update={"articles": [sample_digest().articles[0].model_copy(
        update={"texts": {"en": None, "uz": None}})]})  # fmt: skip
    with pytest.raises(KeyError):
        write_files(out, render_files(broken, LABELS))  # нет русского текста: рендер падает до записи
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_write_failure_removes_temp_files(tmp_path, monkeypatch):
    out = tmp_path / "2026-11-02"
    write_files(out, {"digest.ru.md": "старый"})
    original = Path.write_text

    def fail_on_json(self, *args, **kwargs):
        if self.name == ".digest.json.tmp":
            raise OSError("диск заполнен")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_on_json)
    with pytest.raises(OSError):
        write_files(out, {"digest.ru.md": "новый", "digest.json": "{}"})
    assert sorted(p.name for p in out.iterdir()) == ["digest.ru.md"]
    assert (out / "digest.ru.md").read_text(encoding="utf-8") == "старый"
