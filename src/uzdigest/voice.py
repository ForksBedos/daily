"""Голосовые сообщения: обмен файлами между ботом и посредником (docs/operations.md, «Голосовые посты»).

Бот живёт в контейнере Linux, транскрибатор — программа Windows, связь только через общую папку
voice.dir. В ней на одно голосовое приходится один идентификатор, он же имя файлов:

  ID.ogg.part  бот качает запись               ID.txt         расшифровка от посредника
  ID.ogg       запись готова, её берёт посредник  ID.err       причина сбоя, текст для пользователя
  ID.edited.txt  текст, исправленный человеком   bridge.beat   посредник жив: содержимое меняется

Часы контейнера и Windows могут расходиться, поэтому жизнь посредника определяется не по времени
в файле, а по тому, меняется ли он.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from uzdigest.timeutil import TASHKENT

log = logging.getLogger(__name__)

BEAT = "bridge.beat"
BEAT_GAP_S = 30  # столько отметка не менялась — посредник не работает
POLL_S = 2
_ID = re.compile(r"\d{8}-\d{6}-\d+")
BRIDGE_DOWN = "Транскрибатор на ноутбуке не запущен, расшифровать запись не получилось. Сообщите админу."
TOO_LONG = "Расшифровка идёт слишком долго, я её отменил. Попробуйте запись покороче."


class VoiceError(Exception):
    """Расшифровки нет: причина — в тексте, его можно показать пользователю как есть."""


def new_id(moment: datetime, message_id: int) -> str:
    return f"{moment.astimezone(TASHKENT):%Y%m%d-%H%M%S}-{message_id}"


def check_id(voice_id: str) -> str:
    """Идентификатор приходит из данных кнопки и становится именем файла: только точный формат."""
    if not _ID.fullmatch(voice_id):
        raise ValueError(f"Неверный идентификатор голосового: {voice_id}")
    return voice_id


def day_of(voice_id: str) -> date:
    return datetime.strptime(check_id(voice_id)[:8], "%Y%m%d").date()


def incoming(folder: Path, voice_id: str) -> Path:
    """Куда качать запись: посредник такой файл не видит, пока он не готов."""
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{check_id(voice_id)}.ogg.part"


def publish(folder: Path, voice_id: str) -> None:
    os.replace(incoming(folder, voice_id), folder / f"{voice_id}.ogg")


def wait_transcript(
    folder: Path,
    voice_id: str,
    timeout_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Ждёт расшифровку. Сдавшись, отменяет запись: включившись позже, посредник её не возьмёт."""
    started = beat_at = monotonic()
    beat = _beat(folder)
    while True:
        text = _read(folder / f"{voice_id}.txt")
        if text is not None:
            return text
        error = _read(folder / f"{voice_id}.err")
        if error is not None:
            raise VoiceError(error)
        now = monotonic()
        current = _beat(folder)
        if current != beat:
            beat, beat_at = current, now
        if now - beat_at >= BEAT_GAP_S:
            _cancel(folder, voice_id, BRIDGE_DOWN)
        if now - started >= timeout_s:
            _cancel(folder, voice_id, TOO_LONG)
        sleep(POLL_S)


def read_transcript(folder: Path, voice_id: str) -> str:
    """Текст для поста: исправленный человеком, иначе расшифровка."""
    check_id(voice_id)
    for name in (f"{voice_id}.edited.txt", f"{voice_id}.txt"):
        text = _read(folder / name)
        if text is not None:
            return text
    raise LookupError("Расшифровка этого голосового не найдена, пришлите запись ещё раз.")


def save_edit(folder: Path, voice_id: str, text: str) -> None:
    read_transcript(folder, voice_id)
    write_atomic(folder / f"{voice_id}.edited.txt", text.strip())


def write_atomic(path: Path, text: str) -> None:
    """Другая сторона видит файл только целиком."""
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def _beat(folder: Path) -> str | None:
    return _read(folder / BEAT)


def _cancel(folder: Path, voice_id: str, reason: str) -> None:
    write_atomic(folder / f"{voice_id}.err", reason)
    log.warning("Голосовое %s отменено: %s", voice_id, reason)
    raise VoiceError(reason)
