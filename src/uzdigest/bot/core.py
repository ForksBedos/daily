"""Логика Telegram-бота без Telegram (docs/plan/stage-10-telegram-bot.md).

Каждое действие пользователя превращается в список ответов Reply (текст, кнопки, файлы). Обёртка
над python-telegram-bot (bot/app.py) только отправляет их. Так весь диалог проверяется тестами
без сети. Всё, что бот умеет, — вызовы service.py; своих путей в БД и конфиг у бота нет.

Данные кнопки (до 64 байт) несут всё, что нужно для действия: дату дайджеста, номер темы, площадку,
язык. Поэтому кнопки под старыми сообщениями и после перезапуска бота работают, а утренняя рассылка
не зависит от того, что пользователь нажимал раньше. Дата последней рассылки — в data/bot_state.json.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from uzdigest import service, voice
from uzdigest.config import PROJECT_ROOT, BotUser, Settings
from uzdigest.ops import read_lock
from uzdigest.posts.generate import MODES, Mode, PostError, PostLimitReached, find_article, list_topics
from uzdigest.posts.profiles import Profiles, load_profiles
from uzdigest.render.markdown import article_fields, load_labels, rates_line
from uzdigest.render.model import LANGS, Article, Digest
from uzdigest.timeutil import TASHKENT, tashkent_date, utc_now

log = logging.getLogger(__name__)

DENIED = "Доступ запрещен"  # ответ всем, кого нет в белом списке
MAX_MESSAGE = 4096  # лимит Telegram на текст сообщения
MAX_BUTTON_DATA = 64  # байт: лимит Telegram на данные кнопки
TOPICS_PER_PAGE = 8
DIGESTS_PER_PAGE = 10
TOPIC_BUTTON_CHARS = 60
LANG_NAMES = {"ru": "Русский", "en": "English", "uz": "O‘zbekcha"}
MODES_TEXT = {"variant": "🔁 Другой вариант", "shorter": "➖ Короче", "longer": "➕ Длиннее"}
MD_LINK = re.compile(r"\[([^\]]+)\]\((\S+?)\)")
EXPERT_CHECK = "👤 Перед публикацией рекомендацию проверяет эксперт Crowe"
VOICE_HEADER = "🎙 Расшифровка:\n\n"
VOICE_SPEED = 2.5  # во сколько раз расшифровка дольше записи: замерено на ноутбуке 06.11
REFRESH_FAILED = "⚠️ Обновить дайджест не удалось, ниже прежняя версия. Причину покажет кнопка «Статус»."


@dataclass(frozen=True)
class Button:
    text: str
    data: str  # код действия: news, d:2026-11-03, t:2026-11-03:005, g:2026-11-03:005:telegram:ru:new …

    def __post_init__(self) -> None:
        if len(self.data.encode()) > MAX_BUTTON_DATA:
            raise ValueError(f"Данные кнопки длиннее {MAX_BUTTON_DATA} байт: {self.data}")


@dataclass(frozen=True)
class PostRequest:
    """Какой пост писать: тема дайджеста, площадка, язык, режим. Целиком лежит в данных кнопки."""

    day: date
    number: str  # номер темы в дайджесте: 005
    platform: str
    lang: str
    mode: Mode = "new"

    @property
    def data(self) -> str:
        return ":".join(("g", self.day.isoformat(), self.number, self.platform, self.lang, self.mode))

    @property
    def article_id(self) -> str:
        return f"{self.day.isoformat()}-{self.number}"

    @property
    def label(self) -> str:
        return f"тема {self.number}"

    @property
    def back(self) -> Button:
        return Button("📄 К теме", f"t:{self.day.isoformat()}:{self.number}")

    @property
    def others(self) -> Button:
        return Button("📱 Другая площадка", f"o:{self.day.isoformat()}:{self.number}")

    @property
    def lost(self) -> Button:
        return Button("◀️ К темам", f"d:{self.day.isoformat()}")

    def dislike(self, post_id: int) -> Button:
        day, number = self.day.isoformat(), self.number
        return Button("👎 Не то", f"fb:{day}:{number}:{self.platform}:{self.lang}:{post_id}")


@dataclass(frozen=True)
class VoiceRequest:
    """Какой пост писать по голосовому. Те же поля и кнопки, что у PostRequest, источник — расшифровка."""

    voice_id: str  # 20261106-101530-77
    platform: str
    lang: str
    mode: Mode = "new"

    @property
    def data(self) -> str:
        return ":".join(("vg", self.voice_id, self.platform, self.lang, self.mode))

    @property
    def day(self) -> date:
        return voice.day_of(self.voice_id)

    @property
    def article_id(self) -> str:
        return f"voice-{self.voice_id}"

    @property
    def label(self) -> str:
        return "голосовое"

    @property
    def back(self) -> Button:
        return Button("🎙 К расшифровке", f"vt:{self.voice_id}")

    @property
    def others(self) -> Button:
        return Button("📱 Другая площадка", f"vo:{self.voice_id}")

    @property
    def lost(self) -> Button:
        return Button("📋 Меню", "menu")

    def dislike(self, post_id: int) -> Button:
        return Button("👎 Не то", f"vf:{self.voice_id}:{self.platform}:{self.lang}:{post_id}")


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    html: bool = False  # разметка HTML: карточка темы, превью поста для Telegram
    edit: bool = False  # листание: заменить сообщение с нажатой кнопкой, а не прислать новое
    generate: PostRequest | VoiceRequest | None = None  # после отправки — писать этот пост (долго, в фоне)
    morning: date | None = None  # рассылка за этот день: доставку отмечает обёртка после отправки


class BotCore:
    def __init__(
        self,
        settings: Settings,
        *,
        start_run: Callable[[bool], object] | None = None,
        profiles: Profiles | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.profiles = profiles or load_profiles()
        self.clock = clock
        self.start_run = start_run or self._start_process
        self.waiting: dict[int, int] = {}  # чат → пользователь: ждут дайджест после сбора
        self.refreshing: datetime | None = None  # конец окна дайджеста, который просили обновить
        self.awaiting_feedback: dict[int, tuple[PostRequest | VoiceRequest, int]] = {}  # пост под «👎»
        self.awaiting_edit: dict[int, str] = {}  # пользователь → голосовое, чей текст он исправляет
        self.process: subprocess.Popen | None = None
        self.writing: set[int] = set()  # кто сейчас ждёт пост — по одному посту на пользователя
        self._lock = threading.Lock()

    # --- Доступ и меню ---

    def user(self, telegram_id: int | None) -> BotUser | None:
        """Пользователь из белого списка; чужой — None, ему бот отвечает только DENIED."""
        found = self.settings.bot.user(telegram_id)
        if found is None:
            log.warning("Бот: сообщение от пользователя не из списка, id %s", telegram_id)
        return found

    def menu(self, user: BotUser) -> Reply:
        rows = [[Button("📰 Собрать новости", "news")], [Button("📚 Другие дайджесты", "dl")]]
        if user.role == "admin":
            rows.append([Button("🔄 Обновить дайджест", "refresh"), Button("ℹ️ Статус", "status")])
            rows.append([Button("📊 Показатели", "metrics")])
        return Reply(f"Здравствуйте, {user.name}! Что делаем?", rows)

    # --- Сбор и утренняя рассылка ---

    def news(self, user: BotUser, chat_id: int, *, force: bool = False) -> list[Reply]:
        """«Собрать новости»: готов — сразу темы дня; нет — фоновый сбор, ответ по готовности."""
        if force and user.role != "admin":
            return [Reply("Обновлять дайджест может только админ.")]
        digest = self._load(self._today())
        if not force and digest is not None:
            return [self._topics_page(user, digest)]
        with self._lock:
            self.waiting[chat_id] = user.telegram_id
            lock = read_lock(self._lock_path())
            if lock is not None or self._process_running():
                since = lock.started_at.astimezone(TASHKENT).strftime("%H:%M") if lock else "только что"
                return [Reply(f"Сбор уже идёт (начат в {since}), пришлю дайджест сюда, как будет готов.")]
            self.refreshing = digest.window.until if force and digest is not None else None
            self.start_run(force)
        what = "Обновляю дайджест новостями после утра" if force else "Собираю новости и дайджест"
        return [Reply(f"{what}, это около 10 минут. Пришлю сюда, как будет готово.")]

    def poll(self) -> list[tuple[int, Reply]]:
        """Вызывается периодически. Пока идёт сбор — пусто. Потом:

        - дайджест за сегодня дошёл ещё не до всех — темы дня тем, кто их не получил (утренняя рассылка,
          кто бы ни запустил сбор). Обёртка отмечает доставку после отправки, поэтому сбой сети не лишает
          человека дайджеста: рассылка повторится при следующей проверке;
        - сбор по кнопке закончился — темы или причина сбоя тем, кто ждал и не получил рассылку.
        """
        with self._lock:
            if self._process_running() or read_lock(self._lock_path()) is not None:
                return []
            waiting, self.waiting, self.process = self.waiting, {}, None
            refreshing, self.refreshing = self.refreshing, None
        today = self._today()
        missing = self._not_delivered(today)
        if not waiting and not missing:
            return []
        digest = self._load(today)
        out: list[tuple[int, Reply]] = []
        if digest is not None:
            out = [
                (u.telegram_id, replace(self._topics_page(u, digest, fresh=True), morning=today))
                for u in missing
            ]
        sent = {chat_id for chat_id, _ in out}  # в личном чате его номер — это ID пользователя
        for chat_id, user_id in waiting.items():
            user = self.settings.bot.user(user_id)
            if user is None or chat_id in sent:
                continue
            if digest is None:
                state = service.status(settings=self.settings, now=self.clock())
                text = "Собрать дайджест не удалось.\n\n" + service.describe_status(state)
                out.append((chat_id, Reply(text, [[Button("📋 Меню", "menu")]])))
            else:
                if digest.window.until == refreshing:
                    out.append((chat_id, Reply(REFRESH_FAILED)))
                out.append((chat_id, self._topics_page(user, digest)))
        return out

    def mail_done(self, chat_id: int, day: date) -> None:
        """Рассылка за день этому человеку закрыта: доставлена или слать бесполезно."""
        with self._lock:
            state = self._state()
            done = set(state.get("delivered", [])) if state.get("announced") == day.isoformat() else set()
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {"announced": day.isoformat(), "delivered": sorted(done | {chat_id})}
            path.write_text(json.dumps(data), encoding="utf-8")

    def status(self, user: BotUser) -> list[Reply]:
        if user.role != "admin":
            return [Reply("Статус доступен только админу.")]
        state = service.status(settings=self.settings, now=self.clock())
        return [Reply(service.describe_status(state), [[Button("📋 Меню", "menu")]])]

    def metrics(self, user: BotUser) -> list[Reply]:
        if user.role != "admin":
            return [Reply("Показатели доступны только админу.")]
        state = service.metrics(settings=self.settings, now=self.clock())
        return [Reply(_shorten(service.describe_metrics(state), MAX_MESSAGE), [[Button("📋 Меню", "menu")]])]

    def _today(self) -> date:
        return tashkent_date(self.clock())

    def _load(self, day: date | None) -> Digest | None:
        try:
            return service.digest(day, settings=self.settings)
        except LookupError:
            return None

    def _state_path(self) -> Path:
        return self.settings.db_path.parent / "bot_state.json"

    def _state(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _not_delivered(self, day: date) -> list[BotUser]:
        """Кому рассылка за день ещё не закрыта."""
        state = self._state()
        if state.get("announced") != day.isoformat():
            return list(self.settings.bot.users)
        if "delivered" not in state:  # запись прежней версии бота: рассылка за этот день прошла
            return []
        return [u for u in self.settings.bot.users if u.telegram_id not in state["delivered"]]

    def _lock_path(self) -> Path:
        return self.settings.db_path.parent / "run.lock"

    def _process_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _start_process(self, force: bool) -> None:
        """Сбор и сборка — отдельным процессом: бот не ждёт и отвечает на другие кнопки."""
        command = [sys.executable, "-m", "uzdigest", "run", *(["--force"] if force else [])]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        log.info("Бот запустил сбор: %s", " ".join(command[2:]))

    # --- Дайджесты и темы ---

    def digest_list(self, page: int = 0, *, edit: bool = False) -> list[Reply]:
        """«Другие дайджесты»: все собранные дни, новые первыми, по 10 на страницу."""
        items = service.digests(settings=self.settings)
        if not items:
            return [
                Reply(
                    "Дайджестов ещё нет. Нажмите «Собрать новости».", [[Button("📰 Собрать новости", "news")]]
                )
            ]
        pages, page = _pages(len(items), DIGESTS_PER_PAGE, page)
        rows = []
        for item in items[page * DIGESTS_PER_PAGE : (page + 1) * DIGESTS_PER_PAGE]:
            label = f"{item.date:%d.%m.%Y}, {service.WEEKDAYS[item.date.weekday()]} — "
            rows.append([Button(label + service.articles_text(item.articles), f"d:{item.date.isoformat()}")])
        rows += _nav(page, pages, "dlp:{}")
        rows.append([Button("📋 Меню", "menu")])
        return [Reply(f"Дайджесты (стр. {page + 1} из {pages}). Выберите день:", rows, edit=edit)]

    def digest_page(
        self, user: BotUser, day: date | None, page: int = 0, *, edit: bool = False
    ) -> list[Reply]:
        """Темы дайджеста за день; day=None — последний собранный."""
        digest = self._load(day)
        if digest is None:
            text = f"Дайджеста за {day:%d.%m.%Y} нет." if day else "Дайджестов ещё нет."
            return [
                Reply(text, [[Button("📰 Собрать новости", "news"), Button("📚 Другие дайджесты", "dl")]])
            ]
        return [self._topics_page(user, digest, page, edit=edit)]

    def _topics_page(
        self, user: BotUser, digest: Digest, page: int = 0, *, fresh: bool = False, edit: bool = False
    ) -> Reply:
        """Список тем: полные заголовки по разделам в тексте, кнопка на каждую тему."""
        topics = list_topics(digest)
        pages, page = _pages(len(topics), TOPICS_PER_PAGE, page)
        labels = load_labels()["ru"]
        day = digest.date.isoformat()
        title = "☀️ Готов дайджест" if fresh else "📰 Дайджест"
        lines = [f"{title} за {digest.date:%d.%m.%Y} — {service.articles_text(len(topics))}"]
        rates = rates_line(digest, labels)
        if rates:
            lines.append(rates)
        rows, current = [], None
        for topic in topics[page * TOPICS_PER_PAGE : (page + 1) * TOPICS_PER_PAGE]:
            if topic.category != current:
                current = topic.category
                lines += ["", labels.categories.get(current, current)]
            lines.append(f"{topic.number} · {topic.headline}")
            rows.append([Button(_shorten(f"{topic.number} · {topic.headline}", TOPIC_BUTTON_CHARS),
                                f"t:{day}:{topic.number}")])  # fmt: skip
        if topics:
            lines += ["", f"Стр. {page + 1} из {pages}. Выберите тему — покажу подробности и источник."]
        else:
            lines += ["", "В этом дайджесте нет статей."]
        rows += _nav(page, pages, f"dp:{day}:{{}}")
        rows.append(
            [Button("📎 Файлы", f"f:{day}"), Button("📚 Другие дни", "dl"), Button("📋 Меню", "menu")]
        )
        if user.role == "admin" and digest.date == self._today():
            rows.append([Button("🔄 Обновить дайджест", "refresh")])
        return Reply(_shorten("\n".join(lines), MAX_MESSAGE), rows, edit=edit)

    def files(self, day: date) -> list[Reply]:
        """Файлы дайджеста на трёх языках."""
        summary = service.latest_digest(day, settings=self.settings)
        paths = [p for lang, p in summary.files().items() if lang in LANGS and p.exists()] if summary else []
        if not paths:
            return [Reply(f"Файлов дайджеста за {day:%d.%m.%Y} нет.")]
        return [Reply(f"Дайджест за {day:%d.%m.%Y}: русский, английский, узбекский.", files=paths)]

    def topic(self, day: date, number: str) -> list[Reply]:
        """Карточка темы: подробности, документ, ссылка на источник; под ней — пост для площадки."""
        digest = self._load(day)
        if digest is None:
            return self.digest_list()
        back = [Button("◀️ К темам", f"d:{day.isoformat()}"), Button("📋 Меню", "menu")]
        try:
            article = find_article(digest, number)
        except LookupError:
            return [Reply(f"В дайджесте за {day:%d.%m.%Y} нет темы {number}.", [back])]
        number = article.id.rsplit("-", 1)[1]
        page = digest.articles.index(article) // TOPICS_PER_PAGE
        back[0] = Button("◀️ К темам", f"d:{day.isoformat()}:{page}")
        text = topic_card(article, digest.date)
        return [Reply(text, [*self._platform_rows(day, number), back], html=True)]

    def platforms(self, day: date, number: str) -> list[Reply]:
        """«Другая площадка» под постом: та же тема."""
        rows = [*self._platform_rows(day, number), [Button("📄 К теме", f"t:{day.isoformat()}:{number}")]]
        return [Reply(f"Тема {number}. Для какой площадки пост?", rows)]

    def _platform_rows(self, day: date, number: str) -> list[list[Button]]:
        """Кнопка на площадку из config/posts.yaml, пост сразу на её языке по умолчанию."""
        buttons = [
            Button(f"✍️ {p.name}", PostRequest(day, number, code, p.default_lang).data)
            for code, p in self.profiles.platforms.items()
        ]
        return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    # --- Пост ---

    def write(self, request: PostRequest | VoiceRequest) -> list[Reply]:
        """Нажата площадка, язык или режим: ответ «пишу» и пост следом (generate)."""
        if request.platform not in self.profiles.platforms or request.lang not in LANGS:
            raise ValueError(f"нет площадки или языка: {request}")
        if request.mode not in MODES:
            raise ValueError(f"нет режима: {request.mode}")
        name = self.profiles.get(request.platform).name
        if request.mode == "new":
            text = f"Пишу пост для {name} ({LANG_NAMES[request.lang]}), это 10–30 секунд…"
        else:
            text = "Переписываю пост…"
        return [Reply(text, generate=request)]

    def generate(self, user: BotUser, request: PostRequest | VoiceRequest) -> list[Reply]:
        """Долгое действие: обёртка вызывает его в отдельном потоке."""
        with self._lock:
            if user.telegram_id in self.writing:
                return [Reply("Предыдущий пост ещё пишется, подождите немного.")]
            self.writing.add(user.telegram_id)
        try:
            post = self._make_post(user, request)
        except PostLimitReached as exc:
            return [Reply(f"{exc}. Новые посты — завтра.", [[Button("📋 Меню", "menu")]])]
        except PostError as exc:
            return [
                Reply(
                    f"Пост не получился. {exc}",
                    [[Button("🔁 Ещё раз", request.data), request.back]],
                )
            ]
        except LookupError as exc:
            return [Reply(str(exc), [[request.lost]])]
        finally:
            with self._lock:
                self.writing.discard(user.telegram_id)
        profile = self.profiles.get(post.platform)
        source = "из сохранённых" if post.cached else "новый"
        length = f"{post.chars}/{post.max_chars} символов"
        info = (
            f"{profile.name} · {LANG_NAMES.get(post.lang, post.lang)} · {request.label} · {length} · {source}"
        )
        info += f"\n{EXPERT_CHECK}"
        if post.flags:
            info += f"\n⚠️ Проверить: {'; '.join(post.flags)}"
        actions = [
            [Button(text, replace(request, mode=code).data) for code, text in MODES_TEXT.items()],
            [
                Button(f"🌐 {LANG_NAMES[lang]}", replace(request, lang=lang, mode="new").data)
                for lang in LANGS
                if lang != request.lang
            ],  # fmt: skip
            [request.others, request.back],
            [request.dislike(post.id)],
        ]
        replies = [Reply(info)]
        preview = "Так пост будет выглядеть в канале:\n\n"
        if post.platform == "telegram" and len(preview + post.text) <= MAX_MESSAGE:
            replies.append(Reply(preview + telegram_html(post.text), html=True))
        # Отдельным сообщением — удобно копировать. Длиннее лимита — Telegram не примет, а пометка уже есть
        replies.append(Reply(_shorten(post.text, MAX_MESSAGE), actions))
        return replies

    def _make_post(self, user: BotUser, request: PostRequest | VoiceRequest) -> service.Post:
        if isinstance(request, VoiceRequest):
            return service.voice_post(
                request.voice_id, request.platform, request.lang, request.mode, author=user.name,
                settings=self.settings,
            )  # fmt: skip
        return service.post(
            request.number, request.platform, request.lang, request.mode, day=request.day,
            author=user.name, settings=self.settings,
        )  # fmt: skip

    # --- Голосовые ---

    def voice_refusal(self, duration_s: int) -> Reply | None:
        """Отказ до скачивания: длинная запись заняла бы транскрибатор надолго."""
        limit = self.settings.voice.max_seconds
        if duration_s <= limit:
            return None
        return Reply(f"Запись слишком длинная: можно до {limit // 60} мин. Разбейте её на части.")

    def voice_wait(self, duration_s: int) -> Reply:
        minutes = max(1, math.ceil(duration_s * VOICE_SPEED / 60))
        return Reply(f"🎙 Расшифровываю голосовое, это около {minutes} мин. Пришлю текст сюда.")

    def voice_not_downloaded(self) -> Reply:
        text = "Не удалось скачать запись из Telegram, пришлите её ещё раз чуть позже."
        return Reply(text, [[Button("📋 Меню", "menu")]])

    def voice_id(self, message_id: int) -> str:
        return voice.new_id(self.clock(), message_id)

    def voice_incoming(self, voice_id: str) -> Path:
        return service.voice_incoming(voice_id, settings=self.settings)

    def voice_publish(self, voice_id: str) -> None:
        service.voice_publish(voice_id, settings=self.settings)

    def voice_wait_result(self, voice_id: str, duration_s: int) -> list[Reply]:
        """Долгое действие: обёртка вызывает его в отдельном потоке."""
        try:
            service.wait_voice(voice_id, duration_s, settings=self.settings)
        except voice.VoiceError as exc:
            return [Reply(str(exc), [[Button("📋 Меню", "menu")]])]
        return self.voice_transcript(voice_id)

    def voice_transcript(self, voice_id: str) -> list[Reply]:
        """Текст целиком, чтобы человек проверил его до поста, и под ним — выбор площадки."""
        text = service.voice_text(voice_id, settings=self.settings)
        parts = _chunks(text, MAX_MESSAGE - len(VOICE_HEADER))
        replies = [Reply(VOICE_HEADER + parts[0]), *(Reply(part) for part in parts[1:])]
        ask = (
            "Проверьте текст, особенно числа и названия: ошибка распознавания попадёт в пост. "
            "Для какой площадки написать пост?"
        )
        return [*replies, Reply(ask, self._voice_rows(voice_id))]

    def _voice_rows(self, voice_id: str) -> list[list[Button]]:
        return [*self._voice_platforms(voice_id), [Button("✏️ Исправить текст", f"ve:{voice_id}")]]

    def _voice_platforms(self, voice_id: str) -> list[list[Button]]:
        buttons = [
            Button(f"✍️ {p.name}", VoiceRequest(voice_id, code, p.default_lang).data)
            for code, p in self.profiles.platforms.items()
        ]
        return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    def voice_ask_edit(self, user: BotUser, voice_id: str) -> list[Reply]:
        self.awaiting_edit[user.telegram_id] = voice_id
        return [Reply("Пришлите исправленный текст одним сообщением: скопируйте расшифровку и поправьте её.")]

    def _voice_save_edit(self, voice_id: str, text: str) -> list[Reply]:
        if not text.strip():
            return [Reply("Пустой текст не сохранён.", self._voice_rows(voice_id))]
        try:
            service.edit_voice(voice_id, text, settings=self.settings)
        except LookupError as exc:
            return [Reply(str(exc), [[Button("📋 Меню", "menu")]])]
        return [Reply("Текст обновлён. Для какой площадки написать пост?", self._voice_rows(voice_id))]

    # --- Замечания ---

    def ask_feedback(self, user: BotUser, request: PostRequest | VoiceRequest, post_id: int) -> list[Reply]:
        self.awaiting_feedback[user.telegram_id] = (request, post_id)
        return [Reply("Что не так с постом? Напишите одним сообщением — замечание попадёт к оператору.")]

    def text(self, user: BotUser, text: str) -> list[Reply]:
        """Обычное сообщение: исправленная расшифровка, ответ на «👎» или меню."""
        voice_id = self.awaiting_edit.pop(user.telegram_id, None)
        if voice_id is not None:
            return self._voice_save_edit(voice_id, text)
        target = self.awaiting_feedback.pop(user.telegram_id, None)
        if target is None:
            return [self.menu(user)]
        request, post_id = target
        if not text.strip():
            return [Reply("Пустое замечание не записано.")]
        note = f"{text.strip()} [пост {post_id}, {request.platform}, {request.lang}]"
        service.add_feedback(
            "post", note, day=request.day, article_id=request.article_id, author=user.name,
            source="telegram", settings=self.settings,
        )  # fmt: skip
        return [Reply("Спасибо, замечание записано.", [[request.back, Button("📋 Меню", "menu")]])]

    # --- Кнопки ---

    def button(self, user: BotUser, chat_id: int, data: str) -> list[Reply]:
        """Нажатие кнопки по её коду. Непонятный код (кнопка старой версии бота) — меню."""
        self.awaiting_feedback.pop(user.telegram_id, None)
        self.awaiting_edit.pop(user.telegram_id, None)
        try:
            return self._dispatch(user, chat_id, data)
        except ValueError as exc:
            log.info("Бот: непонятная кнопка %r: %s", data, exc)
            return [self.menu(user)]

    def _dispatch(self, user: BotUser, chat_id: int, data: str) -> list[Reply]:
        action, *args = data.split(":")
        if action == "menu":
            return [self.menu(user)]
        if action == "news":
            return self.news(user, chat_id)
        if action == "refresh":
            return self.news(user, chat_id, force=True)
        if action == "status":
            return self.status(user)
        if action == "metrics":
            return self.metrics(user)
        if action == "dl":
            return self.digest_list()
        if action == "dlp":
            [page] = args
            return self.digest_list(int(page), edit=True)
        if action == "d":
            day, *page = args
            return self.digest_page(user, date.fromisoformat(day), int(page[0]) if page else 0)
        if action == "dp":
            day, page = args
            return self.digest_page(user, date.fromisoformat(day), int(page), edit=True)
        if action == "post":  # кнопка прежней версии: темы последнего дайджеста
            return self.digest_page(user, None)
        if action == "f":
            [day] = args
            return self.files(date.fromisoformat(day))
        if action == "t":
            day, number = args
            return self.topic(date.fromisoformat(day), _number(number))
        if action == "o":
            day, number = args
            return self.platforms(date.fromisoformat(day), _number(number))
        if action == "g":
            day, number, platform, lang, mode = args
            return self.write(PostRequest(date.fromisoformat(day), _number(number), platform, lang, mode))
        if action == "fb":
            day, number, platform, lang, post_id = args
            request = PostRequest(date.fromisoformat(day), _number(number), platform, lang)
            return self.ask_feedback(user, request, int(post_id))
        if action == "vg":
            voice_id, platform, lang, mode = args
            return self.write(VoiceRequest(voice.check_id(voice_id), platform, lang, mode))
        if action == "vt":
            [voice_id] = args
            return self._voice_lookup(voice.check_id(voice_id))
        if action == "vo":
            [voice_id] = args
            rows = [*self._voice_platforms(voice.check_id(voice_id)), [VoiceRequest(voice_id, "", "").back]]
            return [Reply("Голосовое. Для какой площадки пост?", rows)]
        if action == "ve":
            [voice_id] = args
            return self.voice_ask_edit(user, voice.check_id(voice_id))
        if action == "vf":
            voice_id, platform, lang, post_id = args
            return self.ask_feedback(
                user, VoiceRequest(voice.check_id(voice_id), platform, lang), int(post_id)
            )
        raise ValueError(f"неизвестное действие {action}")

    def _voice_lookup(self, voice_id: str) -> list[Reply]:
        try:
            return self.voice_transcript(voice_id)
        except LookupError as exc:
            return [Reply(str(exc), [[Button("📋 Меню", "menu")]])]


# --- Тексты ---


def topic_card(article: Article, day: date) -> str:
    """Карточка темы в HTML: заголовок, раздел, краткое и подробное, документ, источник.

    Строки те же, что в русском файле дайджеста. Длинное подробное содержание обрезается, чтобы
    карточка уместилась в одно сообщение.
    """
    t = load_labels()["ru"]
    fields = article_fields(article, "ru", t)
    number = article.id.rsplit("-", 1)[1]

    def card(details: str) -> str:
        lines = [
            f"<b>{_esc(fields['headline'])}</b>",
            f"{_esc(t.categories.get(article.category, article.category))} · тема {number} · {day:%d.%m.%Y}",
        ]
        lines += [f"<i>{_esc(note)}</i>" for note in fields["notes"] if note]
        if fields["check"]:
            lines.append(f"<b>{_esc(fields['check'])}</b>")
        lines += [
            "",
            f"<b>{t.summary}:</b> {_esc(fields['summary'])}",
            "",
            f"<b>{t.details}:</b>",
            _esc(details),
        ]
        if fields["document"]:
            lines += ["", f"<b>{t.document}:</b> {links_html(fields['document'])}"]
        lines += ["", f"<b>{t.source}:</b> {links_html(fields['source'])}"]
        if article.also:
            lines.append(f"<b>{t.also}:</b> {links_html(fields['also'])}")
        lines += ["", "Сделать пост:"]
        return "\n".join(lines)

    return card(_fit(fields["details"], MAX_MESSAGE - len(card(""))))


def _fit(text: str, budget: int) -> str:
    """Начало text, которое после экранирования займёт не больше budget символов, с «…» в конце."""
    if len(_esc(text)) <= budget:
        return text
    size = 0
    for n, char in enumerate(text):
        size += len(_esc(char))
        if size > budget - 1:
            return text[:n].rstrip() + "…"
    return text


def links_html(text: str) -> str:
    """Строка с Markdown-ссылками [имя](адрес) → HTML для Telegram; всё остальное экранировано."""
    parts, last = [], 0
    for match in MD_LINK.finditer(text):
        parts.append(_esc(text[last : match.start()]))
        parts.append(f'<a href="{html.escape(match[2])}">{_esc(match[1])}</a>')
        last = match.end()
    parts.append(_esc(text[last:]))
    return "".join(parts)


def telegram_html(text: str) -> str:
    """Пост для превью в Telegram: **жирный** → <b>, остальное экранировано."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _esc(text), flags=re.S)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _number(value: str) -> str:
    if not value.isdigit():
        raise ValueError(f"номер темы {value}")
    return value


def _pages(total: int, per_page: int, page: int) -> tuple[int, int]:
    """Число страниц и номер страницы в допустимых пределах."""
    pages = max(1, -(-total // per_page))
    return pages, min(max(page, 0), pages - 1)


def _nav(page: int, pages: int, code: str) -> list[list[Button]]:
    nav = []
    if page > 0:
        nav.append(Button("◀️", code.format(page - 1)))
    if page < pages - 1:
        nav.append(Button("▶️", code.format(page + 1)))
    return [nav] if nav else []


def _chunks(text: str, limit: int) -> list[str]:
    """Режет длинный текст на сообщения по пробелам, ничего не теряя."""
    parts = []
    while len(text) > limit:
        cut = max(text.rfind(" ", 0, limit), text.rfind("\n", 0, limit))
        cut = cut if cut > 0 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [*parts, text]


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
