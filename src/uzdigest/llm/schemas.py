"""Схемы ответов Claude для трёх проходов (docs/plan/stage-5-llm.md).

extra="forbid" даёт в JSON-схеме additionalProperties: false: лишнее поле — ошибка, а не тихо
проигнорированный ответ. Ограничения значений — через enum (Literal): их понимает structured output.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Разделы дайджеста в порядке вывода (PLAN.md, раздел 7)
CATEGORIES = ("legislation", "taxes", "accounting", "finance", "business", "macro")
Category = Literal["legislation", "taxes", "accounting", "finance", "business", "macro"]
Importance = Literal[1, 2, 3, 4, 5]
ActKind = Literal["ZRU", "UP", "PP", "RP", "PKM", "MJ", "other"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Пост для соцсети (этап 7) ---


class PostResponse(_Strict):
    text: str  # пост целиком, с хэштегами и ссылкой
    hashtags: list[str]  # те же хэштеги, что в text, — для контроля
    notes: str  # что пришлось опустить; пусто, если нечего сказать


# --- Отбор ---


class TriageItem(_Strict):
    id: int
    relevant: bool
    category: Category | None  # None, если relevant = false
    importance: Importance
    reason: str  # до 15 слов: для отладки на пилоте


class PairDecision(_Strict):
    a: int
    b: int
    same: bool  # одно и то же событие или документ


class TriageResponse(_Strict):
    items: list[TriageItem]
    pairs: list[PairDecision]


# --- Тексты на русском ---


class ActRef(_Strict):
    kind: ActKind  # код вида: УП → UP, ПКМ → PKM, ведомственный акт с номером Минюста → MJ
    number: str  # как в тексте, без вида: «236», «1010-2»
    date: str | None  # дата принятия ДД.ММ.ГГГГ, если есть в тексте


class Summary(_Strict):
    cluster_id: int
    headline: str
    summary: str  # 2–3 предложения
    details: str  # 600–1500 знаков
    legal_refs: list[ActRef]
    act_matches: bool | None  # реквизиты акта из запроса относятся к этой новости; None — акта нет


class SummarizeResponse(_Strict):
    items: list[Summary]


# --- Перевод ---


class Translation(_Strict):
    cluster_id: int
    headline: str
    summary: str
    details: str


class TranslateResponse(_Strict):
    items: list[Translation]
