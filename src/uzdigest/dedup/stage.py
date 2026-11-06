"""Этап 3 для окна дайджеста: префильтр → правовые ключи → кластеры.

Результаты префильтра и упоминания актов пишутся в items и item_legal_refs: это производные
данные, повторный расчёт их просто обновляет. Кластеры здесь только строятся; в БД их пишет
сборка дайджеста (clustering.save_clusters), чтобы отчёт не менял состояние дайджестов.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from uzdigest.config import DedupSettings, Source
from uzdigest.db import repo
from uzdigest.dedup.clustering import Candidate, ClusterResult, build_clusters
from uzdigest.filter.prefilter import Keywords, prefilter
from uzdigest.legal.doc_key import find_refs
from uzdigest.timeutil import from_iso, tashkent_date


@dataclass
class WindowResult:
    candidates: list[Candidate]  # прошли префильтр
    filtered: dict[int, str]  # id → причина
    result: ClusterResult


def prepare_window(
    conn: sqlite3.Connection,
    sources: list[Source],
    keywords: Keywords,
    settings: DedupSettings,
    since: datetime,
    until: datetime,
    *,
    keep_pairs_from: float | None = None,
    late_from: datetime | None = None,
) -> WindowResult:
    by_code = {s.code: s for s in sources if s.enabled}
    candidates: list[Candidate] = []
    filtered: dict[int, str] = {}
    with conn:
        for row in repo.window_items(conn, since, until, late_from):
            source = by_code.get(row["source"])
            if source is None:
                continue  # источник выключен или убран из реестра
            published_at = from_iso(row["published_at"])
            verdict = prefilter(
                title=row["title"],
                text=row["text"],
                url=row["url"],
                categories=json.loads(row["categories"] or "[]"),
                source=source,
                keywords=keywords,
            )
            refs = find_refs(f"{row['title']}\n{row['text'] or ''}", tashkent_date(published_at).year)
            repo.save_prefilter(conn, row["id"], verdict.passed, verdict.reason)
            repo.save_legal_refs(conn, row["id"], refs)
            if not verdict.passed:
                filtered[row["id"]] = verdict.reason
                continue
            candidates.append(
                Candidate(
                    id=row["id"],
                    source=row["source"],
                    tier=row["tier"],
                    title=row["title"],
                    text=row["text"],
                    url=row["url"],
                    published_at=published_at,
                    refs=tuple(refs),
                )
            )
    decisions = repo.pair_decisions(conn, [c.id for c in candidates])
    result = build_clusters(candidates, settings, decisions, keep_from=keep_pairs_from)
    return WindowResult(candidates, filtered, result)
