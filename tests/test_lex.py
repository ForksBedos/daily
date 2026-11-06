from collections import Counter
from datetime import date

import pytest
from conftest import FakeHttp, fixture_bytes

from uzdigest.collect import collect
from uzdigest.config import Source
from uzdigest.db import repo
from uzdigest.legal.doc_key import kind_code, make_doc_key, normalize_number
from uzdigest.models import RawItem
from uzdigest.sources import rss
from uzdigest.sources.lex_rss import LexParseError, parse_act


@pytest.fixture(scope="module")
def acts():
    return [parse_act(e) for e in rss.parse(fixture_bytes("lex.xml"), "https://lex.uz/ru/rss")]


def test_all_feed_descriptions_parse_with_expected_distribution(acts):
    assert len(acts) == 89
    # docs/plan/stage-2-sources.md: 32 ПКМ, 11 ПП, 4 УП, 4+1 закона (один конституционный),
    # 4+1 актов Сената (одно — Кенгаша), ведомственные акты с регистрацией в Минюсте, одно решение суда
    assert Counter(a.doc_type for a in acts) == {
        "PKM": 32, "MJ": 31, "PP": 11, "ZRU": 5, "SENATE": 5, "UP": 4, "COURT": 1,
    }  # fmt: skip
    assert len({a.doc_key for a in acts}) == 89
    assert all(a.adopted_on and a.lex_id and a.lex_url.startswith("https://lex.uz/ru/docs/") for a in acts)


@pytest.mark.parametrize(
    ("doc_key", "adopted", "effective", "lex_id"),
    [
        ("ZRU-1184-2026", date(2026, 10, 21), date(2027, 4, 22), 8549958),
        ("UP-236-2026", date(2026, 10, 28), date(2026, 10, 30), 8563561),
        ("PP-387-2026", date(2026, 10, 27), date(2026, 10, 28), 8559901),
        ("PKM-588-2026", date(2026, 10, 22), date(2027, 1, 1), 8559660),
        ("MJ-1010-2", date(2026, 10, 29), date(2026, 10, 29), 8561143),
        ("SENATE-PK-411-V-2026", date(2026, 10, 14), date(2026, 10, 14), 8562470),
    ],
)
def test_doc_keys_from_plan(acts, doc_key, adopted, effective, lex_id):
    act = {a.doc_key: a for a in acts}[doc_key]
    assert (act.adopted_on, act.effective_on, act.lex_id) == (adopted, effective, lex_id)


def test_issuer_and_title_are_kept(acts):
    act = {a.doc_key: a for a in acts}["MJ-1010-2"]
    assert act.issuer == "Постановление Правления Центрального банка Республики Узбекистан"
    assert act.title.startswith("О признании утратившим силу")


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("Конституционный Закон Республики Узбекистан", "ZRU"),
        ("Указ Президента Республики Узбекистан", "UP"),
        ("Распоряжение Президента Республики Узбекистан", "RP"),
        ("Постановление Кенгаша Сената Олий Мажлиса Республики Узбекистан", "SENATE"),
        ("Приказ министра юстиции Республики Узбекистан", None),
    ],
)
def test_kind_code(kind, code):
    assert kind_code(kind) == code


def test_number_normalization_across_scripts():
    assert normalize_number("UP", "УП-236") == normalize_number("UP", "PF-236") == "236"
    assert normalize_number("PP", "ПҚ-387") == "387"
    assert normalize_number("ZRU", "O‘RQ-1184") == "1184"
    assert normalize_number("SENATE", "ПС-377-V") == "PS-377-V"
    assert make_doc_key("MJ", "3009-4", None) == "MJ-3009-4"
    with pytest.raises(ValueError):
        make_doc_key("PP", "387", None)


def test_unknown_description_is_an_error():
    entry = RawItem("https://lex.uz/ru/docs/1", "Т", None, "Что-то без номера и даты", None, None)
    with pytest.raises(LexParseError):
        parse_act(entry)


def test_collect_writes_acts_not_items():
    source = Source(code="lex", name="lex.uz", kind="lex_rss", url="https://lex.uz/ru/rss", tier="A")
    conn = repo.connect(":memory:")
    http = FakeHttp({"https://lex.uz/ru/rss": "lex.xml"})

    [first] = collect(conn, http, [source])
    assert (first.status, first.fetched, first.new) == ("ok", 89, 89)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    row = conn.execute("SELECT * FROM legal_acts WHERE doc_key = 'UP-236-2026'").fetchone()
    assert (row["doc_type"], row["number"], row["source"]) == ("UP", "236", "lex_rss")
    assert (row["adopted_on"], row["effective_on"]) == ("2026-10-28", "2026-10-30")

    [second] = collect(conn, http, [source])
    assert second.new == 0
    assert conn.execute("SELECT COUNT(*) FROM legal_acts").fetchone()[0] == 89
