"""Этап 5 для окна дайджеста: отбор → кластеры с решениями Claude → выбор сюжетов → тексты → перевод.

Вызывается после prepare_window (этап 3) и сверки с lex.uz (этап 4). Кластеры пересобираются
с решениями Claude по серой зоне и записываются в БД: от их id зависит кэш текстов (summaries).
Если Claude недоступен, сюжет получает резервный текст из анонса, а перевод остаётся пустым.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from uzdigest.config import DigestSettings, Settings
from uzdigest.db import repo
from uzdigest.dedup.clustering import (
    Candidate,
    Cluster,
    ClusterResult,
    Repeat,
    build_clusters,
    find_repeats,
    save_clusters,
)
from uzdigest.dedup.stage import WindowResult
from uzdigest.llm.client import LlmBudget
from uzdigest.llm.summarize import Job, Text, summarize
from uzdigest.llm.translate import LANGS, Glossary, translate
from uzdigest.llm.triage import Verdict, triage
from uzdigest.timeutil import tashkent_date

# Статус сверки акта в окне: лучший из статусов его упоминаний
_STATUS_ORDER = ("verified", "pending", "draft", "not_found")
# Подходящие бизнесу правовые сюжеты с этими статусами входят в дайджест при любой важности
_LEGAL_ALWAYS = {"verified", "pending"}
FALLBACK_SUMMARY_CHARS = 300


@dataclass
class Article:
    cluster_id: int
    primary: Candidate
    others: list[Candidate]
    category: str
    importance: int
    doc_key: str | None
    doc_status: str | None
    texts: dict[
        str, Text | None
    ]  # ru есть всегда (от Claude или резервный), en и uz — None, если перевода нет
    by_llm: bool  # русский текст написал Claude; False — «без ИИ-обработки»
    flags: list[str]  # замечания проверки фактов: «⚠️ проверить»


@dataclass
class LlmOutcome:
    articles: list[Article]
    result: ClusterResult  # кластеры окна с решениями Claude по серой зоне
    cluster_ids: list[int]  # id кластеров в порядке result.clusters
    repeats: dict[int, Repeat]  # уже выходили в прошлых дайджестах (индекс в result.clusters)
    verdicts: dict[int, Verdict]
    statuses: dict[str, str]  # doc_key → статус сверки
    candidates: dict[int, Candidate]  # материалы окна по id


def process_window(
    conn: sqlite3.Connection,
    settings: Settings,
    window: WindowResult,
    budget: LlmBudget,
    now: datetime,
    glossary: Glossary | None = None,
) -> LlmOutcome:
    candidates = window.candidates
    verdicts = triage(conn, budget, candidates, window.result.gray, settings.llm.triage_batch)
    decisions = repo.pair_decisions(conn, [c.id for c in candidates])
    result = build_clusters(candidates, settings.dedup, decisions)
    cluster_ids = save_clusters(conn, result, list(window.filtered), now)
    repeats = find_repeats(conn, result, candidates, settings.dedup, tashkent_date(now))
    statuses = {
        key: next(s for s in _STATUS_ORDER if s in found)
        for key, found in repo.doc_statuses(conn, [c.id for c in candidates]).items()
    }

    by_id = {c.id: c for c in candidates}
    jobs = []
    for index in select(result.clusters, verdicts, statuses, settings.digest, skip=set(repeats)):
        cluster = result.clusters[index]
        verdict = cluster_verdict(cluster, verdicts)
        jobs.append(
            Job(
                cluster_id=cluster_ids[index],
                primary=by_id[cluster.primary_id],
                others=[by_id[i] for i in cluster.item_ids[1:]],
                doc_key=cluster.doc_key,
                act=repo.legal_act(conn, cluster.doc_key) if cluster.doc_key else None,
                category=verdict.category or ("legislation" if cluster.doc_key else "business"),
                importance=verdict.importance,
            )
        )

    russian = summarize(conn, budget, jobs, settings.llm)
    translations = translate(conn, budget, jobs, russian, settings.llm, glossary or Glossary.load())
    articles = []
    for job in jobs:
        written = russian.get(job.cluster_id)
        texts: dict[str, Text | None] = {"ru": written.text if written else fallback_text(job.primary)}
        flags = list(written.flags) if written else []
        for lang in LANGS:
            translated = translations[lang].get(job.cluster_id)
            texts[lang] = translated.text if translated else None
            flags += translated.flags if translated else []
        articles.append(
            Article(
                cluster_id=job.cluster_id,
                primary=job.primary,
                others=job.others,
                category=job.category or "business",
                importance=job.importance or 1,
                doc_key=job.doc_key,
                doc_status=statuses.get(job.doc_key) if job.doc_key else None,
                texts=texts,
                by_llm=written is not None,
                flags=flags,
            )
        )
    return LlmOutcome(articles, result, cluster_ids, repeats, verdicts, statuses, by_id)


def cluster_verdict(cluster: Cluster, verdicts: dict[int, Verdict]) -> Verdict:
    """Оценка сюжета: самая важная из оценок его материалов, признанных подходящими.

    При равной важности — оценка более раннего материала (главный идёт первым).
    """
    members = [verdicts[i] for i in cluster.item_ids if i in verdicts]
    relevant = [v for v in members if v.relevant]
    if not relevant:
        return members[0] if members else Verdict(False, None, 1, "", "fallback")
    return max(relevant, key=lambda v: v.importance)


def select(
    clusters: list[Cluster],
    verdicts: dict[int, Verdict],
    statuses: dict[str, str],
    settings: DigestSettings,
    skip: set[int] | None = None,
) -> list[int]:
    """Сюжеты дайджеста (индексы в clusters): подходящие бизнесу правовые — всегда, остальные — по важности.

    Закон, который Claude признал не для бизнеса (пенсии, помощь населению, правила дорожного
    движения), в дайджест не идёт: правовой статус сам по себе ничего не решает.
    """
    legal, others = [], []
    for index, cluster in enumerate(clusters):
        if skip and index in skip:
            continue
        verdict = cluster_verdict(cluster, verdicts)
        if not verdict.relevant:
            continue
        if cluster.doc_key and statuses.get(cluster.doc_key) in _LEGAL_ALWAYS:
            legal.append(index)
        elif verdict.importance >= settings.min_importance:
            others.append(index)
    # Важнее — выше; при равной важности — сюжет, о котором написало больше изданий
    others.sort(
        key=lambda i: (-cluster_verdict(clusters[i], verdicts).importance, -len(clusters[i].item_ids))
    )
    taken: Counter[str] = Counter()
    kept = []
    for index in others:
        category = cluster_verdict(clusters[index], verdicts).category or "business"
        if taken[category] < settings.section_limits.get(category, settings.max_articles):
            taken[category] += 1
            kept.append(index)
    return legal + kept[: max(0, settings.max_articles - len(legal))]


def fallback_text(primary: Candidate) -> Text:
    """Резервный текст без Claude: заголовок и начало текста материала, обрезанное по предложению."""
    text = " ".join((primary.text or "").split())
    summary = text[:FALLBACK_SUMMARY_CHARS]
    if len(text) > FALLBACK_SUMMARY_CHARS:
        sentence_end = max((m.end() for m in re.finditer(r"[.!?…](?=\s)", summary)), default=0)
        summary = (
            summary[:sentence_end] if sentence_end > FALLBACK_SUMMARY_CHARS // 3 else f"{summary.rstrip()}…"
        )
    return Text(primary.title, summary, "")
