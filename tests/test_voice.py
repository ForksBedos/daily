"""Голосовые посты: обмен файлами с посредником, посредник, пост по расшифровке, диалог в боте.

Транскрибатор, Telegram и Claude подменены: проверяется то, что получит пользователь и что окажется
в файлах и в базе.
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram.error import TimedOut
from test_bot import ADMIN, SMM, _codes, _core
from test_posts import FakeClaude, _setup

from uzdigest import service, voice, voice_bridge
from uzdigest.bot import app
from uzdigest.bot.core import DENIED, MAX_MESSAGE, VoiceRequest
from uzdigest.config import VoiceSettings
from uzdigest.db import repo
from uzdigest.llm.client import LlmBudget
from uzdigest.posts.generate import PostError, PostLimitReached, make_voice_post
from uzdigest.posts.profiles import load_profiles

VOICE_ID = "20261106-101530-77"
TRANSCRIPT = "С 1 января 2027 года ставка для малого бизнеса составит 4 процента. Проверьте договоры."
GOOD = "**Ставка для малого бизнеса**\n\nС 1 января 2027 года ставка составит 4%.\n\nCrowe Uzbekistan"
PROFILES = load_profiles()


# --- Обмен файлами: сторона бота ---


class Clock:
    """Время идёт только при паузах ожидания; на каждой паузе срабатывает то, что задал тест."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


def _wait(folder, clock, timeout_s=600):
    return voice.wait_transcript(
        folder, VOICE_ID, timeout_s, sleep=clock.sleep, monotonic=clock.monotonic
    )  # fmt: skip


def _beat(folder):
    """Живой посредник: отметка меняется при каждой паузе."""

    def on_sleep(now):
        (folder / voice.BEAT).write_text(str(now), encoding="utf-8")

    return on_sleep


def test_id_carries_the_day_and_fits_a_button():
    voice_id = voice.new_id(datetime(2026, 11, 6, 5, 15, 30, tzinfo=UTC), 77)
    assert voice_id == VOICE_ID  # время Ташкента
    assert voice.day_of(voice_id).isoformat() == "2026-11-06"
    assert len(VoiceRequest(voice_id, "instagram", "ru", "variant").data.encode()) <= 64
    for bad in ("../../etc/passwd", "20261106", "20261106-101530-1:2"):
        with pytest.raises(ValueError):
            voice.check_id(bad)


def test_audio_appears_for_the_bridge_only_when_complete(tmp_path):
    part = voice.incoming(tmp_path, VOICE_ID)
    part.write_bytes(b"ogg")
    assert voice_bridge.pending(tmp_path) == []  # недокачанный файл посредник не берёт
    voice.publish(tmp_path, VOICE_ID)
    assert [p.name for p in voice_bridge.pending(tmp_path)] == [f"{VOICE_ID}.ogg"]


def test_transcript_arrives_while_bridge_is_alive(tmp_path):
    beat = _beat(tmp_path)

    def on_sleep(now):
        beat(now)
        if now >= 100:
            (tmp_path / f"{VOICE_ID}.txt").write_text(TRANSCRIPT, encoding="utf-8")

    assert _wait(tmp_path, Clock(on_sleep)) == TRANSCRIPT


def test_bridge_error_is_shown_as_is(tmp_path):
    (tmp_path / f"{VOICE_ID}.err").write_text("В записи не слышно речи", encoding="utf-8")
    with pytest.raises(voice.VoiceError, match="не слышно речи"):
        _wait(tmp_path, Clock())


def test_silent_bridge_is_reported_and_the_voice_is_cancelled(tmp_path):
    clock = Clock()
    with pytest.raises(voice.VoiceError, match="не запущен"):
        _wait(tmp_path, clock)
    assert clock.now <= voice.BEAT_GAP_S + voice.POLL_S
    (tmp_path / f"{VOICE_ID}.ogg").write_bytes(b"ogg")
    assert voice_bridge.pending(tmp_path) == []  # включившись позже, посредник не тратит на неё время


def test_timeout_when_bridge_lives_but_transcript_never_comes(tmp_path):
    clock = Clock(_beat(tmp_path))
    with pytest.raises(voice.VoiceError, match="слишком долго"):
        _wait(tmp_path, clock, timeout_s=60)
    assert clock.now <= 60 + voice.POLL_S


