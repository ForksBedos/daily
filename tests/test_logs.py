"""Журналы: ни одна запись не теряется при занятом файле, бот пишет отдельно, секреты маскируются."""

import json
import logging
import os

import pytest

from uzdigest import cli
from uzdigest.db import repo
from uzdigest.ops import LOG_FORMAT, SharedLogHandler, log_file, setup_logging
from uzdigest.redact import MASK, MaskingFormatter, mask

TOKEN = "1234567890:AAExampleExampleExampleExampleExample1"  # вид токена Telegram, значение выдуманное
OAUTH = "sk-ant-oat01-ExampleExampleExampleExample"


def _logger(name, handler):
    logger = logging.getLogger(name)
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    return logger


def _lines(folder):
    return [line for path in folder.glob("*.log*") for line in path.read_text(encoding="utf-8").splitlines()]


# --- Ротация на занятом файле ---


@pytest.mark.skipif(os.name != "nt", reason="только Windows не даёт переименовать занятый файл")
def test_no_record_is_lost_while_another_process_holds_the_log(tmp_path):
    """Как бот с открытым logs/uzdigest.log: прежний обработчик терял почти всё после 1 МБ."""
    path = tmp_path / "uzdigest.log"
    path.write_text("", encoding="utf-8")
    handler = SharedLogHandler(path, maxBytes=300, backupCount=2, encoding="utf-8")
    logger = _logger("held", handler)

    with path.open("a", encoding="utf-8"):
        for n in range(30):
            logger.info("запись %02d %s", n, "x" * 40)
    handler.close()

    assert len(_lines(tmp_path)) == 30


def test_log_rotates_as_soon_as_the_file_is_free(tmp_path):
    path = tmp_path / "uzdigest.log"
    handler = SharedLogHandler(path, maxBytes=300, backupCount=2, encoding="utf-8")
    logger = _logger("free", handler)

    for n in range(30):
        logger.info("запись %02d %s", n, "x" * 40)
    handler.close()

    assert sorted(p.name for p in tmp_path.iterdir()) == ["uzdigest.log", "uzdigest.log.1", "uzdigest.log.2"]
    assert path.stat().st_size <= 300


@pytest.mark.parametrize(("command", "expected"), [("bot", "bot.log"), ("run", "uzdigest.log")])
def test_bot_keeps_its_own_log_so_the_pipeline_log_can_rotate(command, expected):
    assert log_file(command) == expected


def test_cli_opens_the_log_of_the_command(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "setup_logging", lambda folder, name: opened.append(name))
    monkeypatch.setattr(cli, "load_settings", lambda: type("S", (), {"logs_dir": tmp_path})())
    monkeypatch.setattr(cli, "_bot", lambda settings: 0)

    assert cli.main(["bot"]) == 0
    assert opened == ["bot.log"]


# --- Маскировка ---


@pytest.mark.parametrize(
    "text",
    [
        f"POST https://api.telegram.org/bot{TOKEN}/sendMessage",
        f"токен {TOKEN} не принят",
        f"OAuth {OAUTH}",
    ],
)
def test_secret_shapes_are_masked_even_without_the_setting(text, monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    masked = mask(text)
    assert TOKEN not in masked and OAUTH not in masked and MASK in masked


def test_value_from_the_environment_is_masked_whatever_it_looks_like(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "простая-строка-без-формы")
    assert mask("ключ простая-строка-без-формы в ошибке") == f"ключ {MASK} в ошибке"


def test_short_setting_does_not_eat_the_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1")
    assert mask("в 10:15 пришло 11 материалов") == "в 10:15 пришло 11 материалов"


def test_ordinary_text_stays_as_it_is():
    text = "Claude: 12345 мс, usage={'input_tokens': 30}, УП-236 от 2026-11-04T04:30:13+00:00"
    assert mask(text) == text


def test_log_file_has_no_secret_in_the_message_or_the_traceback(tmp_path):
    handler = SharedLogHandler(tmp_path / "uzdigest.log", maxBytes=10_000, encoding="utf-8")
    handler.setFormatter(MaskingFormatter(LOG_FORMAT))
    logger = _logger("secret", handler)

    logger.warning("адрес https://api.telegram.org/bot%s/getMe", TOKEN)
    try:
        raise RuntimeError(f"Telegram ответил на /bot{TOKEN}/sendMessage")
    except RuntimeError:
        logger.exception("сбой")
    handler.close()

    text = "\n".join(_lines(tmp_path))
    assert TOKEN not in text
    assert text.count(MASK) == 2


def test_setup_logging_masks_console_and_file(tmp_path, capfd):
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    root.handlers = []
    try:
        setup_logging(tmp_path, "uzdigest.log")
        logging.getLogger("uzdigest.test").warning("токен %s", TOKEN)
        for handler in root.handlers:
            handler.close()
    finally:
        root.handlers, root.level = saved, level

    assert TOKEN not in capfd.readouterr().err
    assert TOKEN not in (tmp_path / "uzdigest.log").read_text(encoding="utf-8")


def test_stats_and_source_errors_in_the_database_are_masked():
    conn = repo.connect(":memory:")
    run_id = repo.start_run(conn, "digest")
    repo.finish_run(conn, run_id, "failed", {"error": f"HTTPError: /bot{TOKEN}/getUpdates", "note": OAUTH})
    conn.execute("INSERT INTO sources (code, name, kind, url, tier) VALUES ('s', 's', 'rss', 'u', 'A')")
    source_id = conn.execute("SELECT id FROM sources").fetchone()[0]

    repo.mark_source_error(conn, source_id, f"Ошибка: /bot{TOKEN}/x")

    stored = conn.execute("SELECT stats_json FROM runs").fetchone()[0]
    assert TOKEN not in stored and OAUTH not in stored
    assert json.loads(stored)["error"] == f"HTTPError: /bot{MASK}/getUpdates"
    assert TOKEN not in conn.execute("SELECT last_error FROM sources").fetchone()[0]
