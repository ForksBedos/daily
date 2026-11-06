"""Процесс сверки упоминаний актов с lex.uz (docs/plan/stage-4-lex.md, «Процесс сверки»)."""

from datetime import UTC, date, datetime, timedelta

from conftest import FakeHttp

from uzdigest.config import LexSettings, Source
from uzdigest.db import repo
from uzdigest.extract.normalize import to_item
from uzdigest.legal.doc_key import find_refs
from uzdigest.legal.lex_doc import act_link
from uzdigest.legal.lex_search import search_url
from uzdigest.legal.verify import LexVerifier
from uzdigest.models import LegalAct, RawItem

T0 = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
SETTINGS = LexSettings()

PP387 = "Постановлением Президента от 27.10.2026 г. № ПП-387 упорядочено использование водохранилищ."
PKM594 = "Постановлением Кабмина от 28.10.2026 г. № 594 утвержден порядок подготовки кадров."
MJ1010 = "ЦБ признал утратившим силу положение о расчётах (рег. № МЮ 1010-2 от 29.10.2026)."
UP240 = "Указом Президента от 01.11.2026 г. № УП-240 утверждена программа."
DRAFT = "На обсуждение вынесен проект постановления ПКМ № 612 от 30.10.2026."

SEARCH = {
    "PP-387-2026": (search_url("PP", "387", 2026), "lex_search_pp387_query.html"),
    "PKM-594-2026": (search_url("PKM", "594", 2026), "lex_search_pkm594.html"),
    "MJ-1010-2": (search_url("MJ", "1010-2", None), "lex_search_mj1010.html"),
    "UP-240-2026": (search_url("UP", "240", 2026), "lex_search_empty.html"),
}
PAGES = {
    "https://lex.uz/ru/docs/8559901": "lex_doc_pp387.html",
    "https://lex.uz/ru/docs/8562875": "lex_doc_pkm594_uz.html",
    "https://lex.uz/ru/docs/8561143": "lex_doc_mj1010.html",
}
ROUTES = {**dict(SEARCH.values()), **PAGES}


def setup_db(*texts, published_at=T0):
    """Материалы источника уровня A, прошедшие префильтр, с упоминаниями актов в item_legal_refs."""
    conn = repo.connect(":memory:")
    [source_id] = repo.sync_sources(
        conn, [Source(code="norma", name="norma", kind="rss", url="https://norma.uz/rss", tier="A")]
    ).values()
    run_id = repo.start_run(conn, "collect")
    with conn:
        for n, text in enumerate(texts):
            raw = RawItem(f"https://norma.uz/{n}", f"Новость {n}", None, text, None, published_at)
            repo.insert_item(conn, source_id, run_id, to_item(raw, "ru", published_at))
            item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
            repo.save_prefilter(conn, item_id, True, "tier:A")
            repo.save_legal_refs(conn, item_id, find_refs(text, published_at.year))
    return conn


def verify(conn, http, now=T0 + timedelta(hours=1)):
    verifier = LexVerifier(conn, http, SETTINGS, now=now)
    return verifier.verify(repo.refs_to_verify(conn, T0 - timedelta(days=1), T0 + timedelta(days=1)))


def statuses(conn):
    return dict(conn.execute("SELECT doc_key, verify_status FROM item_legal_refs").fetchall())


def rss_act(**extra):
    """ПП-387 в справочнике, как его записывает RSS lex.uz: без версий на других языках."""
    return LegalAct(
        doc_key="PP-387-2026",
        doc_type="PP",
        number="387",
        issuer="Постановление Президента Республики Узбекистан",
        title="О мерах по упорядочению использования территорий водохранилищ",
        adopted_on=date(2026, 10, 27),
        effective_on=date(2026, 10, 28),
        lex_id=8559901,
        lex_url="https://lex.uz/ru/docs/8559901",
        source="lex_rss",
        **extra,
    )


def test_complete_act_in_reference_needs_no_network():
    conn = setup_db(PP387)
    repo.upsert_legal_act(conn, rss_act(uz_lex_id=8559898, uz_latn_lex_id=-8559898, en_lex_id=8563448))
    http = FakeHttp({})
    stats = verify(conn, http)
    assert statuses(conn) == {"PP-387-2026": "verified"}
    assert http.calls == [] and stats.errors == []