def test_corrected_text_replaces_the_transcript(tmp_path):
    (tmp_path / f"{VOICE_ID}.txt").write_text(TRANSCRIPT, encoding="utf-8")
    voice.save_edit(tmp_path, VOICE_ID, "Верный текст")
    assert voice.read_transcript(tmp_path, VOICE_ID) == "Верный текст"
    assert (tmp_path / f"{VOICE_ID}.txt").read_text(encoding="utf-8") == TRANSCRIPT  # исходный цел
    with pytest.raises(LookupError):
        voice.read_transcript(tmp_path, "20261106-101530-78")


# --- Посредник ---


class FakeEngine:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def transcribe(self, audio, out_dir):
        self.calls.append(audio.name)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{audio.stem}.txt"
        path.write_text(result, encoding="utf-8-sig")  # движок пишет файл с отметкой UTF-8
        return path


def _audio(folder, voice_id=VOICE_ID):
    path = folder / f"{voice_id}.ogg"
    path.write_bytes(b"ogg")
    return path


def test_bridge_writes_the_transcript_and_does_not_repeat(tmp_path):
    _audio(tmp_path)
    engine = FakeEngine(f"  {TRANSCRIPT}\n")
    voice_bridge.process_pending(tmp_path, engine)
    voice_bridge.process_pending(tmp_path, engine)
    assert engine.calls == [f"{VOICE_ID}.ogg"]
    assert voice.read_transcript(tmp_path, VOICE_ID) == TRANSCRIPT


@pytest.mark.parametrize(
    ("results", "message"),
    [
        (["  \n"], "не слышно речи"),
        ([voice_bridge.EngineFailed("Decoded audio is empty")], "Не удалось расшифровать"),
        ([voice_bridge.EngineDown("нет канала")], "не запустился"),
        ([OSError("обрыв"), OSError("обрыв")], "не запустился"),
    ],
)
def test_bridge_reports_failures_to_the_bot(tmp_path, results, message):
    _audio(tmp_path)
    voice_bridge.process_pending(tmp_path, FakeEngine(*results))
    assert message in (tmp_path / f"{VOICE_ID}.err").read_text(encoding="utf-8")
    with pytest.raises(voice.VoiceError, match=message):
        _wait(tmp_path, Clock())


def test_bridge_survives_engine_restart_mid_file(tmp_path):
    """Приложение при запуске убивает чужой движок: обрыв канала — один повтор."""
    _audio(tmp_path)
    voice_bridge.process_pending(tmp_path, FakeEngine(OSError("обрыв канала"), TRANSCRIPT))
    assert voice.read_transcript(tmp_path, VOICE_ID) == TRANSCRIPT


def test_one_bad_voice_does_not_stop_the_next(tmp_path):
    _audio(tmp_path, "20261106-101530-1")
    _audio(tmp_path, "20261106-101530-2")
    voice_bridge.process_pending(tmp_path, FakeEngine(voice_bridge.EngineFailed("сбой"), TRANSCRIPT))
    assert (tmp_path / "20261106-101530-1.err").exists()
    assert voice.read_transcript(tmp_path, "20261106-101530-2") == TRANSCRIPT


