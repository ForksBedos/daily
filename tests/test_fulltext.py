from datetime import date

import pytest
from conftest import fixture_bytes

from uzdigest.config import load_sources
from uzdigest.extract.fulltext import ExtractError, clean_text, extract
from uzdigest.sources import cbu_rates
from uzdigest.sources.base import SourceError

SOURCES = {s.code: s for s in load_sources()}


def article(code, fixture, url):
    raw = extract(fixture_bytes(fixture), url)
    source = SOURCES[code]
    text = clean_text(
        raw.text, title=raw.title, drop_lines=source.text_drop_lines, stop_lines=source.text_stop_lines
    )
    return raw, text


@pytest.mark.parametrize(
    ("code", "fixture", "url", "title", "day"),
    [
        (
            "buxgalter",
            "buxgalter_article.html",
            "https://buxgalter.uz/ru/publish/doc/text217829_perepis_biznesa",
            "Перепись бизнеса – 2026: кого и как будут регистрировать",
            date(2026, 10, 16),
        ),
        (
            "cbu",
            "cbu_article.html",
            "https://cbu.uz/ru/press_center/news/4793091/",
            "Центральный банк выдал первую лицензию на исламскую банковскую деятельность",
            date(2026, 10, 27),
        ),
        (
            "president",
            "president_view.html",
            "https://president.uz/ru/lists/view/9802",
            "Рабочий визит в Турцию завершился",
            date(2026, 10, 30),
        ),
        (
            "podrobno",
            "podrobno_article.html",
            "https://podrobno.uz/cat/economic/karty-uzcard-i-humo/",
            "Карты Uzcard и Humo появятся среди поддерживаемых в Apple Pay в Узбекистане",
            date(2026, 10, 28),
        ),
        (
            "uzdaily",
            "uzdaily_article.html",
            "https://www.uzdaily.uz/ru/v-uzbekistane-otmenili-znaki-pered-mobilnymi-radarami/",
            "В Узбекистане отменили знаки перед мобильными радарами",
            date(2026, 11, 2),
        ),
    ],
)
def test_article_pages(code, fixture, url, title, day):
    raw, text = article(code, fixture, url)
    assert raw.title == title
    assert raw.day == day
    assert len(text) > 300
    assert title not in text.split("\n")  # повтор заголовка убран


def test_junk_lines_removed():
    _, bux = article(
        "buxgalter", "buxgalter_article.html", "https://buxgalter.uz/ru/publish/doc/text217829_x"
    )
    assert not bux.startswith("Закрываем")
    assert "подпишитесь на наш Телеграм-канал" not in bux
    assert "ПКМ №557 от 09.10.2026" in bux

    _, cbu = article("cbu", "cbu_article.html", "https://cbu.uz/ru/press_center/news/4793091/")
    assert cbu.startswith("В соответствии с постановлением Правления Центрального банка")
    assert "Дата обновления" not in cbu
    assert "звуковое сопровождение" not in cbu.lower()

    _, president = article("president", "president_view.html", "https://president.uz/ru/lists/view/9802")
    assert president.startswith("Президент Республики Узбекистан Шавкат Мирзиёев завершил")


def test_clean_text_rules():
    text = "Заголовок\n  Дата: 1  \n\nАбзац один\nРеклама\nАбзац два\nПодвал\nещё подвал"
    cleaned = clean_text(text, title="заголовок", drop_lines=["^Дата:", "^Реклама$"], stop_lines=["^Подвал"])
    assert cleaned == "Абзац один\nАбзац два"


def test_page_without_article_is_an_error():
    with pytest.raises(ExtractError):
        extract(b"<html><body></body></html>", "https://example.uz/x")


def test_cbu_rates():
    rates = {r.ccy: r for r in cbu_rates.parse(fixture_bytes("cbu_rates.json"))}
    assert len(rates) == 74
    usd = rates["USD"]
    assert (usd.date, usd.rate, usd.diff, usd.nominal) == (date(2026, 10, 31), 12023.46, -42.76, 1)
    assert rates["RUB"].rate == 149.44


def test_cbu_rates_bad_payload():
    with pytest.raises(SourceError):
        cbu_rates.parse(b'{"error": "maintenance"}')
