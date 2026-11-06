"""Уведомитель telegram для settings.yaml (notify): админам бота — о неудачном запуске.

Сообщение уходит, если запуск упал, Claude не обработал часть статей или остановился, в дайджесте нет
статей, не ответил ни один источник. Без токена бота уведомитель молчит. Токен стоит в адресе запроса,
поэтому ошибки сети пересказываются без адреса — токен не попадает в лог.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import httpx

from uzdigest.config import Settings, load_settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def should_notify(result: Any) -> bool:
    if result.status == "failed":
        return True
    digest, collect = result.digest, result.collect
    if collect and collect.sources and all(s.status == "error" for s in collect.sources):
        return True
    if digest is None or digest.status == "exists":
        return False
    return bool(digest.no_llm or not digest.articles or (digest.stats.get("llm") or {}).get("stopped"))


def telegram_notifier(
    result: Any, settings: Settings | None = None, post: Callable[..., httpx.Response] = httpx.post
) -> None:
    from uzdigest.service import describe_run  # service импортирует ops, ops — этот модуль

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not should_notify(result):
        return
    settings = settings or load_settings()
    text = f"⚠️ UzDigest, {result.kind}: {describe_run(result)}"
    for admin in settings.bot.admins():
        try:
            response = post(
                API.format(token=token), json={"chat_id": admin.telegram_id, "text": text}, timeout=20
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Telegram не принял сообщение для {admin.name}: {type(exc).__name__}"
            ) from None
