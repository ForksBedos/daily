"""Генератор постов (этап 7): профили, темы, проверка поста, повтор, кэш и дневной лимит. Claude подменён."""

import json
from datetime import UTC, datetime

import pytest
from test_llm import LLM
from test_render import sample_digest

from uzdigest.config import HttpSettings, PostsSettings, Settings
from uzdigest.db import repo
from uzdigest.llm.client import LlmBudget, LlmResult
from uzdigest.llm.schemas import PostResponse
from uzdigest.posts import qa
from uzdigest.posts.generate import PostError, PostLimitReached, find_article, list_topics, make_post
from uzdigest.posts.profiles import load_profiles
from uzdigest.render.model import LANGS, Text

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
PROFILES = load_profiles()
URL = "https://norma.uz/a1"
GOOD = (
    "**Семьям выплатят по 1 млн сумов**\n\n"
    "Указ № УП-236 от 28.10.2026: помощь получат семьи из Социального реестра, "
    "выплаты — до 30 ноября 2026 года.\n\n"
    "Наши эксперты рекомендуют: сверить список получателей с Социальным реестром.\n\n"
    f"Crowe Uzbekistan\nПодробнее: {URL}\n\n#соцподдержка"
)


def _digest():
    digest = sample_digest()
    first = digest.articles[0]
    first.texts["ru"] = Text(
        headline="Помощь 1 млн сумов нуждающимся семьям",
        summary="Указ № УП-236 от 28.10.2026 предусматривает выплату 1 млн сумов семьям из Реестра.",
        details="Выплаты планируется завершить до 30 ноября 2026 года.",
    )
    return digest


class FakeClaude:
    """Отдаёт ответы по очереди; запросы и системные промпты пишет в calls."""

    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt, schema, *, settings, system_prompt):
        assert schema is PostResponse
        self.calls.append((prompt, system_prompt))
        text = self.texts.pop(0)
        answer = PostResponse(text=text, hashtags=qa.hashtags(text), notes="")
        return LlmResult(answer, 1000, {"input_tokens": 10, "output_tokens": 5})


def _setup(tmp_path, max_per_day=40):
    settings = Settings(
        timezone="Asia/Tashkent", db_path=tmp_path / "db.sqlite3", outputs_dir=tmp_path / "outputs",
        logs_dir=tmp_path, http=HttpSettings(user_agent="test"), llm=LLM,
        posts=PostsSettings(max_per_day=max_per_day),
    )  # fmt: skip
    return repo.connect(":memory:"), settings, _digest()


def _post(conn, settings, digest, claude, **kwargs):
    kwargs.setdefault("platform", "telegram")
    budget = LlmBudget(LLM, ask_fn=claude)
    return make_post(conn, settings, digest, "1", budget=budget, profiles=PROFILES, now=NOW, **kwargs)


# --- Профили и темы ---


def test_profiles_have_limits_and_known_languages(tmp_path):
    assert set(PROFILES.platforms) == {"telegram", "linkedin", "threads", "instagram"}
    for profile in PROFILES.platforms.values():
        assert profile.max_chars > 0 and profile.default_lang in LANGS
        assert profile.target[1] <= profile.max_chars
    assert PROFILES.get("threads").max_chars == 500
    with pytest.raises(LookupError, match="telegram"):
        PROFILES.get("tiktok")
    (tmp_path / "telegram").mkdir()
    (tmp_path / "telegram" / "01.md").write_text("Образец поста\n", encoding="utf-8")
    assert load_profiles(examples_dir=tmp_path).get("telegram").examples == ["Образец поста"]


def test_topics_follow_digest_order_and_ids():
    digest = _digest()
    topics = list_topics(digest)
    assert [t.id for t in topics] == [a.id for a in digest.articles]
    assert [(t.number, t.category, t.doc_key) for t in topics[:2]] == [
        ("001", "legislation", "UP-236-2026"), ("002", "legislation", "PKM-594-2026")
    ]  # fmt: skip
    assert find_article(digest, "3").id == find_article(digest, "003").id == "2026-11-02-003"
    assert find_article(digest, "2026-11-02-004").category == "finance"
    with pytest.raises(LookupError):
        find_article(digest, "99")


# --- Проверка поста ---


def _check(text, lang="ru", platform="telegram", advice=""):
    article = _digest().articles[0]
    sources = [(f"{t.headline}\n{t.summary}\n{t.details}", lang) for lang, t in article.texts.items()]
    sources.append(("236 28.10.2026 30.10.2026", "ru"))
    allowed = {URL, "https://gazeta.uz/a1", "https://lex.uz/ru/docs/8563561"}
    return qa.check(text, lang, PROFILES.get(platform), sources, allowed, advice=advice)


def test_correct_post_passes():
    assert _check(GOOD) == []
    # Год отдельно при полной дате в статье и ссылка без https/www — не ошибки
    assert _check(GOOD.replace("до 30 ноября 2026 года", "в 2026 году").replace(URL, "norma.uz/a1")) == [
        "нет ссылки на источник"
    ]  # адрес без схемы — не ссылка


