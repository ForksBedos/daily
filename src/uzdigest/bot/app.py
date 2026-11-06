"""Запуск Telegram-бота: long polling, белый список, ответы BotCore (docs/plan/stage-10-telegram-bot.md).

Бот сам опрашивает Telegram: сервер, публичный адрес и открытые порты не нужны. Второй экземпляр
не запускается (data/bot.lock). Долгие действия — пост и ожидание сбора — не блокируют другие кнопки.
Фоновая задача раз в 20 с спрашивает BotCore.poll: готов ли новый дайджест для утренней рассылки
и закончился ли сбор, который ждут.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from pathlib import Path

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update, Voice
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from uzdigest.bot.core import DENIED, BotCore, Reply
from uzdigest.config import Settings
from uzdigest.ops import single_instance

log = logging.getLogger(__name__)

POLL_S = 20  # как часто проверять новый дайджест и конец сбора, который ждут пользователи
VOICE_DOWNLOAD_S = 60  # запись на 5 минут — около мегабайта, но сеть бывает медленной
VOICE_DOWNLOAD_TRIES = 2


class BotTokenMissing(RuntimeError):
    pass


def run_bot(settings: Settings) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise BotTokenMissing(
            "Нет TELEGRAM_BOT_TOKEN: впишите токен от @BotFather в .env (см. docs/operations.md)"
        )
    if not settings.bot.users:
        raise BotTokenMissing("В config/settings.yaml нет пользователей бота (bot.users)")
    with single_instance(settings.db_path.parent / "bot.lock"):
        core = BotCore(settings)
        app = (
            Application.builder()
            .token(token)
            .post_init(_post_init)
            .post_shutdown(_post_shutdown)
            .concurrent_updates(True)
            .build()
        )
        app.bot_data["core"] = core
        app.add_handler(CommandHandler(["start", "menu"], _on_start))
        app.add_handler(CallbackQueryHandler(_on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
        app.add_handler(MessageHandler(filters.VOICE, _on_voice))
        app.add_error_handler(_on_error)
        log.info("Бот запущен, пользователей: %s", len(settings.bot.users))
        app.run_polling(allowed_updates=["message", "callback_query"])


async def _post_init(app: Application) -> None:
    # Задача живёт всё время работы бота; ссылка в bot_data, чтобы её остановить при выходе
    app.bot_data["watcher"] = asyncio.get_running_loop().create_task(_watch(app), name="watch-digest")


async def _post_shutdown(app: Application) -> None:
    watcher = app.bot_data.get("watcher")
    if watcher is not None:
        watcher.cancel()


async def _watch(app: Application) -> None:
    """Новый дайджест за сегодня — темы всем из списка; сбор по кнопке закончился — тем, кто ждал."""
    core: BotCore = app.bot_data["core"]
    while True:
        await asyncio.sleep(POLL_S)
        try:
            messages = await asyncio.to_thread(core.poll)
        except Exception:
            log.exception("Бот: проверка дайджеста не удалась")
            continue
        await _send_all(app, core, messages)


async def _send_all(app: Application, core: BotCore, messages: list[tuple[int, Reply]]) -> None:
    """Отправляет ответы фоновой проверки. Рассылка закрывается только по настоящему исходу отправки."""
    delivered: dict[date, int] = {}
    for chat_id, reply in messages:
        try:
            await _send(app, chat_id, reply)
        except (Forbidden, BadRequest) as exc:
            # Человек не писал боту или заблокировал его: повтор не поможет
            log.warning("Бот: не удалось отправить в чат %s: %s", chat_id, exc)
            if reply.morning:
                core.mail_done(chat_id, reply.morning)
        except TelegramError as exc:  # сеть или лимит Telegram: рассылка повторится при следующей проверке
            log.warning("Бот: не удалось отправить в чат %s, повторю: %s", chat_id, exc)
        else:
            if reply.morning:
                core.mail_done(chat_id, reply.morning)
                delivered[reply.morning] = delivered.get(reply.morning, 0) + 1
    for day, count in delivered.items():
        log.info("Бот: дайджест за %s доставлен, получателей %s", day, count)


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core: BotCore = context.bot_data["core"]
    user = core.user(update.effective_user.id if update.effective_user else None)
    await _send(context.application, update.effective_chat.id, core.menu(user) if user else Reply(DENIED))


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core: BotCore = context.bot_data["core"]
    user = core.user(update.effective_user.id if update.effective_user else None)
    chat_id = update.effective_chat.id
    if user is None:
        await _send(context.application, chat_id, Reply(DENIED))
        return
    await _deliver(context.application, chat_id, user, core.text(user, update.message.text))


async def _on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовое: скачать, отдать посреднику, дождаться расшифровки в отдельном потоке."""
    core: BotCore = context.bot_data["core"]
    user = core.user(update.effective_user.id if update.effective_user else None)
    chat_id = update.effective_chat.id
    if user is None:
        await _send(context.application, chat_id, Reply(DENIED))
        return
    message = update.message
    duration = message.voice.duration
    refusal = core.voice_refusal(duration)
    if refusal is not None:
        await _send(context.application, chat_id, refusal)
        return
    await _send(context.application, chat_id, core.voice_wait(duration))
    voice_id = core.voice_id(message.message_id)
    if not await _download_voice(message.voice, core.voice_incoming(voice_id)):
        await _send(context.application, chat_id, core.voice_not_downloaded())
        return
    core.voice_publish(voice_id)
    log.info("Бот: %s прислал голосовое %s, %s с", user.name, voice_id, duration)
    for reply in await asyncio.to_thread(core.voice_wait_result, voice_id, duration):
        await _send(context.application, chat_id, reply)


