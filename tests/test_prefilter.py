import pytest

from uzdigest.config import Source, load_sources
from uzdigest.filter.prefilter import Keywords, prefilter

KEYWORDS = Keywords.load()
SOURCES = {s.code: s for s in load_sources()}


def check(source, title, text="", url="https://example.uz/x", categories=()):
    return prefilter(
        title=title,
        text=text,
        url=url,
        categories=list(categories),
        source=SOURCES[source],
        keywords=KEYWORDS,
    )


@pytest.mark.parametrize("source", ["soliq", "imv", "cbu", "president", "norma", "buxgalter"])
def test_tiers_a_and_b_always_pass(source):
    verdict = check(source, "Футбольный матч и погода на выходные", categories=["Спорт"])
    assert verdict.passed and verdict.reason == "tier"


@pytest.mark.parametrize(
    "title",
    [
        "Хусанов отыграл весь матч, но Манчестер Сити уступил Брайтону",
        "Какая погода ожидается в Узбекистане 31 октября",
        "В Джизакской области убили заведующую детским садом",
        "Шедевры Чайковского прозвучали в Ташкенте",
        "Жавохир Синдаров выиграл турнир Clutch Chess",
        "Милан устроил Интеру голевую встряску в дерби",
    ],
)
def test_stop_topics_filtered(title):
    verdict = check("kun", title, text="Подробности в нашем материале.")
    assert not verdict.passed and verdict.reason.startswith("topic:")


def test_topic_word_near_the_start_rescues_stop_topic():
    assert check("kun", "Хокимият направит футбольному клубу 5 млрд сумов").passed  # «сумов» в заголовке
    lead = "Банк выделил клубу кредит на строительство стадиона."
    assert check("kun", "Футбольный клуб построит стадион", text=lead).passed
    far = "Клуб сыграл вничью. " * 30 + "Спонсор — банк."
    assert not check("kun", "Футбольный клуб сыграл вничью", text=far).passed


def test_stop_rubric_by_category_and_url():
    assert check("daryo", "Узбекистанки завоевали две медали", categories=["Спорт"]).reason == "rubric:Спорт"
    url = "https://podrobno.uz/cat/proisshestviya/v-tashkente-spasli/"
    assert check("podrobno", "В Ташкенте спасли девушку", url=url).reason == "rubric:/cat/proisshestviya/"
    # Тематическое слово в заголовке спасает материал из стоп-рубрики
    assert check("podrobno", "Застройщика обвинили в мошенничестве с кредитами", url=url).passed


def test_ordinary_news_passes():
    verdict = check("uzdaily", "Узбекистан и Сингапур обсудили сотрудничество в фармацевтике")
    assert verdict.passed and verdict.reason == "pass"


def test_exact_and_prefix_entries():
    keywords = Keywords(topic=["ндс$", "налог"], stop_topics=["гол$"], stop_categories=[])
    assert keywords.topic.first("Ставка НДС снижена") == "ндс"
    assert keywords.topic.first("ндсу не платят") is None
    assert keywords.topic.first("Налоговики проверили") == "налоговики"
    assert keywords.stop_topics.first("Голосование прошло") is None
    source = Source(code="x", name="x", kind="rss", url="https://x.uz", tier="C")
    assert not prefilter(
        title="Красивый гол", text="", url="", categories=[], source=source, keywords=keywords
    ).passed
