"""Поиск акта на lex.uz по виду, номеру и году (docs/plan/stage-4-lex.md).

Ищем по номеру акта (`actnum`) с фильтрами вида, органа и года: «ПКМ № 8 за 2025 год» даёт
один акт, а простой запрос «8» — все документы, где встречается это число. Язык не ограничиваем:
свежие акты Кабмина и ведомств первые дни есть только на узбекском (оригинал), русский перевод
появляется позже. В выдаче у каждой языковой версии своя строка и свой id.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from urllib.parse import urlencode

from uzdigest.legal.doc_key import MJ, PKM, PP, RP, UP, ZRU, kind_code, make_doc_key, normalize_number
from uzdigest.models import LegalAct

BASE_URL = "https://lex.uz"

# Фильтры расширенного поиска lex.uz (значения из формы поиска, проверены 02.11.2026).
# У распоряжений (form_id 588) поиск по номеру ничего не находит, поэтому для РП — только орган.
_FORM_LAW, _FORM_DECREE, _FORM_RESOLUTION = 3968, 3973, 3972
_BODY_PRESIDENT, _BODY_CABINET = 19123, 819
_FILTERS: dict[str, dict[str, int]] = {
    ZRU: {"form_id": _FORM_LAW},
    UP: {"form_id": _FORM_DECREE, "body_id": _BODY_PRESIDENT},
    PP: {"form_id": _FORM_RESOLUTION, "body_id": _BODY_PRESIDENT},
    RP: {"body_id": _BODY_PRESIDENT},
    PKM: {"form_id": _FORM_RESOLUTION, "body_id": _BODY_CABINET},
}


class LexLayoutError(ValueError):
    """Страница lex.uz не похожа на ожидаемую: вероятно, сменилась вёрстка."""


def search_url(doc_type: str, number: str, year: int | None) -> str | None:
    """Адрес поиска акта. None — вид, который поиском не ищется (Сенат, суды)."""
    if doc_type == MJ:
        params: dict[str, str | int] = {"actnum": number, "regnummj": 1}
    elif doc_type in _FILTERS and year is not None:
        params = {"actnum": number, **_FILTERS[doc_type], "fyear": year}
    else:
        return None
    return f"{BASE_URL}/ru/search/nat?{urlencode(params)}"


def doc_url(lex_id: int) -> str:
    """Страница документа с русским интерфейсом; для русской версии это и ссылка для дайджеста."""
    return f"{BASE_URL}/ru/docs/{lex_id}"


# --- Строка реквизитов: в выдаче поиска и в шапке страницы документа ---


@dataclass(frozen=True)
class Label:
    lang: str  # ru, uz_cyrl, uz_latn; other — английский перевод или строка не по шаблону
    kind: str | None  # вид и издатель: «Постановление Президента Республики Узбекистан»
    doc_type: str | None  # None — вид акта не распознан
    number: str | None
    adopted_on: date | None  # у ведомственных актов — дата регистрации в Минюсте
    doc_key: str | None


# «Постановление Президента Республики Узбекистан, от 27.10.2026 г. № ПП-387»
# «Постановление Правления Центрального банка …, зарегистрировано 29.10.2026 г., рег. номер 1010-2»
_RU_LABEL = re.compile(
    r"^(?P<kind>.+?)\s*,\s*(?:от\s+(?P<date>\d{2}\.\d{2}\.\d{4})\s*г\.\s*№\s*(?P<number>\S+)"
    r"|зарегистрировано\s+(?P<reg_date>\d{2}\.\d{2}\.\d{4})\s*г\.\s*,\s*рег\.\s*номер\s+(?P<reg>\S+))$"
)
# «Ўзбекистон Республикаси Вазирлар Маҳкамасининг қарори, 28.10.2026 йилдаги 594-сон»
# «Oʻzbekiston Respublikasi … qarori, 29.10.2026 yilda ro‘yxatdan o‘tgan, ro‘yxat raqami 1010-2»
_UZ_LABEL = re.compile(
    r"^(?P<kind>.+?)\s*,\s*(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?:(?:йилдаги|yildagi)\s+(?P<number>\S+?)-(?:сон|son)"
    r"|(?:йилда\s+рўйхатдан\s+ўтган|yilda\s+ro\W?yxatdan\s+o\W?tgan)\s*,\s*"
    r"(?:рўйхат\s+рақами|ro\W?yxat\s+raqami)\s+(?P<reg>\S+))$",
    re.I,
)
_CYRILLIC = re.compile(r"[а-яёўқғҳ]", re.I)
_UNKNOWN = Label("other", None, None, None, None, None)


def parse_label(text: str) -> Label:
    text = " ".join(html.unescape(text).split())
    if match := _UZ_LABEL.match(text):
        lang = "uz_cyrl" if _CYRILLIC.search(match["kind"]) else "uz_latn"
        adopted = match["date"]
    elif (match := _RU_LABEL.match(text)) and _CYRILLIC.search(match["kind"]):
        lang = "ru"
        adopted = match["date"] or match["reg_date"]
    else:
        return _UNKNOWN  # английские строки («Resolution of the President …») нам не нужны
    kind = match["kind"]
    adopted_on = datetime.strptime(adopted, "%d.%m.%Y").date()
    if match["reg"]:
        doc_type, number = MJ, match["reg"]
    else:
        doc_type = kind_code(kind)
        if doc_type is None:
            return Label(lang, kind, None, None, adopted_on, None)
        number = normalize_number(doc_type, match["number"])
    return Label(lang, kind, doc_type, number, adopted_on, make_doc_key(doc_type, number, adopted_on.year))


# --- Выдача поиска ---


@dataclass(frozen=True)
class SearchResult:
    total: int  # сколько найдено всего; на странице выдачи не больше 20 строк
    rows: list[tuple[int, str, str]]  # id документа, название, строка реквизитов

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> SearchResult:
        data = json.loads(text)
        return cls(data["total"], [tuple(row) for row in data["rows"]])


_ROW = re.compile(r'<tr class="dd-table__main-item">(.*?)</tr>', re.S)
_ROW_LINK = re.compile(r'<a class="lx_link\s*" href="/ru/docs/(-?\d+)[^"]*"[^>]*>(.*?)</a>', re.S)
_ROW_LABEL = re.compile(r'<span class="badge[^"]*">(.*?)</span>', re.S)
_TOTAL = re.compile(r"найдено\s+(\d+)\s+документ")
_NOT_FOUND = "refindHeader__notFound"
_TAG = re.compile(r"<[^>]+>")


def parse_search(page: str) -> SearchResult:
    rows = _ROW.findall(page)
    total = _TOTAL.search(page)
    if not rows:
        if _NOT_FOUND in page:
            return SearchResult(0, [])
        raise LexLayoutError("в выдаче поиска lex.uz нет ни строк результатов, ни «ничего не найдено»")
    if total is None:
        raise LexLayoutError("в выдаче поиска lex.uz не найдено число результатов")
    parsed = []
    for row in rows:
        link, label = _ROW_LINK.search(row), _ROW_LABEL.search(row)
        if link is None or label is None:
            raise LexLayoutError(f"строка выдачи lex.uz без ссылки или реквизитов: {row[:200]!r}")
        title = " ".join(html.unescape(_TAG.sub("", link.group(2))).split())
        parsed.append((int(link.group(1)), title, label.group(1)))
    return SearchResult(int(total.group(1)), parsed)


def find_act(result: SearchResult, doc_key: str) -> LegalAct | None:
    """Акт с этим ключом в выдаче: вид, номер и год совпадают. Реквизиты и название — русские, если есть."""
    ids: dict[str, int] = {}
    best: tuple[Label, str] | None = None
    for lex_id, title, text in result.rows:
        label = parse_label(text)
        if label.doc_key != doc_key or label.lang in ids:
            continue
        ids[label.lang] = lex_id
        if best is None or label.lang == "ru":
            best = (label, title)
    if best is None:
        return None
    label, title = best
    ru_id = ids.get("ru")
    return LegalAct(
        doc_key=doc_key,
        doc_type=label.doc_type,
        number=label.number,
        issuer=label.kind,
        title=title,
        adopted_on=label.adopted_on,
        effective_on=None,  # есть только на странице документа
        lex_id=ru_id,
        lex_url=doc_url(ru_id) if ru_id else None,
        source="lex_search",
        uz_lex_id=ids.get("uz_cyrl"),
        uz_latn_lex_id=ids.get("uz_latn"),
    )
