"""Telegram-бот (этап 10): доступ, утренняя рассылка, темы, карточка темы, пост, другие дайджесты.

Telegram не нужен: проверяется BotCore — какие ответы и кнопки бот отправит. Claude и сеть подменены.
"""

import asyncio
from dataclasses import replace
from datetime import date, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
from test_pipeline import NOW, _settings
from test_service import _ready_db, _run

from uzdigest import service
from uzdigest.bot import app
from uzdigest.bot.core import DENIED, MAX_MESSAGE, BotCore, Button, PostRequest, telegram_html, topic_card
from uzdigest.bot.notifier import should_notify, telegram_notifier
from uzdigest.config import BotSettings, BotUser
from uzdigest.db import repo
from uzdigest.ops import Busy, run_lock, single_instance
from uzdigest.posts.generate import Post, PostError, PostLimitReached
from uzdigest.render.model import Article, Digest, SourceRef
from uzdigest.render.model import Text as TextModel

ADMIN, SMM = 999812790, 7584910935
DAY = "2026-11-02"  # дайджест, который собирает _run


def _core(tmp_path, started=None):
    settings = _settings(tmp_path)
    settings.bot = BotSettings(users=[
        BotUser(telegram_id=ADMIN, name="Админ", role="admin"),
        BotUser(telegram_id=SMM, name="СММ", role="smm"),
    ])  # fmt: skip
    started = [] if started is None else started
    core = BotCore(settings, start_run=started.append, clock=lambda: NOW + timedelta(hours=1))
    return core, settings


def _codes(reply):
    return [b.data for row in reply.buttons for b in row]


def _with_digest(tmp_path):
    core, settings = _core(tmp_path)
    _ready_db(settings)
    _run(settings)
    return core, settings


def _delivered(core, messages):
    """То, что обёртка делает после удачной отправки."""
    for chat, reply in messages:
        if reply.morning:
            core.mail_done(chat, reply.morning)


def _copy_digest(settings, day, count):
    """Копия собранного дайджеста на другой день с count статьями: листание и «Другие дайджесты»."""
    conn = repo.connect(settings.db_path)
    row = repo.digest_by_date(conn, DAY)
    model = Digest.from_json(row["model_json"])
    articles = [model.articles[0].model_copy(update={"id": f"{day}-{n:03d}"}) for n in range(1, count + 1)]
    copy = model.model_copy(update={"date": date.fromisoformat(day), "articles": articles})
    repo.save_digest(conn, day, str(settings.outputs_dir / day), row["run_id"], copy.to_json(), [])


# --- Доступ ---


def test_only_whitelist_and_roles(tmp_path):
    core, _ = _core(tmp_path)
    admin, smm = core.user(ADMIN), core.user(SMM)
    assert core.user(123) is None
    assert _codes(core.menu(admin)) == ["news", "dl", "refresh", "status", "metrics"]
    assert _codes(core.menu(smm)) == ["news", "dl"]
    assert core.button(smm, 1, "refresh")[0].text == "Обновлять дайджест может только админ."
    assert core.button(smm, 1, "status")[0].text == "Статус доступен только админу."
    assert core.button(smm, 1, "metrics")[0].text == "Показатели доступны только админу."


def test_admin_sees_the_numbers_in_one_message(tmp_path):
    core, settings = _with_digest(tmp_path)
    for n in range(40):
        repo.connect(settings.db_path)
        service.add_feedback("post", f"Замечание {n} " + "длинное " * 40, settings=settings, now=NOW)

    [reply] = core.button(core.user(ADMIN), ADMIN, "metrics")

    assert reply.text.startswith("📊 Показатели за 30 дней")
    assert "Источники" in reply.text and "Claude" in reply.text and "Замечаний: 40" in reply.text
    assert len(reply.text) <= MAX_MESSAGE
    assert _codes(reply) == ["menu"]