async def _download_voice(tg_voice: Voice, path: Path) -> bool:
    """Telegram готовит файл дольше обычных 5 секунд ожидания, особенно пересланный. Сбой — один повтор."""
    for attempt in range(1, VOICE_DOWNLOAD_TRIES + 1):
        try:
            file = await tg_voice.get_file(read_timeout=VOICE_DOWNLOAD_S)
            await file.download_to_drive(path, read_timeout=VOICE_DOWNLOAD_S)
        except TelegramError as exc:
            log.warning("Бот: голосовое не скачано, попытка %s: %s", attempt, exc)
        else:
            return True
    return False


async def _on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    core: BotCore = context.bot_data["core"]
    user = core.user(query.from_user.id)
    if user is None:
        await _answer(query, DENIED, show_alert=True)
        return
    await _answer(query)
    chat_id = update.effective_chat.id
    log.info("Бот: %s нажал %s", user.name, (query.data or "").split(":")[0])
    replies = await asyncio.to_thread(core.button, user, chat_id, query.data or "")
    if replies and replies[0].edit and await _edit(query, replies[0]):
        replies = replies[1:]
    await _deliver(context.application, chat_id, user, replies)


async def _answer(query: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Подтверждает нажатие. Нажатие старше ~15 с (бот был выключен, ноутбук спал) Telegram уже
    не подтверждает — тогда нажатие всё равно обрабатывается."""
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramError as exc:
        log.info("Бот: нажатие не подтверждено: %s", exc)


async def _deliver(app: Application, chat_id: int, user, replies: list[Reply]) -> None:
    """Отправляет ответы; если ответ просит написать пост — пишет его в фоне и отправляет следом."""
    core: BotCore = app.bot_data["core"]
    for reply in replies:
        await _send(app, chat_id, reply)
        if reply.generate:
            await app.bot.send_chat_action(chat_id, ChatAction.TYPING)
            for result in await asyncio.to_thread(core.generate, user, reply.generate):
                await _send(app, chat_id, result)


async def _edit(query: CallbackQuery, reply: Reply) -> bool:
    """Листание: меняет сообщение с кнопкой. Не вышло (сообщение слишком старое) — ответ придёт новым."""
    try:
        await query.edit_message_text(
            reply.text, parse_mode=_parse_mode(reply), reply_markup=_markup(reply),
            disable_web_page_preview=True,
        )  # fmt: skip
    except TelegramError as exc:
        log.info("Бот: не удалось изменить сообщение: %s", exc)
        return False
    return True


async def _send(app: Application, chat_id: int, reply: Reply) -> None:
    if reply.text:
        await app.bot.send_message(
            chat_id, reply.text, parse_mode=_parse_mode(reply), reply_markup=_markup(reply),
            disable_web_page_preview=True,
        )  # fmt: skip
    for path in reply.files:
        with path.open("rb") as file:
            await app.bot.send_document(chat_id, file, filename=path.name)


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(b.text, callback_data=b.data) for b in row] for row in reply.buttons if row]
    return InlineKeyboardMarkup(rows) if rows else None


def _parse_mode(reply: Reply) -> str | None:
    return ParseMode.HTML if reply.html else None


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update is None and isinstance(context.error, NetworkError):
        # Обрыв при опросе Telegram: библиотека повторяет сама, 140 строк трассировки здесь ни к чему
        log.warning("Бот: обрыв связи с Telegram, соединение восстановится само: %s", context.error)
        return
    log.error("Бот: ошибка обработки: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id, "Что-то пошло не так, попробуйте ещё раз."
            )
        except Exception:
            log.exception("Бот: не удалось сообщить об ошибке")