class FakePipe:
    """Канал движка: на каждое подключение отдаёт заготовленные строки, запросы пишет в sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Stream(self, answer)


class _Stream:
    def __init__(self, pipe, lines):
        self.pipe = pipe
        self.lines = [json.dumps(line).encode() + b"\n" for line in lines]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def write(self, data):
        self.pipe.sent.append(json.loads(data))

    def flush(self):
        pass

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


def _progress(stage, **extra):
    fields = {"FilePath": "x", "Stage": stage, "Percent": 0, "Error": None, "OutputFiles": None, **extra}
    return {"type": "partialText", "text": json.dumps(fields)}


READY = [{"type": "status", "data": {"state": "idle", "loadedModel": "LargeV3TurboQ5"}}]
LOADING = [{"type": "status", "data": {"state": "idle", "loadedModel": "none"}}]


def _engine(pipe, started=None, wait_s=60):
    started = [] if started is None else started
    clock = Clock()
    return voice_bridge.Engine(
        open_pipe=pipe, start=lambda: started.append(1), sleep=clock.sleep, monotonic=clock.monotonic,
        wait_s=wait_s,
    )  # fmt: skip


def test_engine_request_and_result(tmp_path):
    done = [_progress(2), _progress(5, OutputFiles=[str(tmp_path / "a.txt")])]
    done.append({"type": "ack", "data": {"done": 1, "failed": 0}})
    pipe = FakePipe(READY, done)
    path = _engine(pipe).transcribe(tmp_path / "a.ogg", tmp_path / "work")
    assert path == tmp_path / "a.txt"
    request = pipe.sent[1]
    assert request["command"] == "transcribe-folder"
    assert json.loads(request["parameters"]["sourcesJson"]) == [
        {"Value": str(tmp_path / "a.ogg"), "IsUrl": False}
    ]
    options = json.loads(request["parameters"]["optionsJson"])
    assert options["OutputDirectory"] == str(tmp_path / "work") and options["WriteTxt"] is True


def test_engine_failure_carries_the_reason(tmp_path):
    failed = [_progress(6, Error="Decoded audio is empty"), {"type": "ack", "data": {"done": 0, "failed": 1}}]
    with pytest.raises(voice_bridge.EngineFailed, match="Decoded audio is empty"):
        _engine(FakePipe(READY, failed)).transcribe(tmp_path / "a.ogg", tmp_path)
    with pytest.raises(voice_bridge.EngineFailed, match="busy"):
        _engine(FakePipe(READY, [{"type": "error", "error": "busy"}])).transcribe(
            tmp_path / "a.ogg", tmp_path
        )


def test_closed_engine_is_started_and_waited_for(tmp_path):
    started = []
    done = [_progress(5, OutputFiles=["a.txt"]), {"type": "ack", "data": {"done": 1, "failed": 0}}]
    pipe = FakePipe(FileNotFoundError(), LOADING, READY, done)
    _engine(pipe, started).transcribe(tmp_path / "a.ogg", tmp_path)
    assert started == [1]  # модель грузится после старта канала: запрос ушёл только после неё
    assert [r["command"] for r in pipe.sent] == ["status", "status", "transcribe-folder"]


def test_engine_that_never_gets_ready(tmp_path):
    pipe = FakePipe(*[FileNotFoundError()] * 100)
    with pytest.raises(voice_bridge.EngineDown):
        _engine(pipe).transcribe(tmp_path / "a.ogg", tmp_path)


# --- Пост по расшифровке ---


def _voice_post(conn, settings, claude, transcript=TRANSCRIPT, **kwargs):
    kwargs.setdefault("now", datetime(2026, 11, 6, 6, 0, tzinfo=UTC))
    return make_voice_post(
        conn, settings, VOICE_ID, transcript, kwargs.pop("platform", "telegram"), "ru",
        kwargs.pop("mode", "new"), profiles=PROFILES,
        budget=LlmBudget(settings.llm, ask_fn=claude), **kwargs,
    )  # fmt: skip


def test_post_from_voice_is_saved_without_a_link(tmp_path):
    conn, settings, _ = _setup(tmp_path)
    claude = FakeClaude(GOOD)
    post = _voice_post(conn, settings, claude, author="СММ")
    assert post.flags == [] and post.calls == 1 and post.text == GOOD
    prompt, system_prompt = claude.calls[0]
    assert TRANSCRIPT in prompt
    assert "голосов" in system_prompt and "{{" not in system_prompt
    row = repo.post(conn, post.id)
    assert (row["article_id"], row["digest_date"]) == (f"voice-{VOICE_ID}", "2026-11-06")
    assert row["prompt_version"] == "post_voice_v1" and row["author"] == "СММ"
    assert GOOD in (
        settings.outputs_dir / "2026-11-06" / "posts" / f"voice-{VOICE_ID}-telegram-ru-1.md"
    ).read_text(encoding="utf-8")


def test_invented_numbers_and_links_are_caught_and_retried_once(tmp_path):
    conn, settings, _ = _setup(tmp_path)
    bad = GOOD.replace("4%", "9%") + "\nПодробнее: https://example.com/a"
    claude = FakeClaude(bad, bad)
    post = _voice_post(conn, settings, claude)
    assert len(claude.calls) == 2
    assert any("нет в расшифровке" in flag and "9" in flag for flag in post.flags)
    assert any("ссылка не из расшифровки" in flag for flag in post.flags)


def test_same_text_comes_from_cache_corrected_text_is_written_anew(tmp_path):
    conn, settings, _ = _setup(tmp_path)
    _voice_post(conn, settings, FakeClaude(GOOD))
    assert _voice_post(conn, settings, FakeClaude()).cached
    changed = TRANSCRIPT.replace("4 процента", "4% ставки")
    assert not _voice_post(conn, settings, FakeClaude(GOOD), transcript=changed).cached


def test_shorter_rewrites_the_previous_post(tmp_path):
    conn, settings, _ = _setup(tmp_path)
    _voice_post(conn, settings, FakeClaude(GOOD))
    claude = FakeClaude(GOOD)
    _voice_post(conn, settings, claude, mode="shorter")
    assert GOOD in claude.calls[0][0]


def test_voice_posts_count_towards_the_daily_limit(tmp_path):
    conn, settings, _ = _setup(tmp_path, max_per_day=1)
    _voice_post(conn, settings, FakeClaude(GOOD))
    with pytest.raises(PostLimitReached):
        _voice_post(conn, settings, FakeClaude(GOOD), mode="variant")


# --- Бот ---


def _voice_core(tmp_path, transcript=TRANSCRIPT):
    core, settings = _core(tmp_path)
    settings.voice = VoiceSettings(dir=tmp_path / "voice", max_seconds=300)
    settings.voice.dir.mkdir()
    if transcript is not None:
        (settings.voice.dir / f"{VOICE_ID}.txt").write_text(transcript, encoding="utf-8")
    return core, settings


def _post(text=GOOD, flags=()):
    return SimpleNamespace(
        id=5, platform="telegram", lang="ru", text=text, chars=len(text), max_chars=4096,
        cached=False, flags=list(flags),
    )  # fmt: skip


def test_too_long_voice_is_refused_before_download(tmp_path):
    core, _ = _voice_core(tmp_path)
    assert core.voice_refusal(300) is None
    assert "5 мин" in core.voice_refusal(301).text


def test_waiting_text_names_the_time(tmp_path):
    core, _ = _voice_core(tmp_path)
    assert "около 1 мин" in core.voice_wait(18).text
    assert "около 13 мин" in core.voice_wait(300).text


def test_transcript_is_shown_with_platform_and_edit_buttons(tmp_path):
    core, _ = _voice_core(tmp_path)
    replies = core.voice_transcript(VOICE_ID)
    assert TRANSCRIPT in replies[0].text
    codes = _codes(replies[-1])
    for code, profile in core.profiles.platforms.items():
        assert VoiceRequest(VOICE_ID, code, profile.default_lang).data in codes
    assert f"ve:{VOICE_ID}" in codes


def test_long_transcript_is_split_and_nothing_is_lost(tmp_path):
    long = " ".join(f"слово{n}" for n in range(1500))
    core, _ = _voice_core(tmp_path, long)
    replies = core.voice_transcript(VOICE_ID)
    assert all(len(r.text) <= MAX_MESSAGE for r in replies)
    assert all(f"слово{n}" in " ".join(r.text for r in replies) for n in (0, 750, 1499))


def test_failed_transcription_explains_and_offers_menu(tmp_path, monkeypatch):
    core, _ = _voice_core(tmp_path, None)

    def fail(*args, **kwargs):
        raise voice.VoiceError("Транскрибатор на ноутбуке не запущен")

    monkeypatch.setattr(service, "wait_voice", fail)
    [reply] = core.voice_wait_result(VOICE_ID, 10)
    assert "не запущен" in reply.text and _codes(reply) == ["menu"]


def test_correcting_the_transcript_then_writing(tmp_path, monkeypatch):
    core, settings = _voice_core(tmp_path)
    user = core.user(SMM)
    [ask] = core.button(user, SMM, f"ve:{VOICE_ID}")
    assert "исправленный текст" in ask.text
    [saved] = core.text(user, "Верный текст про 4 процента")
    assert "обновлён" in saved.text.lower()
    assert VoiceRequest(VOICE_ID, "telegram", "ru").data in _codes(saved)
    assert voice.read_transcript(settings.voice.dir, VOICE_ID) == "Верный текст про 4 процента"
    assert core.text(user, "просто текст")[0].text == core.menu(user).text  # ожидание закрыто


def test_pressing_a_button_cancels_the_correction(tmp_path):
    core, settings = _voice_core(tmp_path)
    user = core.user(SMM)
    core.button(user, SMM, f"ve:{VOICE_ID}")
    core.button(user, SMM, "menu")
    core.text(user, "случайное сообщение")
    assert voice.read_transcript(settings.voice.dir, VOICE_ID) == TRANSCRIPT


def test_empty_correction_keeps_the_text(tmp_path):
    core, settings = _voice_core(tmp_path)
    user = core.user(SMM)
    core.button(user, SMM, f"ve:{VOICE_ID}")
    assert "Пустой" in core.text(user, "  ")[0].text
    assert voice.read_transcript(settings.voice.dir, VOICE_ID) == TRANSCRIPT


def test_writing_a_post_from_voice(tmp_path, monkeypatch):
    core, _ = _voice_core(tmp_path)
    user = core.user(SMM)
    calls = []

    def fake(voice_id, platform, lang, mode, **kwargs):
        calls.append((voice_id, platform, lang, mode, kwargs["author"]))
        return _post(flags=["нет в расшифровке: 9%"])

    monkeypatch.setattr(service, "voice_post", fake)
    request = VoiceRequest(VOICE_ID, "telegram", "ru")
    [writing] = core.button(user, SMM, request.data)
    assert writing.generate == request and "Пишу пост" in writing.text
    replies = core.generate(user, request)
    assert calls == [(VOICE_ID, "telegram", "ru", "new", "СММ")]
    info, last = replies[0].text, replies[-1]
    assert "голосовое" in info and "эксперт" in info and "нет в расшифровке: 9%" in info
    assert last.text == GOOD
    codes = _codes(last)
    assert VoiceRequest(VOICE_ID, "telegram", "ru", "shorter").data in codes
    assert VoiceRequest(VOICE_ID, "telegram", "uz").data in codes
    assert f"vo:{VOICE_ID}" in codes and f"vf:{VOICE_ID}:telegram:ru:5" in codes
    assert not any(code.startswith(("t:", "o:")) for code in codes)  # кнопок дайджеста здесь нет


def test_post_failures_from_voice(tmp_path, monkeypatch):
    core, _ = _voice_core(tmp_path)
    user = core.user(SMM)
    request = VoiceRequest(VOICE_ID, "telegram", "ru")

    def raises(error):
        def fake(*args, **kwargs):
            raise error

        return fake

    monkeypatch.setattr(service, "voice_post", raises(PostError("Claude не ответил")))
    [failed] = core.generate(user, request)
    assert "Claude не ответил" in failed.text and request.data in _codes(failed)
    monkeypatch.setattr(service, "voice_post", raises(PostLimitReached("Сегодня сделано уже 40 постов")))
    assert "завтра" in core.generate(user, request)[0].text
    monkeypatch.setattr(service, "voice_post", raises(LookupError("Расшифровка не найдена")))
    [lost] = core.generate(user, request)
    assert "не найдена" in lost.text and _codes(lost) == ["menu"]
    assert user.telegram_id not in core.writing


def test_other_platform_and_feedback_for_a_voice_post(tmp_path):
    core, settings = _voice_core(tmp_path)
    user = core.user(SMM)
    [platforms] = core.button(user, SMM, f"vo:{VOICE_ID}")
    assert VoiceRequest(VOICE_ID, "linkedin", "en").data in _codes(platforms)
    core.button(user, SMM, f"vf:{VOICE_ID}:telegram:ru:5")
    [thanks] = core.text(user, "Слишком сухо")
    assert "записано" in thanks.text and f"vt:{VOICE_ID}" in _codes(thanks)
    [note] = service.list_feedback(settings=settings)
    assert note.article_id == f"voice-{VOICE_ID}" and "Слишком сухо" in note.text


def test_back_to_the_transcript(tmp_path):
    core, _ = _voice_core(tmp_path)
    replies = core.button(core.user(SMM), SMM, f"vt:{VOICE_ID}")
    assert TRANSCRIPT in replies[0].text and f"ve:{VOICE_ID}" in _codes(replies[-1])


def test_stale_voice_button_leads_to_the_menu(tmp_path):
    core, _ = _voice_core(tmp_path)
    user = core.user(SMM)
    assert core.button(user, SMM, "vg:../x:telegram:ru:new")[0].text == core.menu(user).text


def test_service_reads_and_posts_through_settings(tmp_path):
    conn, settings, _ = _setup(tmp_path)
    settings.voice = VoiceSettings(dir=tmp_path / "voice")
    with pytest.raises(LookupError):
        service.voice_post(VOICE_ID, "telegram", "ru", settings=settings)
    settings.voice.dir.mkdir()
    (settings.voice.dir / f"{VOICE_ID}.txt").write_text(TRANSCRIPT, encoding="utf-8")
    post = service.voice_post(
        VOICE_ID, "telegram", "ru", settings=settings,
        budget=LlmBudget(settings.llm, ask_fn=FakeClaude(GOOD)),
    )  # fmt: skip
    assert post.article_id == f"voice-{VOICE_ID}"


class FakeTelegramVoice:
    """Как настоящий Telegram: файл готовится дольше 5 секунд, короткое ожидание кончается таймаутом."""

    def __init__(self, duration, content=b"ogg", prepare_s=10, failures=0):
        self.duration = duration
        self.content = content
        self.prepare_s = prepare_s
        self.failures = failures
        self.requests = 0
        self.downloads = 0

    async def get_file(self, *, read_timeout=5):
        self.requests += 1
        if read_timeout < self.prepare_s:
            raise TimedOut
        if self.failures:
            self.failures -= 1
            raise TimedOut
        return self

    async def download_to_drive(self, path, **_):
        self.downloads += 1
        path.write_bytes(self.content)


def _voice_update(user_id, tg_voice):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=user_id),
        message=SimpleNamespace(message_id=77, voice=tg_voice),
    )  # fmt: skip


def _capture(monkeypatch):
    sent = []

    async def send(_app, chat_id, reply):
        sent.append((chat_id, reply.text))

    monkeypatch.setattr(app, "_send", send)
    return sent


def test_stranger_voice_is_not_even_downloaded(tmp_path, monkeypatch):
    core, _ = _voice_core(tmp_path)
    sent = _capture(monkeypatch)
    tg_voice = FakeTelegramVoice(10)
    context = SimpleNamespace(bot_data={"core": core}, application=None)
    asyncio.run(app._on_voice(_voice_update(123, tg_voice), context))
    assert sent == [(123, DENIED)] and tg_voice.downloads == 0


def test_long_voice_is_not_downloaded(tmp_path, monkeypatch):
    core, _ = _voice_core(tmp_path)
    sent = _capture(monkeypatch)
    tg_voice = FakeTelegramVoice(301)
    context = SimpleNamespace(bot_data={"core": core}, application=None)
    asyncio.run(app._on_voice(_voice_update(ADMIN, tg_voice), context))
    assert len(sent) == 1 and "5 мин" in sent[0][1] and tg_voice.downloads == 0


def test_telegram_hiccup_is_retried_once(tmp_path, monkeypatch):
    core, settings = _voice_core(tmp_path, None)
    _capture(monkeypatch)
    monkeypatch.setattr(
        service, "wait_voice", lambda *a, **k: (_ for _ in ()).throw(voice.VoiceError("стоп"))
    )
    tg_voice = FakeTelegramVoice(18, failures=1)
    context = SimpleNamespace(bot_data={"core": core}, application=None)
    asyncio.run(app._on_voice(_voice_update(ADMIN, tg_voice), context))
    assert tg_voice.requests == 2 and tg_voice.downloads == 1


def test_voice_telegram_would_not_give_is_explained(tmp_path, monkeypatch):
    core, settings = _voice_core(tmp_path, None)
    sent = _capture(monkeypatch)
    tg_voice = FakeTelegramVoice(18, failures=5)
    context = SimpleNamespace(bot_data={"core": core}, application=None)
    asyncio.run(app._on_voice(_voice_update(ADMIN, tg_voice), context))
    assert "Расшифровываю" in sent[0][1]
    assert "скачать" in sent[-1][1] and len(sent) == 2
    assert voice_bridge.pending(settings.voice.dir) == []


def test_voice_goes_to_the_bridge_and_the_transcript_comes_back(tmp_path, monkeypatch):
    core, settings = _voice_core(tmp_path, None)
    sent = _capture(monkeypatch)

    def bridge(voice_id, duration, *, settings):
        [audio] = voice_bridge.pending(settings.voice.dir)
        assert audio.read_bytes() == b"ogg" and audio.stem == voice_id and duration == 18
        (settings.voice.dir / f"{voice_id}.txt").write_text(TRANSCRIPT, encoding="utf-8")
        return TRANSCRIPT

    monkeypatch.setattr(service, "wait_voice", bridge)
    context = SimpleNamespace(bot_data={"core": core}, application=None)
    asyncio.run(app._on_voice(_voice_update(ADMIN, FakeTelegramVoice(18)), context))
    assert "Расшифровываю" in sent[0][1]
    assert any(TRANSCRIPT in text for _, text in sent[1:])