def test_stranger_gets_access_denied(tmp_path, monkeypatch):
    core, _ = _core(tmp_path)
    sent, alerts = [], []

    async def send(application, chat_id, reply):
        sent.append((chat_id, reply.text))

    async def answer(text=None, show_alert=False):
        alerts.append((text, show_alert))

    monkeypatch.setattr(app, "_send", send)
    stranger = SimpleNamespace(id=123)
    update = SimpleNamespace(
        effective_user=stranger, effective_chat=SimpleNamespace(id=123),
        message=SimpleNamespace(text="привет"),
        callback_query=SimpleNamespace(from_user=stranger, answer=answer, data="news"),
    )  # fmt: skip
    context = SimpleNamespace(bot_data={"core": core}, application=None)
    for handler in (app._on_start, app._on_text, app._on_button):
        asyncio.run(handler(update, context))
    assert DENIED == "Доступ запрещен"
    assert sent == [(123, DENIED), (123, DENIED)] and alerts == [(DENIED, True)]


def test_stale_button_press_is_still_handled(tmp_path, monkeypatch):
    core, _ = _core(tmp_path)
    delivered = []

    async def answer(text=None, show_alert=False):
        raise BadRequest("Query is too old and response timeout expired or query id is invalid")

    async def deliver(application, chat_id, user, replies):
        delivered.append((chat_id, [r.text for r in replies]))

    monkeypatch.setattr(app, "_deliver", deliver)
    admin = SimpleNamespace(id=ADMIN)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=ADMIN),
        callback_query=SimpleNamespace(from_user=admin, answer=answer, data="menu"),
    )
    asyncio.run(app._on_button(update, SimpleNamespace(bot_data={"core": core}, application=None)))
    assert delivered == [(ADMIN, ["Здравствуйте, Админ! Что делаем?"])]


def test_real_whitelist_from_settings():
    from uzdigest.config import load_settings

    users = load_settings().bot.users
    assert {(u.telegram_id, u.role) for u in users} == {(999812790, "admin"), (7584910935, "admin")}


# --- Утренняя рассылка и «Собрать новости» ---


def test_new_digest_is_sent_to_everyone_once_a_day(tmp_path):
    core, settings = _with_digest(tmp_path)
    sent = core.poll()
    assert [chat for chat, _ in sent] == [ADMIN, SMM]
    assert all(reply.text.startswith("☀️ Готов дайджест за 02.11.2026 — 2 статьи") for _, reply in sent)
    assert f"t:{DAY}:001" in _codes(sent[1][1])
    _delivered(core, sent)
    assert core.poll() == []  # уже разослан
    restarted = BotCore(settings, start_run=[].append, clock=core.clock)
    assert restarted.poll() == []  # и после перезапуска бота повторно не шлётся


def _send_failing(monkeypatch, errors):
    """Подменяет отправку: чат из errors падает с этой ошибкой, остальные получают сообщение."""
    sent = []

    async def send(application, chat_id, reply):
        if chat_id in errors:
            raise errors[chat_id]
        sent.append(chat_id)

    monkeypatch.setattr(app, "_send", send)
    return sent


def test_who_missed_the_morning_digest_gets_it_when_the_network_is_back(tmp_path, monkeypatch, caplog):
    """Раньше рассылка отмечалась до отправки: сбой сети оставлял человека без дайджеста на весь день."""
    core, _ = _with_digest(tmp_path)
    errors = {SMM: NetworkError("обрыв")}
    sent = _send_failing(monkeypatch, errors)
    caplog.set_level("INFO")

    asyncio.run(app._send_all(None, core, core.poll()))

    assert sent == [ADMIN]
    assert "получателей 1" in caplog.text  # в журнале только то, что правда ушло
    errors.clear()
    retry = core.poll()
    assert [chat for chat, _ in retry] == [SMM]  # админу второй раз не шлётся
    asyncio.run(app._send_all(None, core, retry))
    assert sent == [ADMIN, SMM] and core.poll() == []


