"""Автоматическая проверка фактов в текстах Claude (docs/plan/stage-5-llm.md, «Автоматическая проверка»).

Сравниваются не строки, а значения: «1,5 млн» = «1.5 million» = «1 500 000»; «28 октября 2026» =
«28.10.2026» = «28 October 2026» = «2026-yil 28-oktabr». Номера актов — тоже числа: УП-236, PF-236
и UP-236 дают 236. Дата без года («с 1 ноября») совпадает с той же датой любого года.

Разница в тысячу раз — уже другое число: «1,5 млн» не подтверждается ни «1,5 тыс.», ни «1,5 %».
Исключение — начало диапазона: в «с $7,79 до $6,6 млрд» множитель стоит только у второго числа, а в
«с 300 до 1,5 тыс. тонн» первое число уже полное. По тексту этого не понять, поэтому годятся оба значения.

Целые числа до 10 без знака процента не проверяются: их часто пишут словами («в два раза», «twice»).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import zip_longest

_MONTHS_CYR = [
    "январ", "феврал", "март", "апрел", "ма[йяю]", "июн",
    "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
]  # fmt: skip
_MONTHS_UZ = [
    "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avgust", "sent[ya]?abr", "okt[ya]?abr", "noyabr", "dekabr",
]  # fmt: skip
_MONTHS_EN = [
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december",
]  # fmt: skip


def _months(stems: list[str]) -> str:
    """Месяцы как именованные группы mon1…mon12: какая совпала — такой и месяц."""
    return "(?:" + "|".join(f"(?P<mon{n}>{stem})" for n, stem in enumerate(stems, 1)) + ")"


_DAY = r"(?<!\d)(?P<d>\d{1,2})"
_YEAR_BEFORE_CYR = r"(?:(?P<y0>\d{4})\s*йил\w*\s+)?"  # «2026 йил 21 октябрдаги»
_YEAR_BEFORE_UZ = r"(?:(?P<y0>\d{4})-yil\w*\s+)?"  # «2026-yil 28-oktabr»
# Год после даты — только 19xx/20xx и не номер: «31-oktabrdagi 3966-son» — это рег. номер, а не год
_YEAR_AFTER = r"(?:,?\s+(?P<y>(?:19|20)\d{2})(?![\d-]))?"
# Порядок важен: числовая дата раньше словесных, кириллица раньше латиницы
_DATE_PATTERNS = [
    # 28.10.2026
    re.compile(r"(?<!\d)(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})(?!\d)"),
    # 28 октября 2026 · 1 ноября · 2026 йил 21 октябрдаги
    re.compile(rf"{_YEAR_BEFORE_CYR}{_DAY}[-\s]{_months(_MONTHS_CYR)}[а-яё]*{_YEAR_AFTER}", re.I),
    # 2026-yil 28-oktabr · 28 oktabr 2026
    re.compile(rf"{_YEAR_BEFORE_UZ}{_DAY}[-\s]{_months(_MONTHS_UZ)}[a-z‘’']*{_YEAR_AFTER}", re.I),
    # 28 October 2026
    re.compile(rf"(?<!\d)(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+{_months(_MONTHS_EN)}\b{_YEAR_AFTER}", re.I),
    # October 28, 2026
    re.compile(rf"\b{_months(_MONTHS_EN)}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b{_YEAR_AFTER}", re.I),
]  # fmt: skip

_SPACES = "   "  # обычный, неразрывный и узкий неразрывный пробел между разрядами
# Разряды через пробел и десятичная запятая или точка: «12 023,46», «1,5», «1.5»
_NUMBER = re.compile(rf"(?<![\d.,])(?:\d{{1,3}}(?:[{_SPACES}]\d{{3}})+(?!\d)(?:,\d+)?|\d+(?:[.,]\d+)?)")
# В английском запятая отделяет разряды: «12,023.46»
_NUMBER_EN = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+(?!\d)(?:\.\d+)?|\d+(?:\.\d+)?)")
_MULTIPLIERS = [
    (re.compile(r"\s*(?:трлн|триллион\w*|trillion\w*|trln)", re.I), 1e12),
    (re.compile(r"\s*(?:млрд|миллиард\w*|billion\w*|milliard\w*|mlrd|bn\b)", re.I), 1e9),
    (re.compile(r"\s*(?:млн|миллион\w*|million\w*|mln|mn\b)", re.I), 1e6),
    (re.compile(r"\s*(?:тыс\b\.?|тысяч\w*|thousand\w*|ming\w*|минг\w*)", re.I), 1e3),
]
_PERCENT = re.compile(r"\s*(?:%|процент|percent|per cent|foiz)", re.I)
# Что стоит между числами диапазона: «300–400 тыс.», «с $7,79 до $6,6 млрд», «100 dan 500 mlrd»
_RANGE_GAP = re.compile(r"\s*(?:до|и|или|to|and|or|-?dan|va|yoki|[-–—])\s*(?:[$€£]|UZS|USD|EUR)?\s*", re.I)

Date = tuple[int, int, int | None]  # день, месяц, год


@dataclass
class Facts:
    numbers: dict[float, str] = field(default_factory=dict)  # значение → как написано
    dates: dict[Date, str] = field(default_factory=dict)
    range_starts: dict[float, float] = field(default_factory=dict)  # начало диапазона → с множителем конца

    def has_number(self, value: float) -> bool:
        return value in self.numbers or value in self.range_starts.values()

    def missing_numbers(self, other: Facts) -> list[str]:
        """Числа этого текста, как они написаны, которых нет в other."""
        return [
            raw
            for value, raw in self.numbers.items()
            if not other.has_number(value) and not other.has_number(self.range_starts.get(value, value))
        ]

    def has_date(self, date: Date) -> bool:
        day, month, year = date
        return any(
            d == day and m == month and (y is None or year is None or y == year) for d, m, y in self.dates
        )

    def update(self, other: Facts) -> None:
        self.numbers.update(other.numbers)
        self.dates.update(other.dates)
        self.range_starts.update(other.range_starts)


def extract(text: str, lang: str = "ru") -> Facts:
    """Числа и даты текста. lang влияет только на запись чисел: в английском «1,500» — тысяча пятьсот."""
    facts = Facts()
    chars = list(text)
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if "\0" in chars[match.start() : match.end()]:
                continue  # эту дату уже разобрал предыдущий шаблон
            date = _date(match)
            if date is None:
                continue
            facts.dates.setdefault(date, match.group(0).strip())
            chars[match.start() : match.end()] = "\0" * (match.end() - match.start())
    rest = "".join(chars)
    matches = list((_NUMBER_EN if lang == "en" else _NUMBER).finditer(rest))
    for match, following in zip_longest(matches, matches[1:]):
        raw = match.group(0)
        value = (
            float(raw.replace(",", ""))
            if lang == "en"
            else float(re.sub(f"[{_SPACES}]", "", raw).replace(",", "."))
        )
        tail = rest[match.end() :]
        percent = bool(_PERCENT.match(tail))
        multiplier, word = _multiplier(tail)
        value *= multiplier
        raw += word
        if not word and following and _RANGE_GAP.fullmatch(rest[match.end() : following.start()]):
            shared, _ = _multiplier(rest[following.end() :])
            if shared != 1:
                facts.range_starts[_exact(value)] = _exact(value * shared)
        if value == int(value) and value <= 10 and not percent:
            continue
        facts.numbers.setdefault(_exact(value), raw.strip())
    return facts


def _multiplier(tail: str) -> tuple[float, str]:
    """Множитель сразу за числом и как он написан; нет — единица и пустая строка."""
    for pattern, multiplier in _MULTIPLIERS:
        if match := pattern.match(tail):
            return multiplier, match.group(0)
    return 1, ""


def _exact(value: float) -> float:
    """12 значащих цифр: иначе погрешность умножения разводит 0,54 трлн и 540 млрд."""
    return float(f"{round(value, 4):.12g}")


def _date(match: re.Match[str]) -> Date | None:
    groups = match.groupdict()
    if groups.get("m"):
        month = int(groups["m"])
    else:
        month = next(n for n in range(1, 13) if groups.get(f"mon{n}"))
    day = int(groups["d"])
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    year = groups.get("y") or groups.get("y0")
    return day, month, int(year) if year else None


def _format_date(date: Date) -> str:
    day, month, year = date
    return f"{day:02d}.{month:02d}.{year}" if year else f"{day:02d}.{month:02d}"


# --- Проверки ---


def unsupported(answer: str, *sources: str) -> list[str]:
    """Числа и даты русского ответа, которых нет ни в одном источнике (текст материала, реквизиты акта)."""
    found, known = extract(answer), Facts()
    for source in sources:
        known.update(extract(source))
    problems = found.missing_numbers(known)
    problems += [raw for date, raw in found.dates.items() if not known.has_date(date)]
    return problems


def mismatched(ru: str, translated: str, lang: str) -> list[str]:
    """Расхождения чисел и дат между русским текстом и переводом: чего нет в переводе и что в нём лишнее."""
    original, other = extract(ru, "ru"), extract(translated, lang)
    problems = [f"нет {raw}" for raw in original.missing_numbers(other)]
    problems += [f"нет {_format_date(d)}" for d in original.dates if not other.has_date(d)]
    problems += [f"лишнее {raw}" for raw in other.missing_numbers(original)]
    problems += [f"лишнее {_format_date(d)}" for d in other.dates if not original.has_date(d)]
    return problems
