"""Курсы ЦБ: JSON cbu.uz/ru/arkhiv-kursov-valyut/json/ — все валюты на дату последнего курса.

Курсы на конкретную дату: …/json/all/ГГГГ-ММ-ДД/ (проверено 02.11.2026).
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ValidationError

from uzdigest.models import FxRate
from uzdigest.sources.base import SourceError
from uzdigest.web.http import HttpClient


class _Rate(BaseModel):
    Ccy: str
    Nominal: str
    Rate: str
    Diff: str | None = None
    Date: str


def fetch(http: HttpClient, url: str) -> list[FxRate]:
    return parse(http.fetch(url).content)


def parse(content: bytes) -> list[FxRate]:
    try:
        rows = [_Rate.model_validate(row) for row in json.loads(content)]
        rates = [
            FxRate(
                date=datetime.strptime(row.Date, "%d.%m.%Y").date(),
                ccy=row.Ccy,
                nominal=int(row.Nominal),
                rate=float(row.Rate),
                diff=float(row.Diff) if row.Diff else None,
            )
            for row in rows
        ]
    except (ValueError, TypeError, ValidationError) as exc:
        raise SourceError(f"курсы ЦБ не разобраны: {type(exc).__name__}: {exc}") from exc
    if not rates:
        raise SourceError("курсы ЦБ: пустой список")
    return rates