def test_no_mailing_report_when_nothing_was_delivered(tmp_path, monkeypatch, caplog):
    core, _ = _with_digest(tmp_path)
    _send_failing(monkeypatch, {ADMIN: TimedOut("нет ответа"), SMM: TimedOut("нет ответа")})
    caplog.set_level("INFO")

    asyncio.run(app._send_all(None, core, core.poll()))

    assert "доставлен" not in caplog.text
    assert len(core.poll()) == 2


def test_who_blocked_the_bot_is_not_asked_again_all_day(tmp_path, monkeypatch):
    core, _ = _with_digest(tmp_path)
    _send_failing(monkeypatch, {SMM: Forbidden("bot was blocked by the user")})

    asyncio.run(app._send_all(None, core, core.poll()))

    assert core.poll() == []


def test_state_of_the_previous_bot_version_is_not_sent_twice(tmp_path):
    core, settings = _with_digest(tmp_path)
    (settings.db_path.parent / "bot_state.json").write_text('{"announced": "2026-11-02"}', encoding="utf-8")

    assert core.poll() == []


def test_mailing_of_a_new_day_starts_over(tmp_path):
    core, settings = _with_digest(tmp_path)
    _delivered(core, core.poll())
    core.clock = lambda: NOW + timedelta(days=1, hours=1)
    _copy_digest(settings, "2026-11-03", 1)

    assert [chat for chat, _ in core.poll()] == [ADMIN, SMM]


def test_news_ready_sends_topics_of_the_day(tmp_path):
    core, _ = _with_digest(tmp_path)
    [reply] = core.button(core.user(SMM), 1, "news")
    assert reply.text.startswith(
        "📰 Дайджест за 02.11.2026 — 2 статьи\nКурсы ЦБ на 31.10.2026: USD 12 023,46"
    )
    assert "\n001 · " in reply.text and "Стр. 1 из 1." in reply.text
    assert _codes(reply) == [f"t:{DAY}:001", f"t:{DAY}:002", f"f:{DAY}", "dl", "menu"]
    assert _codes(core.button(core.user(ADMIN), 1, "news")[0])[-1] == "refresh"
    [files] = core.button(core.user(SMM), 1, f"f:{DAY}")
    assert [p.name for p in files.files] == ["digest.ru.md", "digest.en.md", "digest.uz.md"]


def test_news_not_ready_collects_once_and_everyone_gets_digest_once(tmp_path):
    started = []
    core, settings = _core(tmp_path, started)
    first = core.news(core.user(ADMIN), ADMIN)
    assert started == [False] and "Собираю новости" in first[0].text
    with run_lock(tmp_path / "run.lock", "run", NOW):
        second = core.news(core.user(SMM), SMM)  # второй нажал, пока идёт сбор
        assert started == [False] and second[0].text.startswith("Сбор уже идёт")
        assert core.poll() == []  # ещё идёт
    _ready_db(settings)
    _run(settings)
    answers = core.poll()
    assert [chat for chat, _ in answers] == [ADMIN, SMM]  # рассылка; ждавшим второй раз не шлётся
    _delivered(core, answers)
    assert core.poll() == []


def test_refresh_answers_only_who_asked(tmp_path):
    started = []
    core, settings = _core(tmp_path, started)
    _ready_db(settings)
    _run(settings)
    _delivered(core, core.poll())  # утренняя рассылка
    core.button(core.user(ADMIN), ADMIN, "refresh")
    assert started == [True]
    _run(settings, force=True, now=NOW + timedelta(minutes=30))
    [(chat, reply)] = core.poll()
    assert chat == ADMIN and reply.text.startswith("📰 Дайджест за 02.11.2026")


def test_failed_refresh_is_told_and_the_old_digest_is_not_shown_as_new(tmp_path):
    started = []
    core, settings = _core(tmp_path, started)
    _ready_db(settings)
    _run(settings)
    _delivered(core, core.poll())
    core.button(core.user(ADMIN), ADMIN, "refresh")

    warning, topics = [reply for _, reply in core.poll()]  # сборка упала: дайджест остался утренним

    assert warning.text.startswith("⚠️ Обновить дайджест не удалось")
    assert topics.text.startswith("📰 Дайджест за 02.11.2026")


