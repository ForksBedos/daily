"""RSS lex.uz → справочник правовых актов (legal_acts). В items ничего не пишется.

Описание записи укладывается в два шаблона:
  «<Вид и издатель> №<номер>. Дата принятия ДД.ММ.ГГГГ. Дата вступления в силу ДД.ММ.ГГГГ»
  «<Вид и издатель> рег. № МЮ <номер>. Дата принятия …»
pubDate в ленте искусственный, даты берём из описания.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from uzdigest.config import Source
from uzdigest.legal import doc_key
from uzdigest.models import LegalAct, RawItem
from uzdigest.sources import rss
from uzdigest.sources.base import FetchContext, FetchOutcome, SourceError
from uzdigest.web.http import HttpClient

DESCRIPTION = re.compile(
    r"^(?P<kind>.+?)\s+(?:рег\.\s*№\s*МЮ\s*(?P<mj>[\d-]+)|№\s*(?P<number>\S+?))\.\s*"
    r"Дата принятия\s+(?P<adopted>\d{2}\.\d{2}\.\d{4})"
    r"(?:\.\s*Дата вступления в силу\s+(?P<effective>\d{2}\.\d{2}\.\d{4}))?"
)
LEX_ID = re.compile(r"/docs/(\d+)")


class LexParseError(ValueError):
    """Описание акта не подходит ни под один шаблон."""


def fetch(http: HttpClient, source: Source, ctx: FetchContext) -> FetchOutcome:
    outcome = rss.fetch(http, source, ctx)
    entries, outcome.items = outcome.items, []  # в items lex.uz не попадает
    for entry in entries:
        try:
            outcome.acts.append(parse_act(entry))
        except ValueError as exc:  # LexParseError, а также невозможная дата в описании
            outcome.warnings.append(f"{exc} ({entry.url})")
    if entries and not outcome.acts:
        raise SourceError(f"ни одно описание lex.uz не разобрано: {outcome.warnings[0]}")
    return outcome


def parse_act(entry: RawItem) -> LegalAct:
    description = entry.lead or ""
    match = DESCRIPTION.match(description)
    if not match:
        raise LexParseError(f"описание не по шаблону: {description[:120]!r} ({entry.url})")
    adopted = _date(match["adopted"])
    if match["mj"]:
        doc_type, number = doc_key.MJ, match["mj"]
    else:
        doc_type = doc_key.kind_code(match["kind"])
        if doc_type is None:
            raise LexParseError(f"вид акта не распознан: {match['kind']!r} ({entry.url})")
        number = doc_key.normalize_number(doc_type, match["number"])
    lex_id = LEX_ID.search(entry.url)
    return LegalAct(
        doc_key=doc_key.make_doc_key(doc_type, number, adopted.year),
        doc_type=doc_type,
        number=number,
        issuer=match["kind"].strip(),
        title=entry.title,
        adopted_on=adopted,
        effective_on=_date(match["effective"]) if match["effective"] else None,
        lex_id=int(lex_id.group(1)) if lex_id else None,
        lex_url=entry.url,
        source="lex_rss",
    )


def _date(value: str) -> date:
    return datetime.strptime(value, "%d.%m.%Y").date()
