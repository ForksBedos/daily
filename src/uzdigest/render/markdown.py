"""Файлы дайджеста: digest.{ru,en,uz}.md и digest.json в папке дня (docs/plan/stage-6-render.md).

Один шаблон templates/digest.md.j2 на три языка, подписи — из config/i18n.yaml. Шаблон только
раскладывает готовые строки; числа, даты, строки «Документ» и «Источник» форматируются здесь.
Файлы сначала целиком собираются в памяти: ошибка рендера не трогает прежнюю папку дня.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import BaseModel, model_validator

from uzdigest.config import CONFIG_DIR
from uzdigest.legal.doc_key import COURT, MJ, PKM, PP, RP, SENATE, UP, ZRU
from uzdigest.llm.schemas import CATEGORIES
from uzdigest.render.model import LANGS, Article, Digest, Doc, SourceRef, Text, Update
from uzdigest.timeutil import TASHKENT

ACT_TYPES = (ZRU, UP, PP, RP, PKM, MJ, SENATE, COURT)
STATUSES = ("verified", "pending", "draft", "not_found")
NO_VALUE = "—"
MAX_ERROR_CHARS = 80


class Labels(BaseModel):
    """Подписи одного языка из config/i18n.yaml."""

    title: str
    months: list[str]
    long_date: str
    short_date: str
    decimal: str
    thousands: str
    rates: str
    categories: dict[str, str]
    summary: str
    details: str
    document: str
    source: str
    also: str
    no_llm: str
    no_translation: str
    check: str
    check_hint: str
    effective: str
    link_lang: dict[str, str]
    statuses: dict[str, str]
    acts: dict[str, str]
    act_date: str
    updates: str
    update: str
    not_responded: str
    all_responded: str
    stale: str
    no_llm_count: str
    no_translation_count: str

    @model_validator(mode="after")
    def _complete(self) -> Labels:
        # Пропущенная подпись видна при загрузке, а не дырой в утреннем дайджесте
        if len(self.months) != 12:
            raise ValueError("months: нужно 12 названий месяцев")
        for name, keys in (("categories", CATEGORIES), ("statuses", STATUSES), ("acts", ACT_TYPES)):
            missing = set(keys) - set(getattr(self, name))
            if missing:
                raise ValueError(f"{name}: нет {', '.join(sorted(missing))}")
        return self


def load_labels(path: Path = CONFIG_DIR / "i18n.yaml") -> dict[str, Labels]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [lang for lang in LANGS if lang not in data]
    if missing:
        raise ValueError(f"В {path.name} нет языков: {', '.join(missing)}")
    return {lang: Labels.model_validate(data[lang]) for lang in LANGS}


_ENV = Environment(
    loader=PackageLoader("uzdigest.render", "templates"),
    autoescape=False,  # Markdown, не HTML
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def render_files(digest: Digest, labels: dict[str, Labels]) -> dict[str, str]:
    """Содержимое всех файлов папки дня: имя → текст."""
    files = {f"digest.{lang}.md": render_markdown(digest, lang, labels[lang]) for lang in LANGS}
    files["digest.json"] = digest.to_json() + "\n"
    return files


def write_files(out_dir: Path, files: dict[str, str]) -> list[Path]:
    """Пишет файлы в папку дня так, что читатель общей папки не увидит половину файла.

    Каждый файл сначала целиком пишется во временный рядом с ним, затем одним переименованием
    (os.replace) заменяет прежний. Временные файлы при сбое убираются.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for name, content in files.items():
            temp = out_dir / f".{name}.tmp"
            temp.write_text(content, encoding="utf-8", newline="\n")
            staged.append((temp, out_dir / name))
        for temp, target in staged:
            os.replace(temp, target)
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)
    return [target for _, target in staged]


def render_markdown(digest: Digest, lang: str, t: Labels) -> str:
    fmt = _Format(t)
    sections = []
    for category in [*CATEGORIES, *sorted({a.category for a in digest.articles} - set(CATEGORIES))]:
        articles = [_article(a, lang, t, fmt) for a in digest.articles if a.category == category]
        if articles:
            sections.append({"name": t.categories.get(category, category), "articles": articles})
    since, until = (
        m.astimezone(TASHKENT).isoformat(timespec="minutes") for m in digest.window.model_dump().values()
    )
    generator = digest.generator
    return _ENV.get_template("digest.md.j2").render(
        t=t,
        lang=lang,
        date=digest.date.isoformat(),
        window=f"{since}/{until}",
        items=len(digest.articles),
        sources_ok=f"{digest.sources_ok}/{digest.sources_total}",
        generator=f"uzdigest {generator.version}, {', '.join(generator.prompts)}, {generator.model}",
        title=t.title.format(date=fmt.long_date(digest.date)),
        rates=_rates(digest, t, fmt),
        sections=sections,
        updates=[_update(u, lang, t, fmt) for u in digest.updates],
        footer=_footer(digest, lang, t, fmt),
    )


def article_fields(article: Article, lang: str, t: Labels) -> dict[str, Any]:
    """Готовые строки статьи, как в файле: карточка темы в боте. Ссылки — Markdown [имя](адрес)."""
    return _article(article, lang, t, _Format(t))


def rates_line(digest: Digest, t: Labels) -> str | None:
    """«Курсы ЦБ на 31.10.2026: USD 12 023,46 (−42,76) · …»; None — курсов нет."""
    return _rates(digest, t, _Format(t))


