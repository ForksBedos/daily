"""Посредник между ботом и транскрибатором (docs/operations.md, «Голосовые посты»).

Работает на Windows, вне контейнера: движок транскрибатора доступен только через именованный канал
Windows, а из Linux-контейнера канал не виден. Посредник берёт записи из общей папки, отдаёт движку
и кладёт рядом расшифровку или причину сбоя (uzdigest/voice.py). Запускается задачей UzDigest-Voice:

  .venv\\Scripts\\pythonw.exe -m uzdigest.voice_bridge --engine ...\\Dictation.Engine.exe

Транскрибатор закрыт — посредник запускает движок сам. Приложение при старте убивает чужой движок
и поднимает свой: обрыв канала посреди записи — один повтор. Тексты в журнал не пишутся.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from uzdigest import voice
from uzdigest.config import load_settings
from uzdigest.ops import Busy, setup_logging, single_instance

log = logging.getLogger(__name__)

PIPE = r"\\.\pipe\dictation-engine"
STAGE_DONE, STAGE_ERROR = 5, 6  # FileTranscriptionStage в транскрибаторе
BEAT_EVERY_S = 5
SCAN_EVERY_S = 2
READY_WAIT_S = 180  # запуск движка и загрузка модели
NO_SPEECH = "В записи не слышно речи."
FAILED = "Не удалось расшифровать запись, попробуйте прислать её ещё раз."
ENGINE_DOWN = "Транскрибатор на ноутбуке не запустился. Сообщите админу."


class EngineDown(Exception):
    """Движок недоступен или не загрузил модель."""


class EngineFailed(Exception):
    """Движок работает, но эту запись расшифровать не смог."""


def _open_pipe() -> Any:
    return open(PIPE, "r+b", buffering=0)


def start_engine(exe: Path) -> None:
    """Движок ищет настройки и модели вверх от рабочей папки: запускаем его из корня транскрибатора."""
    home = next((p for p in exe.parents if (p / "DictationApp.slnx").exists()), exe.parent)
    subprocess.Popen(
        [str(exe)], cwd=home, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )  # fmt: skip
    log.info("Движок транскрибатора запущен")


class Engine:
    def __init__(
        self,
        *,
        start: Callable[[], None],
        open_pipe: Callable[[], Any] = _open_pipe,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wait_s: float = READY_WAIT_S,
    ) -> None:
        self.start = start
        self.open_pipe = open_pipe
        self.sleep = sleep
        self.monotonic = monotonic
        self.wait_s = wait_s

    def transcribe(self, audio: Path, out_dir: Path) -> Path:
        """Расшифровывает файл, возвращает путь к тексту."""
        self._wait_ready()
        out_dir.mkdir(parents=True, exist_ok=True)
        sources = [{"Value": str(audio), "IsUrl": False}]
        options = {"WriteTxt": True, "WriteSrt": False, "WriteVtt": False, "OutputDirectory": str(out_dir)}
        lines = self._call(
            "transcribe-folder",
            {"sourcesJson": json.dumps(sources), "optionsJson": json.dumps(options)},
        )
        final = lines[-1]
        if final.get("type") == "error":
            raise EngineFailed(final.get("error") or "движок вернул ошибку")
        stages = [json.loads(line["text"]) for line in lines[:-1] if line.get("text")]
        done = next((s for s in stages if s.get("Stage") == STAGE_DONE), None)
        if done is None:
            reasons = [s.get("Error") for s in stages if s.get("Stage") == STAGE_ERROR]
            raise EngineFailed(next(filter(None, reasons), "движок не вернул результат"))
        return Path(done["OutputFiles"][0])

    def _wait_ready(self) -> None:
        """Канал открывается раньше, чем загружена модель: файл, отправленный сразу, вернётся с ошибкой."""
        deadline = self.monotonic() + self.wait_s
        started = False
        while True:
            try:
                data = self._call("status")[-1].get("data") or {}
                if data.get("loadedModel", "none") != "none":
                    return
            except FileNotFoundError:  # канала нет — движок закрыт
                if not started:
                    self.start()
                    started = True
            except OSError as exc:  # все экземпляры канала заняты или движок перезапускается
                log.info("Движок пока недоступен: %s", exc)
            if self.monotonic() >= deadline:
                raise EngineDown(f"движок не готов за {self.wait_s} с")
            self.sleep(SCAN_EVERY_S)

    def _call(self, command: str, parameters: dict[str, str] | None = None) -> list[dict]:
        """Запрос одной строкой JSON; ответ — строки хода работы и последняя, итоговая."""
        request = {"command": command, "parameters": parameters}
        lines = []
        with self.open_pipe() as pipe:
            pipe.write(json.dumps(request).encode("utf-8") + b"\n")
            pipe.flush()
            while raw := pipe.readline():
                lines.append(json.loads(raw))
                if lines[-1].get("type") != "partialText":
                    return lines
        raise OSError("канал закрылся до итогового ответа")


def pending(folder: Path) -> list[Path]:
    """Готовые записи без расшифровки и без отметки о сбое, старые первыми."""
    return [
        audio
        for audio in sorted(folder.glob("*.ogg"))
        if not audio.with_suffix(".txt").exists() and not audio.with_suffix(".err").exists()
    ]


def process_pending(folder: Path, engine: Any) -> None:
    for audio in pending(folder):
        started = time.monotonic()
        try:
            text = _transcript(audio, folder / "work", engine)
        except (EngineDown, OSError) as exc:
            log.error("Голосовое %s: движок недоступен: %s", audio.stem, exc)
            voice.write_atomic(audio.with_suffix(".err"), ENGINE_DOWN)
        except Exception:
            log.exception("Голосовое %s: расшифровка не удалась", audio.stem)
            voice.write_atomic(audio.with_suffix(".err"), FAILED)
        else:
            if text:
                voice.write_atomic(audio.with_suffix(".txt"), text)
            else:
                voice.write_atomic(audio.with_suffix(".err"), NO_SPEECH)
            log.info("Голосовое %s: %s символов за %.0f с", audio.stem, len(text), time.monotonic() - started)


def _transcript(audio: Path, out_dir: Path, engine: Any) -> str:
    try:
        path = engine.transcribe(audio, out_dir)
    except OSError as exc:
        log.warning("Голосовое %s: обрыв канала, повторяю: %s", audio.stem, exc)
        path = engine.transcribe(audio, out_dir)
    return path.read_text(encoding="utf-8-sig").strip()


def _beat(folder: Path) -> None:
    """Отдельный поток: пока идёт длинная расшифровка, бот всё равно видит, что посредник жив."""
    count = 0
    while True:
        count += 1
        try:
            (folder / voice.BEAT).write_text(str(count), encoding="utf-8")
        except OSError as exc:  # файл на миг занят контейнером: следующая отметка пройдёт
            log.warning("Посредник: отметка не записана: %s", exc)
        time.sleep(BEAT_EVERY_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Посредник между ботом и транскрибатором")
    parser.add_argument("--engine", type=Path, required=True, help="путь к Dictation.Engine.exe")
    args = parser.parse_args(argv)
    settings = load_settings()
    setup_logging(settings.logs_dir, "voice_bridge.log")
    folder = settings.voice.dir
    folder.mkdir(parents=True, exist_ok=True)
    engine = Engine(start=lambda: start_engine(args.engine))
    try:
        with single_instance(folder / "bridge.lock"):
            log.info("Посредник запущен, папка %s", folder)
            threading.Thread(target=_beat, args=(folder,), daemon=True, name="beat").start()
            while True:
                try:
                    process_pending(folder, engine)
                except (
                    Exception
                ):  # посредник работает месяцами: сбой одного круга не должен его останавливать
                    log.exception("Посредник: сбой при обработке папки")
                time.sleep(SCAN_EVERY_S)
    except Busy:
        log.info("Посредник уже запущен")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
