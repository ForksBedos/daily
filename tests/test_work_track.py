"""Учёт чатов с ИИ: напоминание при 65%, числа чата, возвращение к работе в новом чате."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".claude" / "hooks" / "work_track.py"
spec = importlib.util.spec_from_file_location("work_track", SCRIPT)
work_track = importlib.util.module_from_spec(spec)
spec.loader.exec_module(work_track)


@pytest.fixture
def work_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(work_track, "WORK_DIR", tmp_path / "ai_work")
    monkeypatch.setattr(work_track, "CHATS_FILE", tmp_path / "ai_work" / "chats.jsonl")
    monkeypatch.delenv("CONTEXT_WINDOW_TOKENS", raising=False)
    return tmp_path


def reply(tokens, sidechain=False, tools=0):
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 8,
        "cache_read_input_tokens": tokens - 10,
        "output_tokens": 500,
    }
    blocks = [{"type": "tool_use", "name": "Bash"}] * tools
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": "2026-11-04T10:10:00Z",
        "message": {"model": "claude-opus-5-5", "usage": usage, "content": blocks},
    }


def owner(text):
    return {
        "type": "user",
        "timestamp": "2026-11-04T10:00:00Z",
        "cwd": "C:/proj/daily",
        "message": {"role": "user", "content": text},
    }


def tool_result(error):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": error}]}}


def transcript(tmp_path, *lines):
    path = tmp_path / "chat-1.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def asked(path, event="PostToolUse"):
    return work_track.handle({"hook_event_name": event, "transcript_path": str(path), "session_id": "chat-1"})


def test_context_is_what_the_last_main_reply_read_not_a_helper_agent(work_dir):
    path = transcript(work_dir, reply(1000), reply(900_000, sidechain=True))
    assert work_track.context_tokens(work_track.records(path)) == 1000


def test_no_reminder_below_the_threshold(work_dir):
    assert asked(transcript(work_dir, reply(649_000))) is None


def test_reminder_at_65_percent_reaches_claude_and_the_owner(work_dir):
    answer = asked(transcript(work_dir, reply(650_000)))
    context = answer["hookSpecificOutput"]["additionalContext"]
    assert answer["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "65%" in context and "docs/PROGRESS.md" in context and "новый чат" in context
    assert "65%" in answer["systemMessage"]


def test_reminder_is_not_repeated_on_every_tool_call(work_dir):
    path = transcript(work_dir, reply(650_000))
    assert asked(path)
    assert asked(path) is None
    assert asked(transcript(work_dir, reply(690_000))) is None


def test_reminder_comes_back_ten_percent_later(work_dir):
    assert asked(transcript(work_dir, reply(650_000)))
    assert "75%" in asked(transcript(work_dir, reply(750_000)))["systemMessage"]


def test_each_chat_gets_its_own_reminder(work_dir):
    path = transcript(work_dir, reply(650_000))
    assert asked(path)
    assert work_track.handle(
        {"hook_event_name": "PostToolUse", "transcript_path": str(path), "session_id": "chat-2"}
    )


def test_smaller_window_is_taken_from_the_setting(work_dir, monkeypatch):
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "200000")
    assert "65%" in asked(transcript(work_dir, reply(130_000)), "UserPromptSubmit")["systemMessage"]


def test_unfinished_last_line_does_not_break_the_check(work_dir):
    path = transcript(work_dir, reply(650_000))
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type": "assist')
    assert asked(path)


def test_new_chat_is_told_where_the_work_stopped(work_dir, monkeypatch):
    progress = work_dir / "PROGRESS.md"
    progress.write_text(
        "# Состояние\n\n## Где остановились\n\nШаг 2 готов.\n\n## Договорённости\n\nГит не трогать.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(work_track, "PROGRESS_FILE", progress)

    context = work_track.handle({"hook_event_name": "SessionStart"})["hookSpecificOutput"][
        "additionalContext"
    ]

    assert "Шаг 2 готов" in context
    assert "Гит не трогать" not in context  # только первый раздел: остальное в файле


def test_progress_file_of_this_project_starts_with_where_we_stopped():
    assert work_track.where_we_stopped().startswith("## Где остановились")


def test_end_of_a_turn_leaves_the_numbers_of_the_chat(work_dir):
    path = transcript(
        work_dir,
        owner("сделай журнал"),
        reply(1000, tools=2),
        tool_result(True),
        tool_result(False),
        owner("[Request interrupted by user]"),
        owner("продолжай"),
        reply(5000, tools=1),
    )

    assert asked(path, "Stop") is None

    chat = work_track.latest_chats()[0]
    assert (chat["chat"], chat["task"], chat["model"]) == ("chat-1", "сделай журнал", "claude-opus-5-5")
    assert (chat["owner_messages"], chat["interruptions"]) == (2, 1)
    assert (chat["tool_calls"], chat["tool_errors"]) == (3, 1)
    assert chat["peak_context_tokens"] == 5000
    assert chat["minutes"] == 10.0


def test_report_shows_the_last_picture_of_each_chat(work_dir):
    path = transcript(work_dir, owner("первая задача"), reply(1000, tools=1))
    asked(path, "Stop")
    path = transcript(work_dir, owner("первая задача"), reply(1000, tools=3))
    asked(path, "Stop")

    text = work_track.report()

    assert "первая задача" in text
    assert "Чатов: 1" in text and "вызовов инструментов: 3" in text


def test_empty_report_says_what_to_do(work_dir):
    assert "--backfill" in work_track.report()


def test_hook_file_gives_a_new_chat_the_first_section_of_progress():
    """Весь путь, как у Claude Code: событие на входе скрипта, JSON с разделом на выходе."""
    answer = subprocess.run(
        ["python", str(SCRIPT)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}).encode(),
        capture_output=True,
        check=True,
    )
    context = json.loads(answer.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    assert "Где остановились" in context