def test_act_from_rss_gets_language_versions_from_its_page():
    conn = setup_db(PP387)
    repo.upsert_legal_act(conn, rss_act())
    http = FakeHttp(ROUTES)
    verify(conn, http)
    assert http.urls() == ["https://lex.uz/ru/docs/8559901"]  # без поиска: акт уже в справочнике
    act = repo.legal_act(conn, "PP-387-2026")
    assert (act["uz_lex_id"], act["uz_latn_lex_id"], act["en_lex_id"]) == (8559898, -8559898, 8563448)
    assert act["source"] == "lex_rss"
    assert act_link(act, "en") == ("https://lex.uz/en/docs/8563448", "en")


def test_fresh_act_from_rss_gets_uzbek_versions_from_search():
    # Страница УП-236 через 5 дней после принятия: ни ссылок на другие языки, ни даты вступления в силу
    conn = setup_db("Принят Указ Президента от 28.10.2026 г. № УП-236 о социальной поддержке.")
    repo.upsert_legal_act(
        conn,
        LegalAct(
            doc_key="UP-236-2026", doc_type="UP", number="236", issuer="Указ Президента",
            title="О мерах по социальной поддержке населения", adopted_on=date(2026, 10, 28),
            effective_on=date(2026, 10, 30), lex_id=8563561, lex_url="https://lex.uz/ru/docs/8563561",
            source="lex_rss",
        ),
    )  # fmt: skip
    up236 = search_url("UP", "236", 2026)
    http = FakeHttp(
        {"https://lex.uz/ru/docs/8563561": "lex_doc_up236_fresh.html", up236: "lex_search_up236.html"}
    )
    verify(conn, http)
    assert http.urls() == ["https://lex.uz/ru/docs/8563561", up236]
    act = repo.legal_act(conn, "UP-236-2026")
    assert (act["uz_lex_id"], act["uz_latn_lex_id"], act["en_lex_id"]) == (8563559, -8563559, None)
    assert (act["effective_on"], act["source"]) == ("2026-10-30", "lex_rss")  # дата из RSS не стёрта
    assert act_link(act, "uz") == ("https://lex.uz/uz/docs/-8563559", "uz")


def test_act_found_by_search_is_added_to_reference():
    conn = setup_db(PP387)
    http = FakeHttp(ROUTES)
    stats = verify(conn, http)
    assert statuses(conn) == {"PP-387-2026": "verified"}
    assert http.urls() == [SEARCH["PP-387-2026"][0], "https://lex.uz/ru/docs/8559901"]
    act = repo.legal_act(conn, "PP-387-2026")
    assert (act["source"], act["lex_id"], act["adopted_on"], act["effective_on"]) == (
        "lex_search", 8559901, "2026-10-27", "2026-10-28"
    )  # fmt: skip
    assert act["title"].startswith("О мерах по упорядочению")
    assert (stats.requests, stats.cached, stats.by_key) == (2, 0, {"PP-387-2026": {"verified": 1}})


def test_act_published_in_uzbek_only_is_verified():
    conn = setup_db(PKM594, MJ1010)
    verify(conn, FakeHttp(ROUTES))
    assert statuses(conn) == {"PKM-594-2026": "verified", "MJ-1010-2": "verified"}
    pkm = repo.legal_act(conn, "PKM-594-2026")
    assert (pkm["lex_id"], pkm["uz_lex_id"], pkm["effective_on"]) == (None, 8562875, None)
    assert act_link(pkm, "ru") == ("https://lex.uz/uz/docs/-8562875", "uz")
    mj = repo.legal_act(conn, "MJ-1010-2")
    assert (mj["lex_id"], mj["adopted_on"]) == (8561143, "2026-10-29")


def test_missing_act_is_pending_and_then_not_found():
    conn = setup_db(UP240)
    stats = verify(conn, FakeHttp(ROUTES))
    assert statuses(conn) == {"UP-240-2026": "pending"}
    assert repo.legal_act(conn, "UP-240-2026") is None and stats.became_verified == []
    verify(conn, FakeHttp(ROUTES), now=T0 + timedelta(days=6))
    assert statuses(conn) == {"UP-240-2026": "pending"}
    verify(conn, FakeHttp(ROUTES), now=T0 + timedelta(days=7, hours=1))
    assert statuses(conn) == {"UP-240-2026": "not_found"}


