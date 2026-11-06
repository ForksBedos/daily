"""Проход 5a: отбор материалов и решения по серой зоне дублей (docs/plan/stage-5-llm.md).

Материал оценивается один раз на версию промпта: оценки хранятся в таблице triage, решения
по парам — в pair_decisions (source=llm). Без Claude материал получает резервную оценку
по уровню источника, она в БД не пишется: в следующем запуске Claude оценит его заново.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from uzdigest.db import repo
from uzdigest.dedup.clustering import Candidate, Pair
from uzdigest.llm.client import LlmBudget
from uzdigest.llm.prompt import as_json, load_prompt
from uzdigest.llm.schemas import TriageResponse

log = logging.getLogger(__name__)

# v4 (05.11.2026): оценка 3 — только то, что клиент должен сделать или учесть; фон получает 2.
# v2 (03.11.2026): только темы для компаний; v3: без поручений, занятости, жилья и соцреформ
PROMPT = "triage_v4"
TEXT_CHARS = 400  # начало текста для оценки: заголовка и анонса почти всегда достаточно


@dataclass(frozen=True)
class Verdict:
    relevant: bool
    category: str | None
    importance: int
    reason: str
    origin: str  # llm — ответ Claude (в т. ч. из кэша), fallback — резервная оценка без Claude


def fallback_verdict(candidate: Candidate) -> Verdict:
    """Без Claude: официальные и профессиональные источники и правовые сюжеты — в дайджест, СМИ — нет."""
    if candidate.doc_key:
        return Verdict(True, "legislation", 3, "без ИИ: правовой сюжет", "fallback")
    if candidate.tier in ("A", "B"):
        return Verdict(True, "business", 3, f"без ИИ: источник уровня {candidate.tier}", "fallback")
    return Verdict(False, None, 1, "без ИИ: СМИ", "fallback")


def triage(
    conn: sqlite3.Connection,
    budget: LlmBudget,
    candidates: list[Candidate],
    gray: list[Pair],
    batch_size: int,
) -> dict[int, Verdict]:
    """Оценка каждого материала; заодно решения Claude по парам серой зоны записываются в pair_decisions."""
    prompt = load_prompt(PROMPT)
    ids = [c.id for c in candidates]
    verdicts = {
        item_id: Verdict(
            bool(row["relevant"]), row["category"], row["importance"], row["reason"] or "", "llm"
        )
        for item_id, row in repo.triage_rows(conn, ids, prompt.version).items()
    }
    decided = repo.decided_pairs(conn, ids)
    todo_pairs = [p for p in gray if (p.a, p.b) not in decided]
    todo_items = [c for c in candidates if c.id not in verdicts]
    by_id = {c.id: c for c in candidates}

    for items, pairs in _batches(todo_items, todo_pairs, by_id, batch_size):
        response = budget.ask(_request(items, pairs), TriageResponse, prompt.text, "triage")
        if response is None:
            continue
        asked_items = {c.id for c in items}
        asked_pairs = {(p.a, p.b) for p in pairs}
        with conn:
            for answer in response.items:
                if answer.id not in asked_items:
                    log.warning(
                        "Отбор: Claude вернул оценку материала %s, которого не было в запросе", answer.id
                    )
                    continue
                category = answer.category if answer.relevant else None
                repo.save_triage(
                    conn,
                    answer.id,
                    prompt.version,
                    answer.relevant,
                    category,
                    answer.importance,
                    answer.reason,
                )
                verdicts[answer.id] = Verdict(
                    answer.relevant, category, answer.importance, answer.reason, "llm"
                )
            for decision in response.pairs:
                a, b = sorted((decision.a, decision.b))
                if (a, b) in asked_pairs:
                    repo.save_pair_decision(conn, a, b, "same" if decision.same else "different", "llm")
        missing = asked_items - {a.id for a in response.items}
        if missing:
            log.warning("Отбор: нет оценок для %s материалов: %s", len(missing), sorted(missing))

    for candidate in candidates:
        verdicts.setdefault(candidate.id, fallback_verdict(candidate))
    return verdicts


def _batches(
    items: list[Candidate], pairs: list[Pair], by_id: dict[int, Candidate], size: int
) -> Iterator[tuple[list[Candidate], list[Pair]]]:
    """Пакеты по size материалов. Пара идёт в пакет своего первого материала, второй материал
    добавляется туда же: Claude сравнивает только то, что видит в одном запросе."""
    batches: list[list[Candidate]] = [items[i : i + size] for i in range(0, len(items), size)]
    where = {c.id: n for n, batch in enumerate(batches) for c in batch}
    batch_pairs: dict[int, list[Pair]] = {}
    for pair in pairs:
        n = where.get(pair.a, where.get(pair.b))
        if n is None:  # оба материала уже оценены: нужен пакет только ради пары
            if not batches or len(batches[-1]) >= size:
                batches.append([])
            n = len(batches) - 1
        for item_id in (pair.a, pair.b):
            if all(c.id != item_id for c in batches[n]):
                batches[n].append(by_id[item_id])
                where.setdefault(item_id, n)
        batch_pairs.setdefault(n, []).append(pair)
    for n, batch in enumerate(batches):
        yield batch, batch_pairs.get(n, [])


def _request(items: list[Candidate], pairs: list[Pair]) -> str:
    materials = [
        {
            "id": c.id,
            "source": c.source,
            "tier": c.tier,
            "title": c.title,
            "text": " ".join((c.text or "")[:TEXT_CHARS].split()),
        }
        for c in items
    ]
    request = f"Материалы:\n{as_json(materials)}"
    if pairs:
        request += f"\n\nПары для сравнения:\n{as_json([[p.a, p.b] for p in pairs])}"
    else:
        request += "\n\nПар для сравнения нет: верни пустой список pairs."
    return request