def test_post_problems_are_caught():
    assert _check(GOOD.replace("1 млн", "2 млн")) == ["нет в статье: 2 млн"]
    assert _check(GOOD.replace("1 млн", "1 тыс.")) == ["нет в статье: 1 тыс."]  # ошибка в тысячу раз
    assert _check(GOOD.replace(URL, "https://norma.uz/other")) == [
        "ссылка не из статьи: https://norma.uz/other", "нет ссылки на источник"
    ]  # fmt: skip
    assert _check(GOOD + " " + "слово " * 800)[0].startswith("длина ")
    assert (
        _check(GOOD.replace(f"Подробнее: {URL}\n\n", ""), platform="threads") == []
    )  # 0–1 хэштег, без ссылки
    assert _check(GOOD + " #налоги #ндс #бизнес") == ["хэштегов 4, можно не больше 3"]
    assert _check(GOOD.replace("#соцподдержка", "#ijtimoiyqo‘llab")) == [
        "хэштег оборвётся на апострофе или дефисе: #ijtimoiyqo‘llab"
    ]
    assert _check(GOOD, platform="instagram") == [f"в посте для Instagram ссылок быть не должно: {URL}"]
    assert _check(GOOD, lang="en") == ["пост на языке ru, а заказан en"]


def test_detect_language():
    assert qa.detect_lang("Семьям из Социального реестра выплатят помощь в ноябре") == "ru"
    assert (
        qa.detect_lang("Families in the Social Register will receive one-time assistance in November") == "en"
    )
    assert (
        qa.detect_lang(
            "Ijtimoiy reyestrdagi oilalarga noyabr oyida bir martalik yordam beriladi va bu to‘lov"
        )
        == "uz"
    )
    assert qa.detect_lang("#налоги https://norma.uz") is None
    assert qa.count_chars("пост 📌") == 7  # эмодзи — две единицы UTF-16, как в Telegram


# --- Пост целиком ---


def test_post_saved_to_db_and_file_then_same_post_from_cache(tmp_path):
    conn, settings, digest = _setup(tmp_path)
    claude = FakeClaude(GOOD)
    post = _post(conn, settings, digest, claude)
    assert (post.flags, post.calls, post.cached, post.lang) == ([], 1, False, "ru")
    request, system = claude.calls[0]
    assert "Telegram" in system and "Указ № УП-236 от 28.10.2026" in request and URL in request
    path = tmp_path / "outputs" / "2026-11-02" / "posts" / "2026-11-02-001-telegram-ru-1.md"
    assert post.path == str(path) and path.read_text(encoding="utf-8").endswith(f"{GOOD}\n")

    again = _post(conn, settings, digest, FakeClaude())
    assert (again.id, again.cached, again.calls, again.text) == (post.id, True, 0, GOOD)

    variant = FakeClaude(GOOD.replace("Семьям выплатят", "Нуждающимся семьям выплатят"))
    second = _post(conn, settings, digest, variant, mode="variant")
    assert second.id != post.id and "Предыдущий вариант поста:\n**Семьям выплатят" in variant.calls[0][0]
    assert second.path.endswith("telegram-ru-2.md")


def test_retry_after_failed_check_then_flag(tmp_path):
    conn, settings, digest = _setup(tmp_path)
    bad = GOOD.replace("1 млн", "2 млн")
    claude = FakeClaude(bad, GOOD)
    post = _post(conn, settings, digest, claude)
    assert (post.flags, post.calls, post.text) == ([], 2, GOOD)
    assert "нет в статье: 2 млн" in claude.calls[1][0]  # повтор с перечнем ошибок

    post = _post(conn, settings, digest, FakeClaude(bad, bad), mode="variant")
    assert (post.flags, post.calls) == (["нет в статье: 2 млн"], 2)
    assert "⚠️ проверить: нет в статье: 2 млн" in open(post.path, encoding="utf-8").read()


def test_post_remembers_the_tokens_and_time_of_both_attempts(tmp_path):
    conn, settings, digest = _setup(tmp_path)

    post = _post(conn, settings, digest, FakeClaude("Без ссылки и с числом 999 999.", GOOD))

    row = repo.post(conn, post.id)
    assert json.loads(row["usage_json"]) == {"input_tokens": 20, "output_tokens": 10}
    assert row["duration_ms"] == 2000


def test_daily_limit_and_unavailable_claude(tmp_path):
    conn, settings, digest = _setup(tmp_path, max_per_day=1)
    _post(conn, settings, digest, FakeClaude(GOOD))
    assert _post(conn, settings, digest, FakeClaude()).cached  # из кэша — не считается
    with pytest.raises(PostLimitReached):
        _post(conn, settings, digest, FakeClaude(GOOD), mode="variant")

    conn, settings, digest = _setup(tmp_path)
    budget = LlmBudget(LLM.model_copy(update={"max_calls_per_run": 0}), ask_fn=FakeClaude())
    with pytest.raises(PostError, match="Claude не ответил"):
        make_post(conn, settings, digest, "1", "telegram", budget=budget, profiles=PROFILES, now=NOW)


# --- Голос Crowe ---

ADVICE = "Наши эксперты рекомендуют"


def test_post_is_written_for_crowe_not_as_a_news_agency(tmp_path):
    conn, settings, digest = _setup(tmp_path)
    fake = FakeClaude(GOOD)
    make_post(conn, settings, digest, "1", "telegram", budget=LlmBudget(LLM, ask_fn=fake), now=NOW)
    _, system_prompt = fake.calls[0]
    assert "Crowe Uzbekistan" in system_prompt
    assert ADVICE in system_prompt
    assert "новостного агентства" not in system_prompt


def test_post_without_experts_advice_goes_back_to_claude():
    text = GOOD.replace(f"{ADVICE}: сверить список получателей с Социальным реестром.\n\n", "")
    assert _check(text, advice=ADVICE) == [f"нет блока «{ADVICE}»"]


def test_invented_number_in_the_advice_is_caught():
    text = GOOD.replace("сверить список", "за 15 дней сверить список")
    assert _check(text, advice=ADVICE) == ["нет в статье: 15"]


def test_every_language_has_the_advice_label():
    assert set(PROFILES.brand.advice_label) == set(LANGS)


def test_threads_post_carries_no_link():
    assert PROFILES.get("threads").link == "none"
