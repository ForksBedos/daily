"""Проход 5c: перевод готовых русских текстов на английский и узбекский (docs/plan/stage-5-llm.md).

Переводится только текст, написанный Claude (не резервный анонс). Глоссарий и правила языка —
из config/glossary.yaml, они входят в системный промпт и в отпечаток входа: правка глоссария
пересоздаёт переводы. Числа, даты и номера перевода сверяются с русской версией (qa.mismatched).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from uzdigest.config import CONFIG_DIR, LlmSettings
from uzdigest.db import repo
from uzdigest.llm import qa
from uzdigest.llm.client import LlmBudget
from uzdigest.llm.prompt import Prompt, as_json, input_hash, load_prompt
from uzdigest.llm.schemas import TranslateResponse
from uzdigest.llm.summarize import Job, Text, Written

log = logging.getLogger(__name__)

PROMPT = "translate_v1"
LANGS = ("en", "uz")


@dataclass(frozen=True)
class Glossary:
    languages: dict[str, dict[str, str]]  # язык → name, rules
    terms: list[dict[str, str]]  # ru, en, uz

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "glossary.yaml") -> Glossary:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(data["languages"], data["terms"])

    def system_prompt(self, prompt: Prompt, lang: str) -> str:
        table = "\n".join(f"- {term['ru']} → {term[lang]}" for term in self.terms)
        language = self.languages[lang]
        return prompt.fill(
            language=language["name"], language_rules=language["rules"].strip(), glossary=table
        )


def translate(
    conn: sqlite3.Connection,
    budget: LlmBudget,
    jobs: list[Job],
    russian: dict[int, Written],
    settings: LlmSettings,
    glossary: Glossary,
) -> dict[str, dict[int, Written]]:
    """Переводы по языкам: язык → сюжет → текст. Сюжета нет — перевод не готов."""
    prompt = load_prompt(PROMPT)
    jobs_by_id = {job.cluster_id: job for job in jobs if job.cluster_id in russian}
    result: dict[str, dict[int, Written]] = {}
    for lang in LANGS:
        system_prompt = glossary.system_prompt(prompt, lang)
        payloads = {
            cluster_id: {"cluster_id": cluster_id, **_fields(russian[cluster_id].text)}
            for cluster_id in jobs_by_id
        }
        hashes = {cluster_id: input_hash(system_prompt, payload) for cluster_id, payload in payloads.items()}
        done: dict[int, Written] = {}
        todo: list[int] = []
        for cluster_id in jobs_by_id:
            row = repo.summary_row(conn, cluster_id, prompt.version, lang)
            if row is not None and row["input_hash"] == hashes[cluster_id]:
                text = Text(row["headline"], row["summary"], row["details"])
                # Проверку повторяем: она могла стать точнее с тех пор, как перевод сохранён
                done[cluster_id] = Written(text, _check(russian[cluster_id].text, text, lang))
            else:
                todo.append(cluster_id)

        for start in range(0, len(todo), settings.translate_batch):
            batch = todo[start : start + settings.translate_batch]
            request = f"Статьи:\n{as_json([payloads[cluster_id] for cluster_id in batch])}"
            response = budget.ask(request, TranslateResponse, system_prompt, "translate")
            if response is None:
                continue
            with conn:
                for answer in response.items:
                    job = jobs_by_id.get(answer.cluster_id)
                    if job is None or answer.cluster_id not in batch:
                        log.warning("Перевод %s: лишняя статья %s в ответе", lang, answer.cluster_id)
                        continue
                    text = Text(answer.headline.strip(), answer.summary.strip(), answer.details.strip())
                    flags = _check(russian[answer.cluster_id].text, text, lang)
                    repo.save_summary(
                        conn,
                        cluster_id=answer.cluster_id,
                        prompt_version=prompt.version,
                        lang=lang,
                        model=budget.settings.model,
                        category=job.category,
                        importance=job.importance,
                        headline=text.headline,
                        summary=text.summary,
                        details=text.details,
                        qa_flags=flags,
                        input_hash=hashes[answer.cluster_id],
                    )
                    done[answer.cluster_id] = Written(text, flags)
            missing = set(batch) - {a.cluster_id for a in response.items}
            if missing:
                log.warning("Перевод %s: Claude пропустил статьи %s", lang, sorted(missing))
        result[lang] = done
    return result


def _check(original: Text, translated: Text, lang: str) -> list[str]:
    return [f"{lang}: {problem}" for problem in qa.mismatched(original.full(), translated.full(), lang)]


def _fields(text: Text) -> dict[str, str]:
    return {"headline": text.headline, "summary": text.summary, "details": text.details}