def test_pending_act_that_appears_later_is_reported():
    conn = setup_db(PP387)
    verify(conn, FakeHttp({SEARCH["PP-387-2026"][0]: "lex_search_empty.html"}))
    assert statuses(conn) == {"PP-387-2026": "pending"}
    # через сутки акт появился; вчерашний ответ «не найдено» уже не из кэша
    stats = verify(conn, FakeHttp(ROUTES), now=T0 + timedelta(days=1))
    assert statuses(conn) == {"PP-387-2026": "verified"}
    assert stats.became_verified == ["PP-387-2026"]


def test_pending_from_previous_windows_is_rechecked():
    conn = setup_db(UP240, published_at=T0 - timedelta(days=3))
    with conn:
        conn.execute("UPDATE item_legal_refs SET verify_status = 'pending'")
    http = FakeHttp(ROUTES)
    verify(conn, http)  # окно T0 ± 1 сутки материал не включает
    assert http.urls() == [SEARCH["UP-240-2026"][0]]


def test_guessed_year_is_trusted_only_for_fresh_acts():
    # Даты рядом нет, год взят по дате новости. ПП-387 принят 27.10.2026 — за неделю до новости
    conn = setup_db("Ход работ обсудили на совещании. ПП-387 предусматривает расчистку водохранилищ.")
    assert conn.execute("SELECT year_guessed FROM item_legal_refs").fetchone()[0] == 1
    verify(conn, FakeHttp(ROUTES))
    assert statuses(conn) == {"PP-387-2026": "verified"}
    # ПКМ № 8 этого года принят в январе: «№ 8» без даты в ноябрьской новости может быть другим актом
    conn = setup_db("Порядок утверждён постановлением Кабмина № 8, сообщает министерство.")
    repo.upsert_legal_act(
        conn,
        LegalAct(
            doc_key="PKM-8-2026", doc_type="PKM", number="8", issuer=None, title=None,
            adopted_on=date(2026, 1, 9), effective_on=date(2026, 1, 10), lex_id=7986006, lex_url=None,
            source="lex_search", uz_lex_id=7982830, uz_latn_lex_id=-7982830, en_lex_id=7986042,
        ),
    )  # fmt: skip
    verify(conn, FakeHttp({}))
    assert statuses(conn) == {"PKM-8-2026": "pending"}


def test_recomputed_refs_replace_stale_ones_and_keep_statuses():
    conn = setup_db(PP387)
    verify(conn, FakeHttp(ROUTES))
    item_id = conn.execute("SELECT item_id FROM item_legal_refs").fetchone()[0]
    with conn:
        repo.save_legal_refs(conn, item_id, find_refs(f"{PP387} {UP240}", 2026))
    assert statuses(conn) == {"PP-387-2026": "verified", "UP-240-2026": None}
    with conn:
        repo.save_legal_refs(conn, item_id, find_refs(UP240, 2026))
    assert statuses(conn) == {"UP-240-2026": None}


def test_draft_is_not_searched():
    conn = setup_db(DRAFT)
    http = FakeHttp(ROUTES)
    verify(conn, http)
    assert statuses(conn) == {"PKM-612-2026": "draft"} and http.calls == []


def test_network_error_and_unknown_layout_give_pending_not_verified():
    conn = setup_db(PP387)
    stats = verify(conn, FakeHttp({}))
    assert statuses(conn) == {"PP-387-2026": "pending"}
    assert len(stats.errors) == 1 and "ConnectError" in stats.errors[0]

    conn = setup_db(PP387)
    stats = verify(conn, FakeHttp({SEARCH["PP-387-2026"][0]: b"<html><body>New design</body></html>"}))
    assert statuses(conn) == {"PP-387-2026": "pending"}
    assert "LexLayoutError" in stats.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM lex_lookups").fetchone()[0] == 0  # ошибка не кэшируется


def test_repeated_lookup_within_cache_time_does_not_hit_network():
    conn = setup_db(UP240, PKM594)
    first = FakeHttp(ROUTES)
    verify(conn, first)
    assert len(first.calls) == 3  # поиск UP-240, поиск ПКМ-594 и его страница
    second = FakeHttp(ROUTES)
    stats = verify(conn, second, now=T0 + timedelta(hours=10))
    assert second.calls == [] and stats.cached == 2  # страница ПКМ-594 и поиск UP-240
    assert statuses(conn) == {"UP-240-2026": "pending", "PKM-594-2026": "verified"}