def test_failed_collect_explains(tmp_path):
    core, _ = _core(tmp_path)
    core.news(core.user(ADMIN), 10)
    [(chat, reply)] = core.poll()
    assert chat == 10 and reply.text.startswith("Собрать дайджест не удалось.\n\nДайджестов ещё нет.")


# --- Темы и другие дайджесты ---


def test_topic_card_has_details_source_and_platform_buttons(tmp_path):
    core, settings = _with_digest(tmp_path)
    [card] = core.button(core.user(SMM), 1, f"t:{DAY}:001")
    article = service.digest(settings=settings).articles[0]
    assert card.html and card.text.startswith(f"<b>{article.texts['ru'].headline}</b>")
    assert "<b>Краткое содержание:</b>" in card.text and "<b>Подробное содержание:</b>" in card.text
    assert f'<b>Источник:</b> <a href="{article.source.url}">{article.source.name}</a>' in card.text
    assert _codes(card) == [
        f"g:{DAY}:001:telegram:ru:new", f"g:{DAY}:001:linkedin:en:new",
        f"g:{DAY}:001:threads:ru:new", f"g:{DAY}:001:instagram:ru:new",
        f"d:{DAY}:0", "menu",
    ]  # fmt: skip
    assert [b.text for b in card.buttons[0]] == ["✍️ Telegram", "✍️ LinkedIn"]
    [missing] = core.button(core.user(SMM), 1, f"t:{DAY}:099")
    assert missing.text == "В дайджесте за 02.11.2026 нет темы 099."


def test_long_topic_card_fits_one_message(tmp_path):
    _, settings = _with_digest(tmp_path)
    digest = service.digest(settings=settings)
    article = digest.articles[0]
    text = article.texts["ru"].model_copy(update={"details": "Очень длинный абзац & <тег>. " * 400})
    card = topic_card(article.model_copy(update={"texts": {**article.texts, "ru": text}}), digest.date)
    assert MAX_MESSAGE - 10 < len(card) <= MAX_MESSAGE  # обрезано ровно столько, сколько нужно
    assert "…" in card and "&lt;тег&gt;" in card and "<b>Источник:</b>" in card


def test_topics_paging_and_other_digests(tmp_path, monkeypatch):
    core, settings = _with_digest(tmp_path)
    _copy_digest(settings, "2026-10-30", 20)
    user = core.user(SMM)
    [days] = core.button(user, 1, "dl")
    assert [b.text for row in days.buttons for b in row] == [
        "02.11.2026, пн — 2 статьи", "30.10.2026, пт — 20 статей", "📋 Меню"
    ]  # fmt: skip
    [first] = core.button(user, 1, "d:2026-10-30")
    assert "Стр. 1 из 3." in first.text and not first.edit
    assert _codes(first)[:9] == [*(f"t:2026-10-30:{n:03d}" for n in range(1, 9)), "dp:2026-10-30:1"]
    [last] = core.button(user, 1, "dp:2026-10-30:2")
    assert last.edit and "Стр. 3 из 3." in last.text and "dp:2026-10-30:1" in _codes(last)
    assert "refresh" not in _codes(core.button(core.user(ADMIN), 1, "d:2026-10-30")[0])  # не сегодняшний
    [back] = core.button(user, 1, "t:2026-10-30:017")[0].buttons[-1:]
    assert back[0].data == "d:2026-10-30:2"  # «К темам» — на страницу этой темы
    fake = FakePosts()
    monkeypatch.setattr(service, "post", fake)
    [writing] = core.button(user, 1, "g:2026-10-30:017:threads:ru:new")
    core.generate(user, writing.generate)
    assert fake.calls == [("017", "threads", "ru", "new", "СММ", date(2026, 10, 30))]


