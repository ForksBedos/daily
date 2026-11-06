"""Эксплуатация (docs/plan/stage-8-automation.md): журналы, блокировка запусков, бэкап БД, уведомители."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from uzdigest.redact import MaskingFormatter
from uzdigest.timeutil import from_iso, to_iso, utc_now

log = logging.getLogger(__name__)

LOCK_STALE_H = 2  # блокировка старше — зависший запуск: снимается
BACKUP_NAME = re.compile(r"^uzdigest-\d{4}-\d{2}-\d{2}\.sqlite3$")  # только такие копии чистятся
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 5


class SharedLogHandler(RotatingFileHandler):
    """Журнал, который могут держать открытым другие процессы.

    На Windows такой файл нельзя переименовать. Обычный обработчик при этом теряет все записи подряд
    и на каждой попытке сдвигает старые копии. Здесь запись важнее ротации: файл занят — пишем в него
    дальше, ротация случится со следующей записью, когда файл освободится.
    """

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        probe = f"{self.baseFilename}.rotating"
        try:
            os.rename(self.baseFilename, probe)
            os.rename(probe, self.baseFilename)
        except OSError:
            self.stream = self._open()
            return
        super().doRollover()


def log_file(command: str) -> str:
    """Бот работает всё время и держит свой файл открытым: журнал конвейера иначе не смог бы ротироваться."""
    return "bot.log" if command == "bot" else "uzdigest.log"


def setup_logging(logs_dir: Path, name: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = SharedLogHandler(
        logs_dir / name, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)  # подробности — в файле, в консоли только проблемы
    for handler in (file_handler, console_handler):
        handler.setFormatter(MaskingFormatter(LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class LockInfo:
    kind: str  # collect | digest | run
    pid: int | None
    started_at: datetime


class Busy(Exception):
    """Другой запуск уже идёт."""

    def __init__(self, info: LockInfo):
        super().__init__(f"уже идёт {info.kind} с {info.started_at.isoformat()} (pid {info.pid})")
        self.info = info


def read_lock(path: Path) -> LockInfo | None:
    """Кто держит блокировку. Испорченный файл — время берётся из даты файла."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(data["kind"], data.get("pid"), from_iso(data["started_at"]))
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        return LockInfo("?", None, datetime.fromtimestamp(path.stat().st_mtime).astimezone())


@contextmanager
def run_lock(path: Path, kind: str, now: datetime | None = None) -> Iterator[None]:
    """Один запуск сбора или сборки на одну БД. Занято — Busy; блокировка старше LOCK_STALE_H снимается."""
    now = now or utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = read_lock(path)
            if info is not None and now - info.started_at < timedelta(hours=LOCK_STALE_H):
                raise Busy(info) from None
            log.warning("Снимаю зависшую блокировку %s: %s", path, info)
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"kind": kind, "pid": os.getpid(), "started_at": to_iso(now)}, file)
        break
    else:
        raise Busy(read_lock(path) or LockInfo(kind, None, now))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def backup_db(conn: sqlite3.Connection, backup_dir: Path, day: date, keep_last: int | None) -> Path:
    """Копия БД за день (повторная сборка в тот же день её обновляет).

    keep_last — сколько дневных копий хранить; удаляются только файлы uzdigest-ГГГГ-ММ-ДД.sqlite3,
    ручные копии вроде uzdigest-before-006.sqlite3 не трогаются. None — ничего не удаляется.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"uzdigest-{day.isoformat()}.sqlite3"
    temp = target.with_name(f".{target.name}.tmp")
    copy = sqlite3.connect(temp)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    os.replace(temp, target)
    if keep_last:
        daily = sorted(p for p in backup_dir.iterdir() if BACKUP_NAME.match(p.name))
        for old in daily[:-keep_last]:
            log.info("Удаляю старую копию БД %s (храним %s)", old.name, keep_last)
            old.unlink()
    return target


# --- Уведомители: вызываются после каждого run, сбой одного не роняет запуск ---


def _log_notifier(result: Any) -> None:
    log.info("Итог запуска: %s", result)


def _telegram_notifier(result: Any) -> None:
    from uzdigest.bot.notifier import telegram_notifier

    telegram_notifier(result)


NOTIFIERS: dict[str, Callable[[Any], None]] = {"log": _log_notifier, "telegram": _telegram_notifier}


@contextmanager
def single_instance(path: Path) -> Iterator[None]:
    """Один экземпляр долгого процесса (бота). Блокировку держит ОС: процесс упал — она снята сама."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy(LockInfo("bot", None, utc_now())) from None
        yield
    finally:
        file.close()


def notify(names: list[str], result: Any, notifiers: dict[str, Callable[[Any], None]] | None = None) -> None:
    registry = NOTIFIERS if notifiers is None else notifiers
    for name in names:
        notifier = registry.get(name)
        if notifier is None:
            log.warning("Неизвестный уведомитель %s в settings.yaml (notify)", name)
            continue
        try:
            notifier(result)
        except Exception:
            log.exception("Уведомитель %s не сработал", name)
