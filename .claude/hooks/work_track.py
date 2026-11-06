#!/usr/bin/env python3
"""Учёт работы чатов с ИИ в этом проекте. Его вызывает Claude Code из хуков.

SessionStart      говорит новому чату, где остановилась работа (docs/PROGRESS.md).
UserPromptSubmit  и PostToolUse следят за заполнением контекста и напоминают при 65, 75 и 85%.
Stop              дописывает строку о чате в logs/ai_work/chats.jsonl: токены, вызовы, ошибки, прерывания.

Процент контекста хуки не получают, поэтому он берётся из журнала самого чата: последний ответ
говорит, сколько токенов прочитал. Проверено 04.11.2026: 541 816 здесь против 541 943 в приложении.

    python .claude/hooks/work_track.py --report      таблица чатов
    python .claude/hooks/work_track.py --backfill    добавить чаты, которые уже есть на этом компьютере
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
WORK_DIR = PROJECT_DIR / "logs" / "ai_work"
CHATS_FILE = WORK_DIR / "chats.jsonl"
PROGRESS_FILE = PROJECT_DIR / "docs" / "PROGRESS.md"
DEFAULT_WINDOW = 1_000_000  # окно моделей, как его показывает приложение; другая модель: CONTEXT_WINDOW_TOKENS
HANDOFF_PERCENT = 65
REMIND_STEP = 10
TAIL_BYTES = 2_000_000


def window_size() -> int:
    return int(os.environ.get("CONTEXT_WINDOW_TOKENS") or DEFAULT_WINDOW)


def records(path: Path, tail_only: bool = False) -> list[dict]:
    """Строки журнала чата. Строка, обрезанная хвостом или незаконченной записью, пропускается."""
    with open(path, "rb") as f:
        if tail_only:
            f.seek(max(0, path.stat().st_size - TAIL_BYTES))
        lines = f.read().split(b"\n")
    found = []
    for line in lines:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def context_tokens(items: list[dict]) -> int | None:
    """Сколько прочитал последний ответ основного чата: это и есть контекст, как считает приложение."""
    for item in reversed(items):
        usage = (item.get("message") or {}).get("usage") if item.get("type") == "assistant" else None
        if usage and not item.get("isSidechain"):
            return sum(
                int(usage.get(key) or 0)
                for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
    return None


def owner_text(item: dict) -> str:
    """Слова владельца в строке журнала; пусто у ответов инструментов и служебных строк."""
    if item.get("type") != "user" or item.get("isSidechain") or item.get("isMeta"):
        return ""
    content = (item.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    text = content.strip() if isinstance(content, str) else ""
    return "" if text.startswith("<") else text


def summarize(path: Path) -> dict:
    """Один чат в числах."""
    items = records(path)
    stamps = [i["timestamp"] for i in items if i.get("timestamp")]
    texts = [t for t in map(owner_text, items) if t]
    asks = [t for t in texts if not t.startswith("[Request interrupted")]
    tool_calls = tool_errors = peak = 0
    model = ""
    for item in items:
        if item.get("isSidechain"):
            continue
        message = item.get("message") or {}
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if item.get("type") == "assistant":
            tool_calls += sum(b.get("type") == "tool_use" for b in blocks)
            model = message.get("model") or model
            peak = max(peak, context_tokens([item]) or 0)
        elif item.get("type") == "user":
            tool_errors += sum(b.get("type") == "tool_result" and bool(b.get("is_error")) for b in blocks)
    return {
        "chat": path.stem,
        "folder": next((i["cwd"] for i in items if i.get("cwd")), ""),
        "started": stamps[0] if stamps else "",
        "last": stamps[-1] if stamps else "",
        "minutes": round(_minutes(stamps), 1),
        "owner_messages": len(asks),
        "interruptions": len(texts) - len(asks),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "peak_context_tokens": peak,
        "peak_context_percent": round(peak * 100 / window_size(), 1),
        "model": model,
        "task": " ".join(asks[0].split())[:90] if asks else "",
    }


def _minutes(stamps: list[str]) -> float:
    if len(stamps) < 2:
        return 0.0
    first, last = (datetime.fromisoformat(s.replace("Z", "+00:00")) for s in (stamps[0], stamps[-1]))
    return (last - first).total_seconds() / 60


def remember(line: dict) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHATS_FILE, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def where_we_stopped() -> str:
    """Первый раздел docs/PROGRESS.md: с него начинается новый чат."""
    if not PROGRESS_FILE.exists():
        return ""
    text = PROGRESS_FILE.read_text(encoding="utf-8")
    match = re.search(r"^## .*?(?=^## |\Z)", text, re.M | re.S)
    return match.group(0).strip()[:3000] if match else ""


def context_reminder(payload: dict) -> str | None:
    """Напоминание, которое пора показать, по одному на ступень: 65%, потом 75%, потом 85%."""
    transcript, chat = payload.get("transcript_path"), payload.get("session_id", "chat")
    if not transcript or not Path(transcript).exists():
        return None
    tokens = context_tokens(records(Path(transcript), tail_only=True))
    if tokens is None:
        return None
    percent = tokens * 100 // window_size()
    if percent < HANDOFF_PERCENT:
        return None
    level = HANDOFF_PERCENT + (percent - HANDOFF_PERCENT) // REMIND_STEP * REMIND_STEP
    mark = WORK_DIR / "reminded" / f"{chat}.txt"
    if mark.exists() and int(mark.read_text(encoding="utf-8") or 0) >= level:
        return None
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(str(level), encoding="utf-8")
    remember(
        {
            "event": "context_reminder",
            "chat": chat,
            "percent": percent,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return (
        f"Контекст заполнен на {percent}% ({tokens:,} токенов из {window_size():,}). Правило владельца: при 65% "
        "работа переходит в новый чат. Закончи текущий шаг и выполни раздел «Продолжение в следующем чате» "
        "из AGENTS.md: обнови docs/PROGRESS.md и docs/LESSONS.md, затем начни новый чат."
    ).replace(",", " ")


def handle(payload: dict) -> dict | None:
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        stopped = where_we_stopped()
        if not stopped:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": f"Состояние работ из docs/PROGRESS.md:\n{stopped}",
            }
        }
    if event in ("UserPromptSubmit", "PostToolUse"):
        reminder = context_reminder(payload)
        if not reminder:
            return None
        return {
            "systemMessage": reminder.split(". ")[0] + ".",
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": reminder},
        }
    if event == "Stop" and payload.get("transcript_path"):
        remember(
            {
                "event": "turn_end",
                "at": datetime.now().isoformat(timespec="seconds"),
                **summarize(Path(payload["transcript_path"])),
            }
        )
    return None


def latest_chats() -> list[dict]:
    """Последняя строка каждого чата: каждый ход дописывает новый снимок того же чата."""
    if not CHATS_FILE.exists():
        return []
    chats: dict[str, dict] = {}
    for line in CHATS_FILE.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("event") == "turn_end":
            chats[item["chat"]] = item
    return sorted(chats.values(), key=lambda c: c["started"])


def report() -> str:
    chats = latest_chats()
    if not chats:
        return "Записей о чатах пока нет. Добавить чаты с этого компьютера: --backfill"
    lines = ["Начало            Минут  Сообщ. Прерв. Вызовы Ошибки Контекст  Задача"]
    for c in chats:
        lines.append(
            f"{c['started'][:16].replace('T', ' ')}  {c['minutes']:>5.0f}  {c['owner_messages']:>5}  "
            f"{c['interruptions']:>5}  {c['tool_calls']:>5}  {c['tool_errors']:>5}  "
            f"{c['peak_context_percent']:>6.1f}%  {c['task'][:48]}"
        )
    calls = sum(c["tool_calls"] for c in chats)
    errors = sum(c["tool_errors"] for c in chats)
    lines += [
        "",
        f"Чатов: {len(chats)}, минут: {sum(c['minutes'] for c in chats):.0f}, вызовов инструментов: {calls}, "
        f"из них с ошибкой: {errors} ({errors * 100 / max(calls, 1):.1f}%), "
        f"прерываний владельцем: {sum(c['interruptions'] for c in chats)}",
    ]
    return "\n".join(lines)


def backfill() -> str:
    """Добавляет чаты этой папки, которые Claude Code ещё хранит: старые журналы он чистит сам."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(PROJECT_DIR))
    known = {c["chat"] for c in latest_chats()}
    added = 0
    for path in sorted((Path.home() / ".claude" / "projects" / slug).glob("*.jsonl")):
        if path.stem not in known:
            remember({"event": "turn_end", "at": datetime.now().isoformat(timespec="seconds"), **summarize(path)})
            added += 1
    return f"Добавлено чатов: {added}"


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if "--report" in sys.argv:
        print(report())
        return
    if "--backfill" in sys.argv:
        print(backfill())
        return
    try:
        answer = handle(json.loads(sys.stdin.buffer.read().decode("utf-8")))
        if answer:
            print(json.dumps(answer, ensure_ascii=False))
    except Exception as e:
        # Хук не должен останавливать работу; причина остаётся в файле для следующего чата
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        with open(WORK_DIR / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {type(e).__name__}: {e}\n")


if __name__ == "__main__":
    main()
