"""Кластеры сюжетов (docs/plan/stage-3-dedup.md, задачи 3.4 и 3.5).

Связи между материалами: высокая оценка похожести, общий правовой ключ и решения из
pair_decisions (Claude по серой зоне, разметка команды). Связи объединяются через union-find.
Главный материал кластера — самый ранний, при равенстве — источник выше уровнем (A > B > C).
Пары из серой зоны до этапа 5 считаются разными сюжетами.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from uzdigest.config import DedupSettings
from uzdigest.db import repo
from uzdigest.dedup.similarity import (
    Decision,
    Features,
    Fingerprint,
    compare,
    decide,
    fingerprint,
    rare_word_weights,
    text_overlap,
)
from uzdigest.legal.doc_key import LegalRef
from uzdigest.timeutil import to_iso

TIER_RANK = {"A": 0, "B": 1, "C": 2}
# По правовому ключу связываются только материалы об акте, а не упоминающие его к слову.
# На 28 материалах с реквизитами (30.10–02.11.2026) акт, о котором заметка, стоит в заголовке или
# первых ~600 знаках, а упоминания «к слову» — дальше 900 знаков или это акты прошлых лет
# («ранее порядок регулировался ПКМ № 318 от 2025 года»).
# Материал с большим числом актов — обзор («Что изменится с 1 ноября»): по ключу он не связывается,
# иначе склеил бы в один кластер все упомянутые в нём документы.
MAX_REFS_FOR_LINK = 3
LINK_POSITION = 700  # знаков от начала «заголовок + текст»
LINK_MAX_AGE_YEARS = 1


@dataclass(frozen=True)
class Candidate:
    id: int
    source: str
    tier: str
    title: str
    text: str | None  # полный текст или анонс
    url: str
    published_at: datetime
    refs: tuple[LegalRef, ...] = ()

    @property
    def doc_key(self) -> str | None:
        """Ключ акта, о котором материал: первое свежее упоминание в начале текста, не проект."""
        if len(self.refs) > MAX_REFS_FOR_LINK:
            return None
        min_year = self.published_at.year - LINK_MAX_AGE_YEARS
        return next(
            (
                r.doc_key
                for r in self.refs
                if not r.is_draft and r.position <= LINK_POSITION and (r.year is None or r.year >= min_year)
            ),
            None,
        )


@dataclass(frozen=True)
class Pair:
    a: int  # a < b
    b: int
    features: Features
    score: float
    decision: Decision  # по правилам; решение из pair_decisions — в override
    override: str | None = None

    @property
    def same(self) -> bool:
        return self.override == "same" if self.override else self.decision == "same"


@dataclass
class Cluster:
    item_ids: list[int]  # главный первым, остальные по времени
    kind: str  # news | legal
    doc_key: str | None

    @property
    def primary_id(self) -> int:
        return self.item_ids[0]


@dataclass
class ClusterResult:
    clusters: list[Cluster]
    pairs: list[Pair] = field(default_factory=list)  # пары с оценкой не ниже keep_from
    gray: list[Pair] = field(default_factory=list)  # серая зона между разными кластерами — для Claude


def build_clusters(
    candidates: list[Candidate],
    settings: DedupSettings,
    decisions: dict[tuple[int, int], str] | None = None,
    keep_from: float | None = None,
) -> ClusterResult:
    decisions = decisions or {}
    keep_from = settings.gray_threshold if keep_from is None else keep_from
    by_id = {c.id: c for c in candidates}
    prints: dict[int, Fingerprint] = {c.id: fingerprint(c.title, c.text) for c in candidates}
    weights = rare_word_weights({c.id: f"{c.title}\n{c.text or ''}" for c in candidates})
    links = _UnionFind(by_id)

    ordered = sorted(candidates, key=lambda c: c.published_at)
    window = timedelta(hours=settings.pair_window_h)
    pairs: list[Pair] = []
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if second.published_at - first.published_at > window:
                break
            if first.source == second.source:
                continue  # одно издание не дублирует само себя; повтор адреса снят ещё при сборе
            a, b = sorted((first.id, second.id))
            features = compare(prints[a], prints[b])
            score = features.score(settings)
            decision = decide(features, settings)
            # Заголовки и начало разные, а весь текст совпал: один закон пересказан разными словами
            if (
                decision == "different"
                and text_overlap(weights[a], weights[b]) >= settings.text_gray_threshold
            ):
                decision = "gray"
            pair = Pair(a, b, features, score, decision, decisions.get((a, b)))
            if pair.same:
                links.union(a, b)
            if score >= keep_from or pair.override or decision == "gray":
                pairs.append(pair)

    by_key: dict[str, list[int]] = {}
    for c in candidates:
        if c.doc_key:
            by_key.setdefault(c.doc_key, []).append(c.id)
    for ids in by_key.values():
        for other in ids[1:]:
            links.union(ids[0], other)

    clusters = [_cluster([by_id[i] for i in group]) for group in links.groups()]
    clusters.sort(key=lambda cl: by_id[cl.primary_id].published_at)
    gray = [
        p for p in pairs if p.decision == "gray" and not p.override and links.find(p.a) != links.find(p.b)
    ]
    return ClusterResult(clusters, sorted(pairs, key=lambda p: -p.score), gray)


def _cluster(members: list[Candidate]) -> Cluster:
    members.sort(key=lambda c: (c.published_at, TIER_RANK[c.tier], c.id))
    keys = Counter(c.doc_key for c in members if c.doc_key)
    doc_key = keys.most_common(1)[0][0] if keys else None
    return Cluster([c.id for c in members], "legal" if doc_key else "news", doc_key)


class _UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[int]]:
        groups: dict[int, list[int]] = {}
        for x in self.parent:
            groups.setdefault(self.find(x), []).append(x)
        return list(groups.values())


# --- Запись и повторы между днями ---


def save_clusters(
    conn: sqlite3.Connection, result: ClusterResult, filtered_ids: list[int], now: datetime
) -> list[int]:
    """Пишет кластеры в clusters и items. Возвращает id кластеров в порядке result.clusters.

    Кластер сохраняет свой id между пересборками: если главный или другой материал уже был в
    кластере, этот кластер переиспользуется. От id зависит кэш текстов Claude (summaries).
    """
    previous = repo.item_cluster_ids(conn, [i for cl in result.clusters for i in cl.item_ids])
    taken: set[int] = set()
    cluster_ids = []
    with conn:
        for cluster in result.clusters:
            reuse = next(
                (previous[i] for i in cluster.item_ids if previous.get(i) and previous[i] not in taken), None
            )
            cluster_id = repo.upsert_cluster(
                conn, reuse, cluster.kind, cluster.primary_id, cluster.doc_key, to_iso(now)
            )
            taken.add(cluster_id)
            repo.assign_cluster(conn, cluster_id, cluster.primary_id, cluster.item_ids)
            cluster_ids.append(cluster_id)
        repo.mark_filtered(conn, filtered_ids)
    return cluster_ids


@dataclass(frozen=True)
class Repeat:
    previous_cluster_id: int
    reason: str  # doc_key | item | similar | text


def find_repeats(
    conn: sqlite3.Connection,
    result: ClusterResult,
    candidates: list[Candidate],
    settings: DedupSettings,
    today: date,
) -> dict[int, Repeat]:
    """Кластеры, которые уже выходили в дайджестах за settings.repeat_days. Ключ — индекс в result.clusters.

    Исключение «акт был ожидающим, стал подтверждённым» решает этап 6 по previous_cluster_id.
    """
    published = repo.published_clusters(
        conn, (today - timedelta(days=settings.repeat_days)).isoformat(), today.isoformat()
    )
    if not published:
        return {}
    by_id = {c.id: c for c in candidates}
    by_key = {row["doc_key"]: row["id"] for row in published if row["doc_key"]}
    by_item = {row["primary_item_id"]: row["id"] for row in published}
    old_prints = [(row["id"], fingerprint(row["title"], row["text"])) for row in published]
    # Прошлые сюжеты с отрицательными ключами: у материалов и кластеров нумерация своя
    weights = rare_word_weights(
        {-row["id"]: f"{row['title']}\n{row['text'] or ''}" for row in published}
        | {c.id: f"{c.title}\n{c.text or ''}" for c in candidates}
    )

    repeats: dict[int, Repeat] = {}
    for index, cluster in enumerate(result.clusters):
        if cluster.doc_key and cluster.doc_key in by_key:
            repeats[index] = Repeat(by_key[cluster.doc_key], "doc_key")
            continue
        old_item = next((by_item[i] for i in cluster.item_ids if i in by_item), None)
        if old_item is not None:
            repeats[index] = Repeat(old_item, "item")
            continue
        primary = by_id[cluster.primary_id]
        new_print = fingerprint(primary.title, primary.text)
        for old_id, old_print in old_prints:
            if decide(compare(new_print, old_print), settings) == "same":
                repeats[index] = Repeat(old_id, "similar")
                break
        if index in repeats:
            continue
        same_text = next(
            (
                old_id
                for item_id in cluster.item_ids
                for old_id, _ in old_prints
                if text_overlap(weights[item_id], weights[-old_id]) >= settings.repeat_text_threshold
            ),
            None,
        )
        if same_text is not None:
            repeats[index] = Repeat(same_text, "text")
    return repeats
