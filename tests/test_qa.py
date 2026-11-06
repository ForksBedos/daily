"""Проверка фактов (llm/qa.py): нормализация чисел и дат на трёх языках."""

import pytest

from uzdigest.llm.qa import extract, mismatched, unsupported


@pytest.mark.parametrize(
    ("ru", "other", "lang"),
    [
        ("Выплата 1,5 млн сумов", "Payment of UZS 1.5 million", "en"),
        ("Выплата 1,5 млн сумов", "1,5 mln so‘m to‘lanadi", "uz"),
        ("Выплата 1 500 000 сумов", "Payment of UZS 1.5 million", "en"),
        ("Курс 12 023,46 сума", "The rate is 12,023.46 soums", "en"),
        ("Курс 12 023,46 сума", "Kurs 12 023,46 so‘m", "uz"),
        ("Ставка 14 %", "The rate is 14%", "en"),
        ("Ставка 14 %", "Stavka 14 foiz", "uz"),
        ("Принят 28.10.2026", "Adopted on 28 October 2026", "en"),
        ("Принят 28 октября 2026 года", "Adopted on October 28, 2026", "en"),
        ("Принят 28 октября 2026 года", "2026-yil 28-oktabrda qabul qilindi", "uz"),
        ("С 1 ноября", "From 1 November 2026", "en"),  # дата без года совпадает с любой
        ("Указ № УП-236 и ПКМ № 594", "Decree No. UP-236 and Resolution No. 594", "en"),
        ("Указ № УП-236 и ПКМ № 594", "PF-236-son Farmon va VMQ-594", "uz"),
        ("Выросло в 2 раза за 3 года", "Doubled over three years", "en"),  # числа до 10 словами
        ("$1,2 млрд и 150 тыс. человек", "$1.2 billion and 150,000 people", "en"),
        # множитель только у второго числа диапазона
        (
            "Разница сократилась с $7,79 до $6,6 млрд",
            "The gap narrowed from $7.79 billion to $6.6 billion",
            "en",
        ),
        ("Разница сократилась с $7,79 до $6,6 млрд", "Farq 7,79 mlrd dollardan 6,6 mlrd dollargacha", "uz"),
        ("От 100 до 500 млрд сумов", "Between UZS 100 billion and UZS 500 billion", "en"),
        ("От 100 до 500 млрд сумов", "100 dan 500 mlrd so‘mgacha", "uz"),
        ("280–300 тысяч человек", "280,000-300,000 people", "en"),
        ("280–300 тысяч человек", "280–300 ming kishi", "uz"),
        ("Купюры 100 и 200 тысяч сумов", "100,000 and 200,000 soum banknotes", "en"),
        # начало диапазона без множителя может быть полным значением
        ("От 14 600 до 20 тыс. сумов", "From 14,600 to 20,000 soums", "en"),
        ("С 300 до 1,5 тыс. тонн", "From 300 to 1,500 tonnes", "en"),
        # узбекские множители с окончаниями
        ("Программа на 7,8 миллиарда долларов", "Dastur 7,8 milliard dollarlik", "uz"),
        ("Более 3,2 тысячи специалистов", "3,2 mingdan ortiq mutaxassis", "uz"),
        ("551,1 тыс. предприятий", "551,1 mingtasi korxonalar", "uz"),
        ("Более 3,2 тысячи специалистов", "3,2 мингдан ортиқ мутахассис", "uz"),
        ("$1,5 млрд и $500 млн", "$1.5bn and $500mn", "en"),
        # одно значение разными словами: погрешность умножения не мешает
        ("0,54 трлн сумов", "UZS 540 billion", "en"),
        ("2,01 трлн сумов", "2 010 mlrd so‘m", "uz"),
        # номер после даты — не год
        ("рег. МЮ № 3966 от 31.10.2026", "2026-yil 31-oktabrdagi 3966-son bilan ro‘yxatdan o‘tgan", "uz"),
    ],
)
def test_same_facts_in_translation(ru, other, lang):
    assert mismatched(ru, other, lang) == []


def test_translation_mismatches_are_reported():
    problems = mismatched("Ставка 14 % с 1 ноября 2026 года", "The rate is 15% from 2 November 2026", "en")
    assert problems == ["нет 14", "нет 01.11.2026", "лишнее 15", "лишнее 02.11.2026"]


def test_invented_facts_are_caught():
    source = "Постановление Кабинета Министров от 28.10.2026 г. № 594. Доля государства 50 % или более."
    assert unsupported("ПКМ № 594 от 28.10.2026 касается компаний с долей от 50 %", source) == []
    assert unsupported("С 1 ноября компании заплатят 2 млн сумов по ПКМ № 594", source) == [
        "2 млн",
        "1 ноября",
    ]
    # реквизиты акта — тоже источник
    assert unsupported("Вступил в силу 30.10.2026", source, "594 28.10.2026 30.10.2026") == []


def test_uzbek_cyrillic_source_dates_and_years():
    facts = extract("Президентнинг 2026 йил 21 октябрдаги ПҚ-387-сон қарори, 2025 йилда")
    assert (21, 10, 2026) in facts.dates
    assert set(facts.numbers) == {387.0, 2025.0}


def test_dates_are_not_counted_as_numbers():
    facts = extract("от 28.10.2026 г. № 594, с 1 января 2027 года")
    assert set(facts.numbers) == {594.0}
    assert set(facts.dates) == {(28, 10, 2026), (1, 1, 2027)}


@pytest.mark.parametrize(
    ("answer", "source", "problems"),
    [
        ("Штраф 1,5 млн сумов", "Штраф 1,5 тыс. сумов", ["1,5 млн"]),
        ("Взнос 5 000 сумов", "Ставка 5 %", ["5 000"]),
        ("Штраф 150 сумов", "Штраф 150 тыс. сумов", ["150"]),  # множитель потерян
        ("Штраф 1,5 млн сумов", "Штраф 1 500 сумов", ["1,5 млн"]),
        # диапазон не спасает другой множитель
        ("От 100 до 500 млн сумов", "от 100 млрд до 500 млрд сумов", ["100", "500 млн"]),
    ],
)
def test_number_a_thousand_times_off_is_not_confirmed(answer, source, problems):
    assert unsupported(answer, source) == problems


@pytest.mark.parametrize(
    ("ru", "other", "lang", "problems"),
    [
        ("Штраф 1,5 млн сумов", "A fine of UZS 1.5 thousand", "en", ["нет 1,5 млн", "лишнее 1.5 thousand"]),
        ("Штраф 1,5 млн сумов", "Jarima 1,5 ming so‘m", "uz", ["нет 1,5 млн", "лишнее 1,5 ming"]),
    ],
)
def test_translation_a_thousand_times_off_is_reported(ru, other, lang, problems):
    assert mismatched(ru, other, lang) == problems


@pytest.mark.parametrize(
    ("answer", "source"),
    [
        ("От 100 до 500 млрд сумов", "от 100 млрд до 500 млрд сумов"),
        ("Отрицательная разница сократилась с $7,79 млрд", "с $7,79 до $6,6 млрд"),
        ("Заём на 2 млн сумов", "от 2 до 100 миллионов сумов"),  # число до 10 — начало диапазона
        ("Бензин стоит от 14 600 сумов", "от 14 600 до 20 тыс. сумов"),  # начало — полное значение
        ("Объём был 300 тонн", "примерно с 300 до 1,5 тыс. тонн"),
    ],
)
def test_range_with_one_multiplier_confirms_both_readings(answer, source):
    assert unsupported(answer, source) == []
