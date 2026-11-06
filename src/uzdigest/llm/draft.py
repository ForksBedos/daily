"""Черновик дайджеста для ручной проверки текстов Claude (docs/plan/stage-5-llm.md, «Оценка качества»).

Не формат дайджеста (его делает этап 6): по каждому сюжету — тексты на трёх языках, источник
для сверки фактов, оценка отбора и замечания проверки. В конце — подходящие сюжеты, не вошедшие в лимит.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from uzdigest.llm.schemas import CATEGORIES
from uzdigest.llm.stage import LlmOutcome, cluster_verdict
from uzdigest.timeutil import TASHKENT

CATEGORY_NAMES = {
    "legislation": "Законодательство",
    "taxes": "Налоги",
    "accounting": "Бухучёт и отчётность",
    "finance": "Финансы и банки",
    "business": "Бизнес и инвестиции",
    "macro": "Макроэкономика",
}
LANG_NAMES = {"ru": "RU", "en": "EN", "uz": "UZ"}


def write_draft(path: Path, outcome: LlmOutcome, header: dict[str, Any]) -> Path:
    lines = [f"# Черновик дайджеста — {header['date']}", ""]
    lines += [f"- {key}: {value}" for key, value in header.items() if key != "date"]
    lines.append("")
    order = {category: n for n, category in enumerate(CATEGORIES)}
    articles = sorted(outcome.articles, key=lambda a: (order.get(a.category, 99), -a.importance))
    for n, article in enumerate(articles, 1):
        ru = article.texts["ru"]
        lines += [f"## {n}. [{CATEGORY_NAMES.get(article.category, article.category)}] {ru.headline}", ""]
        published = article.primary.published_at.astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M")
        meta = [
            f"важность {article.importance}",
            f"кластер {article.cluster_id}",
            f"источник: [{article.primary.source}]({article.primary.url}) · {published}",
        ]
        if article.doc_key:
            meta.append(f"документ: {article.doc_key} — {article.doc_status}")
        if article.others:
            meta.append("также: " + " · ".join(f"[{c.source}]({c.url})" for c in article.others))
        lines += [" · ".join(meta), ""]
        if not article.by_llm:
            lines += ["**Без ИИ-обработки.**", ""]
        if article.flags:
            lines += ["⚠️ **Проверить:** " + "; ".join(article.flags), ""]
        for lang, text in article.texts.items():
            if text is None:
                lines += [f"**{LANG_NAMES[lang]}:** перевод не готов", ""]
                continue
            lines += [f"**{LANG_NAMES[lang]}. {text.headline}**", "", text.summary, ""]
            if text.details:
                lines += [text.details, ""]
        lines += [f"<details><summary>Исходный текст ({article.primary.source})</summary>", ""]
        lines += [
            f"**{article.primary.title}**",
            "",
            article.primary.text or "",
            "",
            "</details>",
            "",
            "---",
            "",
        ]

    chosen = {a.cluster_id for a in outcome.articles}
    skipped = []
    for index, cluster in enumerate(outcome.result.clusters):
        verdict = cluster_verdict(cluster, outcome.verdicts)
        if outcome.cluster_ids[index] not in chosen and verdict.relevant:
            skipped.append((verdict, cluster, index in outcome.repeats))
    if skipped:
        lines += ["## Подходящие, но не вошедшие", ""]
        for verdict, cluster, repeat in sorted(skipped, key=lambda s: -s[0].importance):
            note = " (уже был в дайджесте)" if repeat else ""
            title = outcome.candidates[cluster.primary_id].title
            lines.append(f"- {verdict.importance} · {verdict.category} · {title}{note} — {verdict.reason}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