@pytest.mark.parametrize(
    "data",
    ["t:005", "p:telegram", "m:shorter", "other", "d:вчера", "xyz",
     f"g:{DAY}:001:facebook:ru:new", f"g:{DAY}:001:telegram:de:new", f"g:{DAY}:001:telegram:ru:faster"],
)  # fmt: skip
def test_old_or_broken_buttons_show_menu(tmp_path, data):
    core, _ = _with_digest(tmp_path)
    [reply] = core.button(core.user(SMM), 1, data)
    assert reply.text.startswith("Здравствуйте")


def test_button_data_fits_telegram_limit():
    assert len(PostRequest(date(2026, 11, 2), "1000", "instagram", "uz", "variant").data.encode()) <= 64
    with pytest.raises(ValueError):
        Button("x", "g:" + "a" * 70)


# --- Пост ---


class FakePosts:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, article, platform, lang, mode, *, day, author, settings):
        self.calls.append((article, platform, lang, mode, author, day))
        if self.error:
            raise self.error
        text = "**Заголовок <важно>**\n\nТекст поста.\n\nИсточник: https://norma.uz/a1"
        return Post(
            id=len(self.calls),
            digest_date=day,
            article_id=f"{day}-{article}",
            platform=platform,
            lang=lang,
            mode=mode,
            text=text,
            chars=len(text),
            max_chars=4096,
            hashtags=[],
            notes="",
            flags=["нет в статье: 2 млн"] if mode == "longer" else [],
            path="x",
            calls=1,
            cached=False,
        )


def test_post_dialog(tmp_path, monkeypatch):
    core, _ = _with_digest(tmp_path)
    fake = FakePosts()
    monkeypatch.setattr(service, "post", fake)
    user = core.user(SMM)
    [writing] = core.button(user, 1, f"g:{DAY}:001:telegram:ru:new")
    assert writing.generate == PostRequest(date(2026, 11, 2), "001", "telegram", "ru")
    assert writing.text.startswith("Пишу пост для Telegram (Русский)")
    info, preview, text = core.generate(user, writing.generate)
    assert fake.calls == [("001", "telegram", "ru", "new", "СММ", date(2026, 11, 2))]
    assert info.text.startswith("Telegram · Русский · тема 001")
    assert preview.html and "<b>Заголовок &lt;важно&gt;</b>" in preview.text
    assert text.text.startswith("**Заголовок")
    assert _codes(text) == [
        f"g:{DAY}:001:telegram:ru:variant", f"g:{DAY}:001:telegram:ru:shorter",
        f"g:{DAY}:001:telegram:ru:longer", f"g:{DAY}:001:telegram:en:new", f"g:{DAY}:001:telegram:uz:new",
        f"o:{DAY}:001", f"t:{DAY}:001",
        f"fb:{DAY}:001:telegram:ru:1",
    ]  # fmt: skip

    # «Короче» — тот же выбор, режим shorter; «Другая площадка» сохраняет тему
    [shorter] = core.button(user, 1, f"g:{DAY}:001:telegram:ru:shorter")
    assert shorter.text == "Переписываю пост…" and shorter.generate.mode == "shorter"
    [platforms] = core.button(user, 1, f"o:{DAY}:001")
    assert platforms.text.startswith("Тема 001.") and f"g:{DAY}:001:linkedin:en:new" in _codes(platforms)
    info = core.generate(user, replace(writing.generate, mode="longer"))[0]
    assert "⚠️ Проверить: нет в статье: 2 млн" in info.text
    assert "Перед публикацией рекомендацию проверяет эксперт" in info.text


@pytest.mark.parametrize(
    ("error", "start"),
    [
        (
            PostLimitReached("Сегодня сделано уже 40 постов — дневной лимит (posts.max_per_day)"),
            "Сегодня сделано",
        ),
        (
            PostError("Claude не ответил: лимит подписки Claude исчерпан"),
            "Пост не получился. Claude не ответил",
        ),
    ],
)
def test_post_errors_are_plain_text(tmp_path, monkeypatch, error, start):
    core, _ = _with_digest(tmp_path)
    monkeypatch.setattr(service, "post", FakePosts(error))
    [reply] = core.generate(core.user(ADMIN), PostRequest(date(2026, 11, 2), "001", "telegram", "ru"))
    assert reply.text.startswith(start)


