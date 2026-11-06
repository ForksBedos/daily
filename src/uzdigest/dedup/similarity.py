"""Попарная похожесть материалов (docs/plan/stage-3-dedup.md, задача 3.2).

Дубли между СМИ почти всегда перефразированы, поэтому одного сравнения заголовков мало.
Признаки пары:
  title   — Jaccard по основам слов заголовка, без служебных и слишком частых слов. Основа —
            слово без окончания, обрезанное до 5 букв: «возбудили» и «возбуждено» → «возбу»,
            «знаками», «знаков» и «знаки» → «знак»;
  anchors — число общих «якорей»: чисел, слов на латинице и имён собственных;
  text    — Jaccard по основам слов первых 400 знаков текста (или анонса).
Итоговая оценка — взвешенная сумма, веса и пороги в settings.yaml (секция dedup).

План предлагал для текста 5-словные шинглы. На 582 реальных материалах (30.10–02.11.2026) у
перефразированных дублей шинглы почти не пересекаются (0,00–0,06), а основы слов начала
текста дают 0,13–0,53 при 99-м перцентиле у случайных пар 0,10. Шинглы ловят только
перепечатки, а у перепечаток и так совпадает заголовок.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from uzdigest.config import DedupSettings

Decision = Literal["same", "gray", "different"]

STEM = 5
TEXT_HEAD = 400
# Окончания русских слов, длинные первыми. Только обрезки до 5 букв мало: «знаками» и «знаков»
# давали разные основы, и пересказы одной новости про радары уходили в «разные»
ENDINGS = sorted(
    "ами ями ого его ому ему ыми ими ах ях ов ев ей ой ий ый ая яя ое ее ые ие ам ям ом ем ую юю "
    "их ых ть ли ла ло а я о е и ы у ю".split(),
    key=len,
    reverse=True,
)

# Служебные слова и слова, которые есть почти в каждой новости: они дают ложную похожесть
# («Музеи Узбекистана и Петербурга договорились…» и «Узбекистан и США договорились…»)
STOPWORDS = frozenset(
    """
    в во на и о об обо по с со за для из к ко от до не ни что как а но или у же бы при под над
    перед после это этот эта эти тот та те его ее её их он она они мы вы я все всё был была были
    будет будут быть есть также уже еще ещё только более менее между через около свой своих
    года году год лет г млн млрд тыс сум сумов долларов
    """.split()
)
COMMON_STEMS = frozenset(
    # страна, столица, глава государства
    "узбек ташке респу прези мирзи шавка стран "
    # ведомства и органы: с заглавной буквы, но есть в каждой второй новости
    "кабин минис комит главн управ центр нацио госуд агент совет палат хоким облас район город "
    "налог банк сенат олий парла "
    # глаголы и слова-связки заголовков
    "нового новое новые новый может могут станет стали стало решил заяви сообщ расск".split()
)
# Названия изданий встречаются в тексте («(UzDaily.uz) —», «АН Podrobno.uz») и не говорят о сюжете
MEDIA = frozenset(
    "uzdaily podrobno spot kun daryo gazeta kursiv nuz uza review norma buxgalter uz ru".split()
)

_WORD = re.compile(r"[a-zа-яёўқғҳ0-9]+", re.I)
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
# Даты — не якоря: «29 октября», «28.10.2026» есть в каждой второй новости за неделю
_DATES = re.compile(rf"\b\d{{1,2}}\.\d{{1,2}}\.\d{{2,4}}\b|\b\d{{1,2}}\s+(?:{_MONTHS})\b", re.I)
_LATIN = re.compile(r"\b[A-Za-z][A-Za-z\-]{2,}\b")
_PROPER = re.compile(r"(?<=[^\s.!?…«\"(—-]\s)[А-ЯЁЎҚҒҲ][а-яёўқғҳ]{2,}")


@dataclass(frozen=True)
class Fingerprint:
    title: frozenset[str]
    anchors: frozenset[str]
    text: frozenset[str]


@dataclass(frozen=True)
class Features:
    title: float
    anchors: int
    text: float

    def score(self, settings: DedupSettings) -> float:
        anchors = min(self.anchors, settings.anchors_cap) / settings.anchors_cap
        return settings.w_title * self.title + settings.w_anchors * anchors + settings.w_text * self.text


def fingerprint(title: str, text: str | None) -> Fingerprint:
    """text — полный текст или анонс. Якоря ищутся в заголовке и начале текста."""
    head = (text or "")[:TEXT_HEAD]
    return Fingerprint(
        title=frozenset(_stems(title)),
        anchors=frozenset(_anchors(f"{title}. {head}")),
        text=frozenset(_stems(head)),
    )


def rare_word_weights(texts: dict[int, str]) -> dict[int, dict[str, float]]:
    """Вес основ слов всего текста: редкие в окне весят много, частые почти ничего. Длина вектора 1."""
    stems = {key: Counter(_stems(text)) for key, text in texts.items()}
    documents = Counter(stem for counts in stems.values() for stem in counts)
    total = len(texts)
    weights: dict[int, dict[str, float]] = {}
    for key, counts in stems.items():
        raw = {s: (1 + math.log(n)) * math.log((1 + total) / documents[s]) for s, n in counts.items()}
        length = math.sqrt(sum(v * v for v in raw.values())) or 1.0
        weights[key] = {s: v / length for s, v in raw.items()}
    return weights


def text_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    """Совпадение двух текстов по редким словам, от 0 до 1."""
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return sum(v * large.get(s, 0.0) for s, v in small.items())


def compare(a: Fingerprint, b: Fingerprint) -> Features:
    return Features(
        title=_jaccard(a.title, b.title),
        anchors=len(a.anchors & b.anchors),
        text=_jaccard(a.text, b.text),
    )


def decide(features: Features, settings: DedupSettings) -> Decision:
    score = features.score(settings)
    if score >= settings.merge_threshold:
        return "same"
    if score >= settings.gray_threshold:
        return "gray"
    return "different"


def _stems(text: str) -> list[str]:
    words = (w.lower().replace("ё", "е") for w in _WORD.findall(text))
    stems = [_stem(w) for w in words if w not in STOPWORDS and not w.isdigit() and len(w) > 1]
    return [s for s in stems if s not in COMMON_STEMS and s not in MEDIA]


def _stem(word: str) -> str:
    if len(word) > 4 and not word.isascii():
        for ending in ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                word = word[: -len(ending)]
                break
    return word[:STEM]


def _anchors(text: str) -> set[str]:
    anchors = set()
    for raw in _NUMBER.findall(_DATES.sub(" ", text)):
        value = raw.replace(",", ".")
        number = float(value)
        if number < 10 or (number.is_integer() and 1990 <= number <= 2035):
            continue  # однозначные числа и годы есть почти в каждой новости
        anchors.add(f"#{value.rstrip('0').rstrip('.') if '.' in value else value}")
    for word in _LATIN.findall(text):
        word = word.lower()
        if word not in MEDIA:
            anchors.add(word)
    for word in _PROPER.findall(text):
        stem = word.lower().replace("ё", "е")[:STEM]
        if stem not in COMMON_STEMS:
            anchors.add(stem)
    return anchors


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
