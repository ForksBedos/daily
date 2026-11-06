"""Проход 5b: заголовок, краткое и подробное содержание на русском (docs/plan/stage-5-llm.md).

Текст сюжета пишется по главному материалу и реквизитам акта с lex.uz. Результат хранится
в summaries (lang = ru) с отпечатком входа: пока текст материала, реквизиты и промпт не менялись,
повторная сборка Claude не вызывает. Каждый ответ проходит автоматическую проверку фактов (qa.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from uzdigest.config import LlmSettings
from uzdigest.db import repo
from uzdigest.dedup.clustering import Candidate
from uzdigest.legal.doc_key import normalize_number, split_doc_key
from uzdigest.llm import qa
from uzdigest.llm.client import LlmBudget
from uzdigest.llm.prompt import as_json, input_hash, load_prompt
from uzdigest.llm.schemas import SummarizeResponse, Summary

log = logging.getLogger(__name__)

PROMPT = "summarize_v1"
CUT_MARK = "[текст сокращён]"
FACT_FLAG = "факт не из источника: "


@dataclass(frozen=True)
class Text:
    headline: str
    summary: str
    details: str

    def full(self) -> str:
        return f"{self.headline}\n{self.summary}\n{self.details}"


@dataclass(frozen=True)
class Job:
    """Сюжет, который идёт в дайджест."""

    cluster_id: int
    primary: Candidate
    others: list[Candidate]
    doc_key: str | None  # правовой сюжет
    act: sqlite3.Row | None  # реквизиты из legal_acts, если акт найден
    category: str | None
    importance: int | None


@dataclass(frozen=True)
class Written:
    text: Text
    flags: list[str]  # замечания проверки фактов; непустые — «⚠️ проверить»


def summarize(
    conn: sqlite3.Connection, budget: LlmBudget, jobs: list[Job], settings: LlmSettings
) -> dict[int, Written]:
    """Русские тексты сюжетов. Сюжета нет в ответе — Claude недоступен или пропустил его."""
    prompt = load_prompt(PROMPT)
    payloads = {job.cluster_id: _payload(job, settings.max_text_chars) for job in jobs}
    hashes = {cluster_id: input_hash(prompt.text, payload) for cluster_id, payload in payloads.items()}
    written: dict[int, Written] = {}
    todo: list[Job] = []
    for job in jobs:
        row = repo.summary_row(conn, job.cluster_id, prompt.version, "ru")
        if row is not None and row["input_hash"] == hashes[job.cluster_id]:
            text = Text(row["headline"], row["summary"], row["details"])
            # Проверку фактов повторяем: она могла стать точнее; замечания по акту — из БД
            act_flags = [f for f in json.loads(row["qa_flags"] or "[]") if not f.startswith(FACT_FLAG)]
            facts = fact_flags(text, job, payloads[job.cluster_id]["text"])
            written[job.cluster_id] = Written(text, facts + act_flags)
        else:
            todo.append(job)

    for start in range(0, len(todo), settings.summarize_batch):
        batch = {job.cluster_id: job for job in todo[start : start + settings.summarize_batch]}
        request = f"Сюжеты:\n{as_json([payloads[cluster_id] for cluster_id in batch])}"
        response = budget.ask(request, SummarizeResponse, prompt.text, "summarize")
        if response is None:
            continue
        with conn:
            for answer in response.items:
                job = batch.get(answer.cluster_id)
                if job is None:
                    log.warning(
                        "Тексты: Claude вернул сюжет %s, которого не было в запросе", answer.cluster_id
                    )
                    continue
                text = Text(answer.headline.strip(), answer.summary.strip(), answer.details.strip())
                flags = check_summary(text, answer, job, payloads[job.cluster_id]["text"])
                repo.save_summary(
                    conn,
                    cluster_id=job.cluster_id,
                    prompt_version=prompt.version,
                    lang="ru",
                    model=budget.settings.model,
                    category=job.category,
                    importance=job.importance,
                    headline=text.headline,
                    summary=text.summary,
                    details=text.details,
                    qa_flags=flags,
                    input_hash=hashes[job.cluster_id],
                )
                written[job.cluster_id] = Written(text, flags)
        missing = set(batch) - {a.cluster_id for a in response.items}
        if missing:
            log.warning("Тексты: Claude пропустил сюжеты %s", sorted(missing))
    return written


def fact_flags(text: Text, job: Job, source_text: str) -> list[str]:
    sources = (job.primary.title, source_text, _act_facts(job.act))
    return [f"{FACT_FLAG}{raw}" for raw in qa.unsupported(text.full(), *sources)]


def check_summary(text: Text, answer: Summary, job: Job, source_text: str) -> list[str]:
    """Факты ответа против источника и реквизитов акта (qa.py), плюс сверка упомянутого акта с doc_key."""
    flags = fact_flags(text, job, source_text)
    if job.doc_key:
        doc_type, number, _ = split_doc_key(job.doc_key)
        named = {(ref.kind, _ref_number(ref.kind, ref.number)) for ref in answer.legal_refs}
        if (doc_type, number) not in named:
            flags.append(f"в тексте не назван акт {job.doc_key}")
    if answer.act_matches is False:
        flags.append("реквизиты акта на lex.uz не совпадают с темой новости")
    return flags


def _ref_number(kind: str, number: str) -> str:
    """Номер из ответа Claude в виде, как в doc_key: «№ УП-236» → «236», «1010-2» → «1010-2»."""
    number = number.replace("№", "").strip()
    return number if kind == "MJ" else normalize_number(kind, number)


def _payload(job: Job, max_chars: int) -> dict[str, Any]:
    return {
        "cluster_id": job.cluster_id,
        "source": job.primary.source,
        "title": job.primary.title,
        "text": cut_text(job.primary.text or "", max_chars, job.primary.id),
        "act": _act(job.act),
        "also": [c.title for c in job.others],
    }


def cut_text(text: str, max_chars: int, item_id: int | None = None) -> str:
    """Обрезает длинный текст по границе абзаца и ставит явную пометку. Молча не режем."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    if cut < max_chars // 2:
        cut = max_chars
    log.info("Текст материала %s сокращён: %s → %s знаков", item_id, len(text), cut)
    return f"{text[:cut].rstrip()}\n{CUT_MARK}"


def _act(act: sqlite3.Row | None) -> dict[str, Any] | None:
    if act is None:
        return None
    return {
        "kind": act["issuer"],
        "number": act["number"],
        "adopted_on": act["adopted_on"],
        "effective_on": act["effective_on"],
        "title": act["title"],
    }


def _act_facts(act: sqlite3.Row | None) -> str:
    """Реквизиты акта текстом, который понимает qa.extract: номер и даты ДД.ММ.ГГГГ."""
    if act is None:
        return ""
    dates = [
        date.fromisoformat(act[col]).strftime("%d.%m.%Y")
        for col in ("adopted_on", "effective_on")
        if act[col]
    ]
    return " ".join([act["number"], *dates])
