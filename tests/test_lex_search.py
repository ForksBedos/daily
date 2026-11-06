"""Разбор поиска и страниц документов lex.uz (docs/plan/stage-4-lex.md). Фикстуры сохранены 02.11.2026."""

from datetime import date

import pytest
from conftest import fixture_bytes

from uzdigest.legal.doc_key import kind_code, split_doc_key
from uzdigest.legal.lex_doc import DocHeader, act_link, parse_doc
from uzdigest.legal.lex_search import (
    LexLayoutError,
    SearchResult,
    find_act,
    parse_label,
    parse_search,
    search_url,
)


def search(name):
    return parse_search(fixture_bytes(name).decode("utf-8"))


def doc(name):
    return parse_doc(fixture_bytes(name).decode("utf-8"))


@pytest.mark.parametrize(
    ("text", "lang", "doc_key", "adopted"),
    [
        ("Постановление Президента Республики Узбекистан, от 27.10.2026 г. № ПП-387", "ru", "PP-387-2026",
         date(2026, 10, 27)),
        # пробел перед запятой встречается в старых актах
        ("Постановление Кабинета Министров Республики Узбекистан , от 16.07.2019 г. № 594", "ru",
         "PKM-594-2019", date(2019, 7, 16)),
        ("Постановление Правления Центрального банка Республики Узбекистан, зарегистрировано 29.10.2026 г., "
         "рег. номер 1010-2", "ru", "MJ-1010-2", date(2026, 10, 29)),
        ("Ўзбекистон Республикаси Вазирлар Маҳкамасининг қарори, 28.10.2026 йилдаги 594-сон", "uz_cyrl",
         "PKM-594-2026", date(2026, 10, 28)),
        ("Oʻzbekiston Respublikasi Prezidentining Farmoni, 28.10.2026 yildagi PF-236-son", "uz_latn",
         "UP-236-2026", date(2026, 10, 28)),
        ("Oʻzbekiston Respublikasining Konstitutsiyaviy qonuni, 14.10.2026 yildagi O‘RQ-1181-son", "uz_latn",
         "ZRU-1181-2026", date(2026, 10, 14)),
        ("Ўзбекистон Республикаси Президентининг қарори, 27.10.2026 йилдаги ПҚ-387-сон", "uz_cyrl",
         "PP-387-2026", date(2026, 10, 27)),
        ("Oʻzbekiston Respublikasi Markaziy banki boshqaruvining qarori, 29.10.2026 yilda ro‘yxatdan o‘tgan, "
         "ro‘yxat raqami 1010-2", "uz_latn", "MJ-1010-2", date(2026, 10, 29)),
        # вид не из тех, что мы ищем, — реквизиты есть, ключа нет
        ("Постановление Конституционного суда Республики Узбекистан, от 22.09.2026 г. № ПКС-8", "ru", None,
         date(2026, 9, 22)),
        # английский перевод не разбираем
        ("Resolution of the President of the Republic of Uzbekistan, от 27.10.2026 г. № RP-387", "other",
         None, None),
    ],
)  # fmt: skip
def test_parse_label(text, lang, doc_key, adopted):
    label = parse_label(text)
    assert (label.lang, label.doc_key, label.adopted_on) == (lang, doc_key, adopted)


def test_kind_code_in_uzbek_and_doc_key_split():
    assert kind_code("Ўзбекистон Республикасининг Қонуни") == "ZRU"
    assert kind_code("Oʻzbekiston Respublikasi Vazirlar Mahkamasining qarori") == "PKM"
    assert kind_code("Ўзбекистон Республикаси Президентининг Фармони") == "UP"
    assert split_doc_key("PKM-594-2026") == ("PKM", "594", 2026)
    assert split_doc_key("MJ-1010-2") == ("MJ", "1010-2", None)
    assert split_doc_key("SENATE-PK-411-V-2026") == ("SENATE", "PK-411-V", 2026)


def test_search_url_by_kind():
    assert search_url("PP", "387", 2026) == (
        "https://lex.uz/ru/search/nat?actnum=387&form_id=3972&body_id=19123&fyear=2026"
    )
    assert search_url("PKM", "594", 2026) == (
        "https://lex.uz/ru/search/nat?actnum=594&form_id=3972&body_id=819&fyear=2026"
    )
    assert search_url("MJ", "1010-2", None) == "https://lex.uz/ru/search/nat?actnum=1010-2&regnummj=1"
    assert (
        search_url("ZRU", "1181", 2026) == "https://lex.uz/ru/search/nat?actnum=1181&form_id=3968&fyear=2026"
    )
    assert search_url("SENATE", "PK-411-V", 2026) is None  # Сенат — только справочник из RSS


def test_pp387_search_picks_act_of_the_right_year():
    # Простой запрос «ПП-387» выдаёт акты 2026, 2025 и 2022 годов с тем же номером
    result = search("lex_search_pp387_query.html")
    assert (result.total, len(result.rows)) == (8, 8)
    act = find_act(result, "PP-387-2026")
    assert (act.lex_id, act.adopted_on, act.source) == (8559901, date(2026, 10, 27), "lex_search")
    assert act.lex_url == "https://lex.uz/ru/docs/8559901"
    assert act.issuer == "Постановление Президента Республики Узбекистан"
    assert act.title.startswith("О мерах по упорядочению использования территорий водохранилищ")
    assert find_act(result, "PP-387-2025").lex_id == 7963123
    assert find_act(result, "PP-387-2024") is None


