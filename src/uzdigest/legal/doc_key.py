"""Правовой ключ акта: `{вид}-{номер}-{год}` (PLAN.md, раздел 4).

Ключ одинаков для упоминаний на русском, узбекской латинице и кириллице: УП-236, PF-236 и
ПФ-236 от 2026 года — это UP-236-2026. Нумерация указов и постановлений периодически
начинается заново, поэтому в ключ входит год принятия. Ведомственные акты идентифицируются
номером регистрации в Минюсте, он уникален и без года: MJ-1010-2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Коды видов (docs/plan/stage-2-sources.md, задача 2.3)
ZRU, UP, PP, RP, PKM, SENATE, MJ, COURT = "ZRU", "UP", "PP", "RP", "PKM", "SENATE", "MJ", "COURT"

# Вид акта по началу описания на lex.uz: «Указ Президента Республики Узбекистан …»
_KIND_PATTERNS = [
    (re.compile(r"^(конституционный\s+)?закон\b", re.I), ZRU),
    (re.compile(r"^указ\s+президента\b", re.I), UP),
    (re.compile(r"^постановление\s+президента\b", re.I), PP),
    (re.compile(r"^распоряжение\s+президента\b", re.I), RP),
    (re.compile(r"^постановление\s+кабинета\s+министров\b", re.I), PKM),
    (re.compile(r"^постановление\s+(кенгаша\s+)?сената\b", re.I), SENATE),
    (re.compile(r"^решение\b.*\bсуд", re.I), COURT),
]
# То же на узбекском: «Ўзбекистон Республикаси Президентининг қарори», «… Vazirlar Mahkamasining qarori»
_KIND_PATTERNS_UZ = [
    (re.compile(r"\b(конституциявий\s+)?қонуни\b|\b(konstitutsiyaviy\s+)?qonuni\b", re.I), ZRU),
    (re.compile(r"президентининг\s+фармони\b|prezidentining\s+farmoni\b", re.I), UP),
    (re.compile(r"президентининг\s+қарори\b|prezidentining\s+qarori\b", re.I), PP),
    (re.compile(r"президентининг\s+фармойиши\b|prezidentining\s+farmoyishi\b", re.I), RP),
    (re.compile(r"вазирлар\s+маҳкамасининг\s+қарори\b|vazirlar\s+mahkamasining\s+qarori\b", re.I), PKM),
]

# Буквенная часть номера, которая повторяет вид акта: ЗРУ-1184 → 1184
_NUMBER_PREFIX = re.compile(r"^(ЗРУ|ЎРҚ|O[‘’'ʻ`]?RQ|УП|ПФ|PF|ПП|ПҚ|PQ|РП|Р|R|ВМҚ|VMQ)-", re.I)

_TRANSLIT = str.maketrans(
    {
        "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ж": "J", "З": "Z", "И": "I",
        "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T",
        "У": "U", "Ф": "F", "Х": "X", "Ц": "TS", "Ч": "CH", "Ш": "SH", "Э": "E", "Ю": "YU", "Я": "YA",
        "Ў": "O", "Қ": "Q", "Ғ": "G", "Ҳ": "H",
    }
)  # fmt: skip


def kind_code(kind_text: str) -> str | None:
    """Код вида акта по его названию на русском или узбекском. None — вид не распознан."""
    text = kind_text.strip()
    for pattern, code in _KIND_PATTERNS + _KIND_PATTERNS_UZ:
        if pattern.search(text):
            return code
    return None


def normalize_number(doc_type: str, number: str) -> str:
    """Номер без повтора вида и латиницей: «УП-236» → «236», «ПС-377-V» → «PS-377-V»."""
    value = re.sub(r"\s+", "", number).strip(".")
    if doc_type in (ZRU, UP, PP, RP, PKM):
        value = _NUMBER_PREFIX.sub("", value)
    return value.upper().translate(_TRANSLIT)


def make_doc_key(doc_type: str, number: str, year: int | None) -> str:
    if doc_type == MJ:
        return f"{MJ}-{number}"
    if year is None:
        raise ValueError(f"для {doc_type}-{number} нужен год принятия")
    return f"{doc_type}-{number}-{year}"


def split_doc_key(key: str) -> tuple[str, str, int | None]:
    """Ключ обратно в вид, номер и год: PKM-594-2026 → (PKM, 594, 2026), MJ-1010-2 → (MJ, 1010-2, None)."""
    doc_type, rest = key.split("-", 1)
    if doc_type == MJ:
        return doc_type, rest, None
    number, year = rest.rsplit("-", 1)
    return doc_type, number, int(year)


# --- Упоминания актов в свободном тексте (docs/plan/stage-3-dedup.md, задача 3.3) ---


@dataclass(frozen=True)
class LegalRef:
    doc_key: str
    doc_type: str
    number: str
    year: int | None  # None только у MJ
    year_guessed: bool  # даты рядом нет, взят год публикации материала
    is_draft: bool  # рядом сказано, что это проект
    raw_mention: str
    position: int


# Номер с буквенным видом: УП-236, ПП-387, ЗРУ-1184, PF-236, ПҚ-387, O‘RQ-1184, ВМҚ-594
_CODED = re.compile(
    r"(?<![\w-])(ЗРУ|ЎРҚ|O[‘’'ʻ`]?RQ|УП|ПФ|PF|ПП|ПҚ|PQ|РП|ВМҚ|VMQ)\s?[-–]\s?(\d{1,4})(?![\d/])", re.I
)
_CODE_BY_PREFIX = {
    "ЗРУ": ZRU, "ЎРҚ": ZRU, "ORQ": ZRU, "УП": UP, "ПФ": UP, "PF": UP, "ПП": PP, "ПҚ": PP, "PQ": PP,
    "РП": RP, "ВМҚ": PKM, "VMQ": PKM,
}  # fmt: skip
# «№ 594» без вида рядом — постановление Кабмина, только если о Кабмине сказано чуть раньше
_NUMBER = re.compile(r"№\s*(\d{1,4})(?![\d/-])")
_KABMIN_BEFORE = re.compile(
    r"(\bПКМ\b|постановлени\w*\s+(Кабинета\s+Министров|Кабмина|КМ|правительства)"
    r"|(Кабинет\w*\s+Министров|Кабмин\w*|правительств\w*)\s+(принял\w*|утвердил\w*)\s+постановлени\w*)"
    r"[^№;]{0,40}$",  # между ними только дата: «Постановлением Кабмина от 28.10.2026 г. № 594»
    re.I,
)
# Ведомственные акты: «рег. № 3956», «рег. № МЮ 1010-2», «зарегистрирован Минюстом … № 3009-4»
_MJ = re.compile(
    r"(рег\.?\s*№\s*(?:МЮ\s*)?|зарегистрирован\w*\s+(?:в\s+)?(?:Минюст\w*|Министерств\w+\s+юстиции)[^№.]{0,60}№\s*)"
    r"(\d{3,4}(?:-\d{1,2})?)(?!\d)",
    re.I,
)
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
# 28.10.2026 · 29 октября 2026 · 2026 йил 21 октябрдаги · «№ 440 от 2019 года» (только год)
_DATE = re.compile(
    rf"\b\d{{1,2}}\.\d{{1,2}}\.(\d{{4}})\b|\b\d{{1,2}}\s+(?:{_MONTHS})\s+(\d{{4}})|\b(\d{{4}})\s+йил"
    r"|\bот\s+(\d{4})\s+г(?:ода\b|\.)",
    re.I,
)
_DRAFT = re.compile(
    r"\bпроект\w*\s+(постановлени|указ|закон|ПКМ|ПП|УП|документ)|\bобсуждени\w*|regulation\.gov\.uz"
    r"|\bloyiha|\bлойиҳа",
    re.I,
)
_DATE_RADIUS = 45  # дата «от …» стоит вплотную к номеру: «от 28.10.2026 г. № 594», «№ 596 от 29 октября 2026»
_DRAFT_RADIUS = 120


def find_refs(text: str, default_year: int) -> list[LegalRef]:
    """Все упоминания актов в тексте, по одному на doc_key, в порядке появления."""
    found: list[tuple[int, int, str, str]] = []  # начало, конец, вид, номер
    for match in _CODED.finditer(text):
        prefix = re.sub(r"[‘’'ʻ`]", "", match.group(1)).upper()
        found.append((match.start(), match.end(), _CODE_BY_PREFIX[prefix], match.group(2)))
    for match in _NUMBER.finditer(text):
        if _KABMIN_BEFORE.search(text[max(0, match.start() - 120) : match.start()]):
            found.append((match.start(), match.end(), PKM, match.group(1)))
    for match in _MJ.finditer(text):
        found.append((match.start(), match.end(), MJ, match.group(2)))

    refs: dict[str, LegalRef] = {}
    for start, end, doc_type, number in sorted(found):
        if doc_type == MJ:
            year, guessed = None, False
        else:
            others = [(s, e) for s, e, t, n in found if (t, n) != (doc_type, number)]
            year, guessed = _year_near(text, start, end, default_year, others)
        key = make_doc_key(doc_type, number, year)
        if key in refs:
            continue
        around = text[max(0, start - _DRAFT_RADIUS) : end + _DRAFT_RADIUS]
        refs[key] = LegalRef(
            doc_key=key,
            doc_type=doc_type,
            number=number,
            year=year,
            year_guessed=guessed,
            is_draft=bool(_DRAFT.search(around)),
            raw_mention=text[max(0, start - 40) : end + 40].replace("\n", " ").strip(),
            position=start,
        )
    return list(refs.values())


def _year_near(
    text: str, start: int, end: int, default_year: int, others: list[tuple[int, int]]
) -> tuple[int, bool]:
    """Год ближайшей даты. others — упоминания других актов: дата за ними относится уже к ним."""
    lo = max(0, start - _DATE_RADIUS)
    best: tuple[int, int] | None = None  # расстояние, год
    for match in _DATE.finditer(text, lo, end + _DATE_RADIUS):
        gap = (match.end(), start) if match.end() <= start else (end, match.start())
        if any(gap[0] <= s and e <= gap[1] for s, e in others):
            continue  # «Қонун (ЎРҚ-419); ЎРҚ-1155-сон Қонун (22.06.2026)»: дата — у ЎРҚ-1155
        distance = max(0, gap[1] - gap[0])
        year = int(next(g for g in match.groups() if g))
        if best is None or distance < best[0]:
            best = (distance, year)
    return (best[1], False) if best else (default_year, True)