def test_thumbs_down_saves_feedback(tmp_path, monkeypatch):
    core, settings = _with_digest(tmp_path)
    monkeypatch.setattr(service, "post", FakePosts())
    user = core.user(SMM)
    core.generate(user, PostRequest(date(2026, 11, 2), "001", "linkedin", "en"))
    assert core.text(user, "просто текст")[0].text.startswith("Здравствуйте")  # без «👎» — меню
    core.button(user, 1, f"fb:{DAY}:001:linkedin:en:1")
    assert core.text(user, "Слишком длинно")[0].text == "Спасибо, замечание записано."
    [item] = service.list_feedback(settings=settings)
    assert (item.kind, item.source, item.author, item.article_id) == ("post", "telegram", "СММ", f"{DAY}-001")
    assert item.text == "Слишком длинно [пост 1, linkedin, en]"


def test_telegram_html_escapes_everything_but_bold():
    assert telegram_html("**A & B**\n<script>") == "<b>A &amp; B</b>\n&lt;script&gt;"


class _TelegramHtml(HTMLParser):
    """Читает разметку как Telegram: разрешённые теги, ни одного лишнего, все закрыты."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open, self.tags, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.open.append(tag)
        self.tags.append((tag, attrs))

    def handle_endtag(self, tag):
        assert self.open.pop() == tag

    def handle_data(self, data):
        self.text.append(data)


NASTY = (
    '<script>alert(1)</script> & <b>жирный</b> «кавычки» "двойные" </a> <a href="http://x">ссылка</a> [а](б)'
)


def test_text_from_the_sources_cannot_change_the_markup_of_a_topic_card():
    source = SourceRef(
        code="kun",
        name='ku"n<.uz',
        url='https://kun.uz/1?a=1&b="><script>',
        published_at=NOW,
        time_known=True,
    )
    other = source.model_copy(update={"name": "gazeta<b>.uz", "url": "https://gazeta.uz/<x>"})
    article = Article(
        id=f"{DAY}-001", cluster_id=1, category="taxes", importance=4, doc=None, source=source, also=[other],
        texts={"ru": TextModel(headline=NASTY, summary=NASTY, details=NASTY), "en": None, "uz": None},
        by_llm=False, flags=[f"факт не из источника: {NASTY}"],
    )  # fmt: skip

    card = topic_card(article, date(2026, 11, 2))

    parser = _TelegramHtml()
    parser.feed(card)
    parser.close()
    assert parser.open == []
    assert {tag for tag, _ in parser.tags} <= {"b", "i", "a"}
    assert [attrs for tag, attrs in parser.tags if tag == "a"] == [
        [("href", 'https://kun.uz/1?a=1&b="><script>')],
        [("href", "https://gazeta.uz/<x>")],
    ]
    assert NASTY in "".join(parser.text)


def test_post_from_claude_cannot_change_the_markup_of_the_preview():
    parser = _TelegramHtml()
    parser.feed(telegram_html(f"**{NASTY}**\n\n{NASTY}\n\n** незакрытая звёздочка"))
    parser.close()
    assert parser.open == [] and {tag for tag, _ in parser.tags} == {"b"}


def test_post_longer_than_a_message_does_not_break_the_answer(tmp_path, monkeypatch):
    """Превью с подписью длиннее лимита Telegram: сообщение падало, и сам пост уже не приходил."""
    core, _ = _with_digest(tmp_path)
    fake = FakePosts()

    def long_post(*args, **kwargs):
        post = fake(*args, **kwargs)
        return post.model_copy(
            update={"text": "т" * 5000, "chars": 5000, "flags": ["длина 5000 больше лимита 4096"]}
        )

    monkeypatch.setattr(service, "post", long_post)

    replies = core.generate(core.user(SMM), PostRequest(date(2026, 11, 2), "001", "telegram", "ru"))

    assert all(len(reply.text) <= MAX_MESSAGE for reply in replies)
    assert "⚠️ Проверить: длина 5000" in replies[0].text
    assert not any(reply.html for reply in replies)  # превью нет, а текст с кнопками есть
    assert replies[-1].buttons


def test_short_network_break_of_polling_is_one_line_in_the_log(caplog):
    context = SimpleNamespace(error=NetworkError("httpx.ReadError: "), bot=None)

    asyncio.run(app._on_error(None, context))

    [record] = caplog.records
    assert record.levelname == "WARNING" and record.exc_info is None


def test_button_press_leaves_a_line_in_the_log(tmp_path, monkeypatch, caplog):
    core, _ = _core(tmp_path)
    caplog.set_level("INFO")

    async def answer(*args, **kwargs):
        return None

    async def deliver(application, chat_id, user, replies):
        return None

    monkeypatch.setattr(app, "_deliver", deliver)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=ADMIN),
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=ADMIN), answer=answer, data=f"t:{DAY}:001"
        ),
    )

    asyncio.run(app._on_button(update, SimpleNamespace(bot_data={"core": core}, application=None)))

    assert "Бот: Админ нажал t" in caplog.text


# --- Уведомитель и единственный экземпляр ---


def _summary(status="ok", articles=3, no_llm=0, stats=None):
    return service.DigestSummary(
        date=date(2026, 11, 2), dir="x", status=status, articles=articles, legal=0, updates=0, by_category={},
        headlines=[], no_llm=no_llm, no_translation=0, qa_flagged=0, failed_sources=[], window_from=NOW,
        window_to=NOW, stats=stats or {},
    )  # fmt: skip


def _all_failed():
    runs = [
        service.SourceRun(code=c, status="error", fetched=0, new=0, backfilled=0, error="x", warnings=0)
        for c in ("kun", "daryo")
    ]
    return service.CollectSummary(status="partial", sources=runs)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "ok", "digest": _summary()}, False),
        ({"status": "partial", "digest": _summary(no_llm=2)}, True),
        ({"status": "partial", "digest": _summary(articles=0)}, True),
        ({"status": "partial", "digest": _summary(stats={"llm": {"stopped": "лимит"}})}, True),
        ({"status": "partial", "collect": _all_failed()}, True),
        ({"status": "exists", "digest": _summary(status="exists", no_llm=2)}, False),
    ],
)
def test_admin_hears_about_everything_that_needs_attention_and_nothing_else(result, expected):
    run = service.RunResult(kind="run", started_at=NOW, finished_at=NOW, **result)
    assert should_notify(run) is expected


def test_notifier_only_admins_only_failures_and_no_token_in_errors(tmp_path, monkeypatch):
    _, settings = _core(tmp_path)
    failed = service.RunResult(
        kind="run", status="failed", started_at=NOW, finished_at=NOW, error="OSError: x"
    )
    ok = service.RunResult(kind="run", status="ok", started_at=NOW, finished_at=NOW)
    sent = []

    def post(url, json, timeout):
        sent.append((url, json["chat_id"]))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    telegram_notifier(failed, settings, post)
    assert sent == []  # без токена молчит
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET")
    telegram_notifier(ok, settings, post)
    telegram_notifier(failed, settings, post)
    assert [chat for _, chat in sent] == [ADMIN]

    def broken(url, json, timeout):
        raise httpx.ConnectError(f"cannot reach {url}")

    with pytest.raises(RuntimeError) as error:
        telegram_notifier(failed, settings, broken)
    assert "SECRET" not in str(error.value)


def test_second_bot_instance_refused(tmp_path):
    with single_instance(tmp_path / "bot.lock"):
        with pytest.raises(Busy):
            with single_instance(tmp_path / "bot.lock"):
                pass
    with single_instance(tmp_path / "bot.lock"):
        pass