def test_cabinet_search_returns_all_language_versions():
    # Без фильтра по году: 66 результатов, на странице 20 (поэтому в поиске всегда есть fyear)
    result = search("lex_search_pkm8.html")
    assert (result.total, len(result.rows)) == (66, 20)
    act = find_act(result, "PKM-8-2026")
    assert (act.lex_id, act.uz_lex_id, act.uz_latn_lex_id) == (7986006, 7982830, -7982830)
    assert act.issuer == "Постановление Кабинета Министров Республики Узбекистан"


def test_fresh_cabinet_act_is_found_in_uzbek_only():
    # ПКМ № 594 от 28.10.2026: на lex.uz только узбекский оригинал, русского перевода ещё нет
    act = find_act(search("lex_search_pkm594.html"), "PKM-594-2026")
    assert (act.lex_id, act.lex_url, act.uz_lex_id, act.uz_latn_lex_id) == (None, None, 8562875, -8562875)
    assert act.issuer == "Ўзбекистон Республикаси Вазирлар Маҳкамасининг қарори"


def test_ministry_act_by_justice_registration_number():
    act = find_act(search("lex_search_mj1010.html"), "MJ-1010-2")
    # знак id не означает алфавит: здесь отрицательный id у кириллицы
    assert (act.lex_id, act.uz_lex_id, act.uz_latn_lex_id) == (8561143, -8561230, 8561230)
    assert (act.doc_type, act.number, act.adopted_on) == ("MJ", "1010-2", date(2026, 10, 29))


def test_decree_and_constitutional_law():
    up = find_act(search("lex_search_up236.html"), "UP-236-2026")
    assert (up.lex_id, up.uz_lex_id, up.uz_latn_lex_id) == (8563561, 8563559, -8563559)
    # в выдаче есть и английский перевод («Constitution Law …»): он пропускается
    zru = find_act(search("lex_search_zru1181.html"), "ZRU-1181-2026")
    assert (zru.lex_id, zru.issuer) == (8543217, "Конституционный Закон Республики Узбекистан")


def test_empty_and_broken_search_pages():
    assert search("lex_search_empty.html") == SearchResult(0, [])
    with pytest.raises(LexLayoutError):
        parse_search("<html><body><div class='results'>Новая вёрстка</div></body></html>")


def test_search_result_json_roundtrip():
    result = search("lex_search_up236.html")
    assert SearchResult.from_json(result.to_json()) == result


def test_pp387_document_page():
    header = doc("lex_doc_pp387.html")
    assert header.parsed_label().doc_key == "PP-387-2026"
    assert header.effective_on() == date(2026, 10, 28)
    assert header.version_ids(8559901) == {
        "ru": 8559901,
        "uz_cyrl": 8559898,
        "uz_latn": -8559898,
        "en": 8563448,
    }
    assert DocHeader.from_json(header.to_json()) == header


def test_pages_without_translations_or_effective_date():
    uz_only = doc("lex_doc_pkm594_uz.html")
    assert (uz_only.parsed_label().doc_key, uz_only.effective_on()) == ("PKM-594-2026", None)
    assert uz_only.version_ids(8562875) == {"uz_cyrl": 8562875}
    mj = doc("lex_doc_mj1010.html")
    assert (mj.parsed_label().doc_key, mj.effective_on(), mj.version_ids(8561143)) == (
        "MJ-1010-2",
        None,
        {"ru": 8561143},
    )
    with pytest.raises(LexLayoutError):
        parse_doc("<html><body>Документ не найден</body></html>")


def test_links_by_digest_language():
    full = {"lex_id": 8559901, "uz_lex_id": 8559898, "uz_latn_lex_id": -8559898, "en_lex_id": 8563448}
    assert act_link(full, "ru") == ("https://lex.uz/ru/docs/8559901", "ru")
    assert act_link(full, "uz") == ("https://lex.uz/uz/docs/-8559898", "uz")
    assert act_link(full, "en") == ("https://lex.uz/en/docs/8563448", "en")
    ru_only = {"lex_id": 8561143, "uz_lex_id": None, "uz_latn_lex_id": None, "en_lex_id": None}
    assert act_link(ru_only, "en") == ("https://lex.uz/ru/docs/8561143", "ru")  # «(in Russian)»
    assert act_link(ru_only, "uz") == ("https://lex.uz/ru/docs/8561143", "ru")
    uz_cyrl_only = {"lex_id": None, "uz_lex_id": 8562875, "uz_latn_lex_id": None, "en_lex_id": None}
    assert act_link(uz_cyrl_only, "ru") == ("https://lex.uz/docs/8562875", "uz")
    assert act_link(uz_cyrl_only, "en") == ("https://lex.uz/docs/8562875", "uz")