def _article(article: Article, lang: str, t: Labels, fmt: _Format) -> dict[str, Any]:
    text: Text = article.texts.get(lang) or article.texts["ru"]
    notes = []
    if not article.by_llm:
        notes.append(t.no_llm)
    elif article.texts.get(lang) is None:
        notes.append(t.no_translation)
    check = None
    if article.flags:
        # Замечания проверки — на русском: в других языках только метка со ссылкой на русскую версию
        check = f"{t.check}: {'; '.join(article.flags)}" if lang == "ru" else f"{t.check} ({t.check_hint})"
    return {
        "id": article.id,
        "doc_key": article.doc.doc_key if article.doc else None,
        "headline": text.headline,
        "notes": notes,
        "check": check,
        "summary": text.summary or NO_VALUE,
        "details": text.details or NO_VALUE,
        "document": _document(article.doc, lang, t, fmt) if article.doc else None,
        "source": f"{_link(article.source)} · {fmt.moment(article.source)}",
        "also": " · ".join(_link(ref) for ref in article.also) or NO_VALUE,
    }


def _link(ref: SourceRef) -> str:
    return f"[{ref.name}]({ref.url})"


def _act(doc: Doc, t: Labels, fmt: _Format) -> str:
    template = t.acts.get(doc.doc_type)
    name = template.format(number=doc.number) if template else doc.doc_key
    return t.act_date.format(act=name, date=fmt.short_date(doc.adopted_on)) if doc.adopted_on else name


def _lex_link(doc: Doc, lang: str, t: Labels) -> str | None:
    link = doc.lex.get(lang)
    if link is None:
        return None
    note = f" ({t.link_lang.get(link.lang, link.lang)})" if link.lang != lang else ""
    return f"[lex.uz]({link.url}){note}"


def _document(doc: Doc, lang: str, t: Labels, fmt: _Format) -> str:
    """«Указ Президента № УП-236 от 28.10.2026 — ✅ lex.uz, дата вступления в силу: 30.10.2026»."""
    status = t.statuses.get(doc.status, "") if doc.status else ""
    link = _lex_link(doc, lang, t) if doc.status == "verified" else None
    mark = " ".join(part for part in (status, link) if part)
    line = f"{_act(doc, t, fmt)} — {mark}" if mark else _act(doc, t, fmt)
    if doc.effective_on:
        line += ", " + t.effective.format(date=fmt.short_date(doc.effective_on))
    return line


def _update(update: Update, lang: str, t: Labels, fmt: _Format) -> str:
    return t.update.format(
        act=_act(update.doc, t, fmt),
        link=_lex_link(update.doc, lang, t) or "lex.uz",
        date=fmt.short_date(update.previous_date),
        headline=update.headlines.get(lang) or update.headlines["ru"],
    )


def _rates(digest: Digest, t: Labels, fmt: _Format) -> str | None:
    if not digest.rates:
        return None
    parts = []
    for rate in digest.rates:
        ccy = rate.ccy if rate.nominal == 1 else f"{rate.nominal} {rate.ccy}"
        diff = f" ({fmt.number(rate.diff, signed=True)})" if rate.diff is not None else ""
        parts.append(f"{ccy} {fmt.number(rate.rate)}{diff}")
    return f"{t.rates.format(date=fmt.short_date(digest.rates[0].date))}: {' · '.join(parts)}"


def _footer(digest: Digest, lang: str, t: Labels, fmt: _Format) -> str:
    failures = digest.failures
    if failures.sources:
        reasons = []
        for source in failures.sources:
            if source.error:
                reason = " ".join(source.error.split())[:MAX_ERROR_CHARS]
            elif source.last_ok_at:
                reason = t.stale.format(date=fmt.moment_at(source.last_ok_at))
            else:
                reason = NO_VALUE
            reasons.append(f"{source.name} ({reason})")
        parts = [f"{t.not_responded}: {', '.join(reasons)}"]
    else:
        parts = [t.all_responded]
    parts.append(f"{t.no_llm_count}: {failures.no_llm}")
    if lang != "ru":
        parts.append(f"{t.no_translation_count}: {sum(a.texts.get(lang) is None for a in digest.articles)}")
    return ". ".join(parts) + "."


class _Format:
    """Даты и числа по правилам языка."""

    def __init__(self, labels: Labels) -> None:
        self.t = labels

    def long_date(self, day: date) -> str:
        return self.t.long_date.format(day=day.day, month=self.t.months[day.month - 1], year=day.year)

    def short_date(self, day: date) -> str:
        return self.t.short_date.format(
            day=day.day, dd=f"{day.day:02d}", mm=f"{day.month:02d}", month=self.t.months[day.month - 1],
            year=day.year,
        )  # fmt: skip

    def moment_at(self, moment: datetime) -> str:
        local = moment.astimezone(TASHKENT)
        return f"{self.short_date(local.date())} {local:%H:%M}"

    def moment(self, ref: SourceRef) -> str:
        """Время публикации по Ташкенту; только дата, если источник времени не знает."""
        if ref.time_known:
            return self.moment_at(ref.published_at)
        return self.short_date(ref.published_at.astimezone(TASHKENT).date())

    def number(self, value: float, *, signed: bool = False) -> str:
        """12 023,46 · 12,023.46; со знаком: +17,29 и −42,76 (минус U+2212)."""
        digits = f"{abs(value):,.2f}".replace(",", "\0").replace(".", self.t.decimal)
        digits = digits.replace("\0", self.t.thousands)
        sign = "−" if value < 0 else "+" if signed and value > 0 else ""
        return sign + digits
