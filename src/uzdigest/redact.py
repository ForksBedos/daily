"""Маскировка секретов: токены не должны попасть в логи, runs.stats_json и тексты ошибок."""

from __future__ import annotations

import logging
import os
import re

MASK = "***"
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
MIN_SECRET_CHARS = 8  # короче — не секрет, а ошибка в .env: маскировка съела бы обычный текст
SECRET_SHAPES = (
    re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}"),  # токен Telegram, в том числе внутри адреса /bot…/
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),  # токены и ключи Anthropic
)


def mask(text: str) -> str:
    """Заменяет секреты из окружения и всё, что похоже на токен, на ***."""
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= MIN_SECRET_CHARS:
            text = text.replace(value, MASK)
    for shape in SECRET_SHAPES:
        text = shape.sub(MASK, text)
    return text


class MaskingFormatter(logging.Formatter):
    """Маскирует готовую строку целиком: и сообщение, и текст исключения."""

    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))
