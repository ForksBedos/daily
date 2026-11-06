"""Слой управления (docs/plan/stage-8-automation.md): всё, что делают люди и Планировщик.

Функции возвращают данные (pydantic-модели), а не печатают. CLI вызывает их сейчас, Telegram-бот
(этап 10) будет вызывать их же. describe_* превращают результаты в понятный текст на русском.
Сбор и сборка дайджеста идут под общей блокировкой data/run.lock: второй запуск получает «уже идёт».
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from uzdigest import voice
from uzdigest.collect import FETCH_ERRORS, collect, collect_rates
from uzdigest.config import Settings, Source, load_settings, load_sources
from uzdigest.db import repo
from uzdigest.enrich import enrich
from uzdigest.llm.client import LlmBudget
from uzdigest.metrics import Metrics, collect_metrics, render_brief
from uzdigest.ops import Busy, backup_db, notify, read_lock, run_lock
from uzdigest.pipeline import build_digest
from uzdigest.posts.generate import (
    Mode,
    Post,
    Topic,
    list_topics,
    load_digest,
    make_post,
    make_voice_post,
)
from uzdigest.redact import mask
from uzdigest.render.markdown import load_labels
from uzdigest.render.model import LANGS, Digest
from uzdigest.timeutil import TASHKENT, from_iso, tashkent_date, tashkent_to_utc, utc_now
from uzdigest.web.http import HttpClient

log = logging.getLogger(__name__)

RunKind = Literal["collect", "digest", "run"]
VOICE_WAIT_BASE_S = 120  # запуск движка и загрузка модели, если транскрибатор был закрыт
FEEDBACK_KINDS = {
    "missed": "пропущено",
    "duplicate": "дубль",
    "extra": "лишнее",
    "error": "ошибка",
    "translation": "перевод",
    "post": "пост",
    "other": "прочее",
}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
COLLECT_RESULTS = {"ok": "без ошибок", "failed": "ни один источник не ответил"}


# --- Модели результатов ---


class SourceRun(BaseModel):
    code: str
    status: str  # ok | not_modified | error
    fetched: int
    new: int
    backfilled: int
    error: str | None
    warnings: int
    gap_h: float | None = None


class CollectSummary(BaseModel):
    status: Literal["ok", "partial"]  # partial — какой-то источник или курсы ЦБ с ошибкой
    sources: list[SourceRun]
    texts_fetched: int = 0
    texts_failed: int = 0
    texts_failed_by_source: dict[str, int] = {}
    texts_skipped: int = 0
    rates_date: str | None = None
    rates_error: str | None = None


class DigestSummary(BaseModel):
    date: date
    dir: str
    status: str  # ok | partial | exists
    articles: int
    legal: int
    updates: int
    by_category: dict[str, int]
    headlines: list[tuple[str, str, str]]  # id, раздел, заголовок
    no_llm: int
    no_translation: int
    qa_flagged: int
    failed_sources: list[tuple[str, str]]  # код, ошибка или «нет свежего опроса»
    window_from: datetime
    window_to: datetime
    trimmed: bool = False
    stats: dict[str, Any] = {}  # runs.stats_json сборки; пусто для уже собранного дайджеста

    def files(self) -> dict[str, Path]:
        folder = Path(self.dir)
        return {**{lang: folder / f"digest.{lang}.md" for lang in LANGS}, "json": folder / "digest.json"}


class DigestRef(BaseModel):
    """Строка списка дайджестов: «Другие дайджесты» в боте."""

    date: date
    articles: int


class RunResult(BaseModel):
    kind: RunKind
    status: Literal["ok", "partial", "failed", "busy", "exists"]
    started_at: datetime
    finished_at: datetime
    collect: CollectSummary | None = None
    digest: DigestSummary | None = None
    backup: str | None = None
    error: str | None = None  # status = failed
    busy_kind: str | None = None  # status = busy: что уже идёт и с какого времени
    busy_since: datetime | None = None


class RunInfo(BaseModel):
    finished_at: datetime
    status: str
    detail: dict[str, Any]


class Status(BaseModel):
    now: datetime
    running: tuple[str, datetime] | None  # что идёт сейчас и с какого времени
    last_collect: RunInfo | None
    last_digest: DigestSummary | None
    failed_build: RunInfo | None  # последняя сборка упала и новой ещё не было
    failing_sources: list[tuple[str, str]]  # код, причина
    claude_token: bool
    claude_limit: dict[str, Any] | None  # последнее состояние лимита подписки из сборки
    claude_stopped: str | None  # почему Claude остановился в последней сборке
    claude_failed: int  # неудачных вызовов в последней сборке
    claude_used: bool  # сборки с Claude уже были
    posts_today: int
    posts_limit: int
    next_digest: datetime
    next_collect: datetime


class Feedback(BaseModel):
    id: int
    created_at: datetime
    digest_date: date | None
    article_id: str | None
    kind: str
    text: str
    author: str | None
    source: str


# --- Сбор и сборка ---


def run(
    kind: RunKind = "run",
    *,
    force: bool = False,
    use_llm: bool = True,
    only: set[str] | None = None,
    backfill: bool = True,
    enrich_text: bool = True,
    gap_from: datetime | None = None,
    settings: Settings | None = None,
    sources: list[Source] | None = None,
    http_factory: Callable[[], Any] | None = None,
    budget: LlmBudget | None = None,
    now: datetime | None = None,
) -> RunResult:
    """Сбор, сборка дайджеста или оба (kind=run) под блокировкой. Ошибка не бросается, а возвращается."""
    settings = settings or load_settings()
    sources = sources or load_sources()
    started = now or utc_now()
    unknown = (only or set()) - {s.code for s in sources}
    if unknown:
        raise ValueError(f"Неизвестные источники: {', '.join(sorted(unknown))}")
    result = RunResult(kind=kind, status="ok", started_at=started, finished_at=started)
    try:
        with run_lock(_lock_path(settings), kind, started):
            conn = repo.connect(settings.db_path)
            with (http_factory or (lambda: HttpClient(settings.http)))() as http:
                if kind in ("collect", "run"):
                    result.collect = _collect(
                        conn, settings, http, sources, only, backfill, enrich_text, gap_from
                    )
                if kind in ("digest", "run"):
                    result.backup = str(
                        backup_db(conn, settings.db_path.parent / "backup", tashkent_date(started),
                                  settings.backup.keep_last)
                    )  # fmt: skip
                    built = build_digest(
                        conn, settings, http, sources, now, force=force, use_llm=use_llm, budget=budget
                    )
                    if built.status == "exists":
                        result.digest = latest_digest(built.date, settings=settings, conn=conn)
                    else:
                        result.digest = _digest_summary(built.digest, str(built.out_dir), built.status)
                        result.digest.trimmed, result.digest.stats = built.window.trimmed, built.stats
    except Busy as busy:
        result.status, result.busy_kind, result.busy_since = "busy", busy.info.kind, busy.info.started_at
    except Exception as exc:
        log.exception("Запуск %s не удался", kind)
        result.status, result.error = "failed", mask(f"{type(exc).__name__}: {exc}")
    else:
        partial = (result.collect and result.collect.status == "partial") or (
            result.digest and result.digest.status == "partial"
        )
        if result.digest and result.digest.status == "exists":
            result.status = "exists"
        elif partial:
            result.status = "partial"
    result.finished_at = utc_now()
    notify(settings.notify, result)
    return result


def get_or_build_digest(force: bool = False, **kwargs: Any) -> RunResult:
    """Дайджест за сегодня: уже собран — сразу он; нет или force — сбор и сборка."""
    settings = kwargs.get("settings") or load_settings()
    now = kwargs.get("now") or utc_now()
    if not force:
        existing = latest_digest(tashkent_date(now), settings=settings)
        if existing is not None:
            return RunResult(kind="run", status="exists", started_at=now, finished_at=now, digest=existing)
    return run("run", force=force, **{**kwargs, "settings": settings})


def _collect(
    conn: sqlite3.Connection,
    settings: Settings,
    http: Any,
    sources: list[Source],
    only: set[str] | None,
    backfill: bool,
    enrich_text: bool,
    gap_from: datetime | None,
) -> CollectSummary:
    results = collect(
        conn,
        http,
        sources,
        lookback_h=settings.collect.lookback_h,
        backfill_settings=settings.backfill if backfill else None,
        max_workers=settings.http.max_workers,
        only=only,
        gap_from=gap_from,
    )
    run_id = repo.last_run(conn, "collect")["id"]
    summary = CollectSummary(
        status="partial" if any(r.status == "error" for r in results) else "ok",
        sources=[SourceRun(**asdict(r)) for r in results],
    )
    if enrich_text:
        stats = enrich(
            conn,
            http,
            sources,
            settings.enrich,
            lookback_h=settings.collect.lookback_h,
            max_workers=settings.http.max_workers,
            only=only,
        )
        summary.texts_fetched, summary.texts_failed = stats.fetched, stats.failed
        summary.texts_failed_by_source, summary.texts_skipped = dict(stats.failed_by_source), stats.skipped
    if settings.rates and only is None:
        try:
            summary.rates_date = collect_rates(conn, http, settings.rates)
        except FETCH_ERRORS as exc:
            summary.rates_error = f"{type(exc).__name__}: {exc}"
            summary.status = "partial"
    repo.merge_run_stats(
        conn,
        run_id,
        {
            "texts": {
                "fetched": summary.texts_fetched,
                "failed": summary.texts_failed,
                "failed_by_source": summary.texts_failed_by_source,
                "skipped": summary.texts_skipped,
            },
            "rates": {"date": summary.rates_date, "error": summary.rates_error},
        },
    )
    return summary


def _digest_summary(digest: Digest, out_dir: str, status: str) -> DigestSummary:
    articles = digest.articles
    return DigestSummary(
        date=digest.date,
        dir=out_dir,
        status=status,
        articles=len(articles),
        legal=sum(a.doc is not None for a in articles),
        updates=len(digest.updates),
        by_category=dict(Counter(a.category for a in articles)),
        headlines=[(a.id, a.category, a.texts["ru"].headline) for a in articles],
        no_llm=digest.failures.no_llm,
        no_translation=digest.failures.no_translation,
        qa_flagged=sum(bool(a.flags) for a in articles),
        failed_sources=[(s.code, s.error or "нет свежего опроса") for s in digest.failures.sources],
        window_from=digest.window.since,
        window_to=digest.window.until,
    )


# --- Дайджест, темы, посты ---


def latest_digest(
    day: date | None = None, *, settings: Settings | None = None, conn: sqlite3.Connection | None = None
) -> DigestSummary | None:
    """Дайджест за день (по умолчанию — последний собранный): файлы и сводка. None — его нет."""
    settings = settings or load_settings()
    conn = conn or repo.connect(settings.db_path)
    try:
        digest = load_digest(conn, day)
    except LookupError:
        return None
    return _digest_summary(digest, str(settings.outputs_dir / digest.date.isoformat()), "exists")


def digests(*, settings: Settings | None = None) -> list[DigestRef]:
    """Все собранные дайджесты, новые первыми."""
    settings = settings or load_settings()
    return [DigestRef(**dict(row)) for row in repo.digest_list(repo.connect(settings.db_path))]


def digest(day: date | None = None, *, settings: Settings | None = None) -> Digest:
    """Модель дайджеста за день (по умолчанию — последний): статьи целиком. Нет — LookupError."""
    settings = settings or load_settings()
    return load_digest(repo.connect(settings.db_path), day)


def topics(day: date | None = None, *, settings: Settings | None = None) -> tuple[date, list[Topic]]:
    settings = settings or load_settings()
    digest = load_digest(repo.connect(settings.db_path), day)
    return digest.date, list_topics(digest)


def post(
    article: str,
    platform: str,
    lang: str | None = None,
    mode: Mode = "new",
    *,
    day: date | None = None,
    author: str | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Post:
    settings = settings or load_settings()
    conn = repo.connect(settings.db_path)
    return make_post(
        conn, settings, load_digest(conn, day), article, platform, lang, mode, author=author, **kwargs
    )


# --- Голосовые ---


def voice_incoming(voice_id: str, *, settings: Settings | None = None) -> Path:
    """Куда скачать запись. Скачанную отдаёт посреднику voice_publish."""
    return voice.incoming((settings or load_settings()).voice.dir, voice_id)


def voice_publish(voice_id: str, *, settings: Settings | None = None) -> None:
    voice.publish((settings or load_settings()).voice.dir, voice_id)


def wait_voice(voice_id: str, duration_s: int, *, settings: Settings | None = None) -> str:
    """Ждёт расшифровку. Запас шестикратный: движок вдвое медленнее записи, а в очереди бывает другая."""
    settings = settings or load_settings()
    return voice.wait_transcript(settings.voice.dir, voice_id, VOICE_WAIT_BASE_S + 6 * duration_s)


def voice_text(voice_id: str, *, settings: Settings | None = None) -> str:
    return voice.read_transcript((settings or load_settings()).voice.dir, voice_id)


def edit_voice(voice_id: str, text: str, *, settings: Settings | None = None) -> None:
    voice.save_edit((settings or load_settings()).voice.dir, voice_id, text)


def voice_post(
    voice_id: str,
    platform: str,
    lang: str | None = None,
    mode: Mode = "new",
    *,
    author: str | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Post:
    settings = settings or load_settings()
    transcript = voice.read_transcript(settings.voice.dir, voice_id)
    conn = repo.connect(settings.db_path)
    return make_voice_post(
        conn, settings, voice_id, transcript, platform, lang, mode, author=author, **kwargs
    )


# --- Статус ---


def status(*, settings: Settings | None = None, now: datetime | None = None) -> Status:
    settings = settings or load_settings()
    now = now or utc_now()
    conn = repo.connect(settings.db_path)
    lock = read_lock(_lock_path(settings))
    collect_row = repo.last_run(conn, "collect")
    digest_row = repo.last_run(conn, "digest", ("ok", "partial"))
    last_build = repo.last_run(conn, "digest", ("ok", "partial", "failed"))
    llm = (repo.json_field(digest_row, "stats_json") or {}).get("llm", {}) if digest_row else {}
    failing = [
        (row["code"], row["last_error"] or "нет свежего опроса")
        for row in repo.source_health(conn)
        if row["last_error"] or not row["last_ok_at"]
    ]
    midnight = tashkent_to_utc(datetime.combine(tashkent_date(now), time()))
    return Status(
        now=now,
        running=(lock.kind, lock.started_at) if lock else None,
        last_collect=_run_info(collect_row),
        last_digest=latest_digest(settings=settings, conn=conn),
        failed_build=_run_info(last_build) if last_build and last_build["status"] == "failed" else None,
        failing_sources=failing,
        claude_token=bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        claude_limit=llm.get("rate_limit"),
        claude_stopped=llm.get("stopped"),
        claude_failed=llm.get("failed", 0),
        claude_used=bool(llm),
        posts_today=repo.count_posts_since(conn, midnight),
        posts_limit=settings.posts.max_per_day,
        next_digest=next_digest_at(settings, now),
        next_collect=next_collect_at(settings, now),
    )


def _run_info(row: sqlite3.Row | None) -> RunInfo | None:
    if row is None:
        return None
    return RunInfo(
        finished_at=from_iso(row["finished_at"]),
        status=row["status"],
        detail=repo.json_field(row, "stats_json") or {},
    )


def _weekday_slots(settings: Settings, now: datetime, times: list[str]) -> datetime:
    """Ближайшее из времён times (ЧЧ:ММ по Ташкенту) в будний день после now."""
    local = now.astimezone(TASHKENT)
    for days in range(8):
        day = local.date() + timedelta(days=days)
        if day.weekday() >= 5:
            continue
        for value in sorted(times):
            moment = datetime.combine(day, time.fromisoformat(value), TASHKENT)
            if moment > local:
                return moment
    raise AssertionError("в расписании нет будних запусков")


def next_digest_at(settings: Settings, now: datetime) -> datetime:
    return _weekday_slots(settings, now, [settings.schedule.digest_at])


def next_collect_at(settings: Settings, now: datetime) -> datetime:
    return _weekday_slots(settings, now, settings.schedule.collect_slots())


# --- Показатели ---


def metrics(days: int = 30, *, settings: Settings | None = None, now: datetime | None = None) -> Metrics:
    settings = settings or load_settings()
    return collect_metrics(repo.connect(settings.db_path), settings, days, now or utc_now())


def describe_metrics(state: Metrics) -> str:
    """Короткий отчёт для кнопки «Показатели» в боте; полный — metrics.render_text."""
    return render_brief(state)


# --- Замечания ---


def add_feedback(
    kind: str,
    text: str,
    *,
    day: date | None = None,
    article_id: str | None = None,
    author: str | None = None,
    source: str = "cli",
    settings: Settings | None = None,
    now: datetime | None = None,
) -> Feedback:
    if kind not in FEEDBACK_KINDS:
        raise ValueError(f"Тип замечания {kind}: есть {', '.join(FEEDBACK_KINDS)}")
    if not text.strip():
        raise ValueError("Пустое замечание")
    settings = settings or load_settings()
    conn = repo.connect(settings.db_path)
    if article_id and day is None:
        day = date.fromisoformat(article_id.rsplit("-", 1)[0])  # 2026-11-03-002 → 2026-11-03
    feedback_id = repo.save_feedback(
        conn,
        {
            "created_at": now or utc_now(),
            "digest_date": day.isoformat() if day else None,
            "article_id": article_id,
            "kind": kind,
            "text": text.strip(),
            "author": author,
            "source": source,
        },
    )
    return next(f for f in list_feedback(settings=settings, conn=conn) if f.id == feedback_id)


def list_feedback(
    day: date | None = None, *, settings: Settings | None = None, conn: sqlite3.Connection | None = None
) -> list[Feedback]:
    conn = conn or repo.connect((settings or load_settings()).db_path)
    return [
        Feedback(**{**dict(row), "created_at": from_iso(row["created_at"])})
        for row in repo.feedback_rows(conn, day.isoformat() if day else None)
    ]


def export_feedback(path: Path, day: date | None = None, *, settings: Settings | None = None) -> Path:
    """CSV для Excel: точка с запятой, UTF-8 с BOM."""
    rows = list_feedback(day, settings=settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["id", "создано", "дайджест", "статья", "тип", "текст", "автор", "откуда"])
        for f in rows:
            created = f.created_at.astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M")
            writer.writerow([f.id, created, f.digest_date or "", f.article_id or "", FEEDBACK_KINDS[f.kind],
                             f.text, f.author or "", f.source])  # fmt: skip
    return path


# --- Тексты для людей ---


def _when(moment: datetime, now: datetime) -> str:
    local, today = moment.astimezone(TASHKENT), now.astimezone(TASHKENT).date()
    if local.date() == today:
        return f"сегодня в {local:%H:%M}"
    if local.date() == today + timedelta(days=1):
        return f"завтра в {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"вчера в {local:%H:%M}"
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m} в {local:%H:%M}"


def articles_text(n: int) -> str:
    """1 статья, 2 статьи, 5 статей."""
    if n % 10 == 1 and n % 100 != 11:
        word = "статья"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "статьи"
    else:
        word = "статей"
    return f"{n} {word}"


def _source_names(codes: list[str]) -> str:
    names = {s.code: s.name for s in load_sources()}
    return ", ".join(names.get(code, code) for code in codes)


def describe_status(state: Status) -> str:
    """«Дайджест за 03.11 готов: 25 статей…» — для uzdigest status и кнопки «Статус» в боте."""
    lines = []
    if state.running:
        what = {"collect": "сбор новостей", "digest": "сборка дайджеста", "run": "сбор и сборка дайджеста"}
        lines.append(
            f"Сейчас идёт {what.get(state.running[0], 'запуск')} (с {_when(state.running[1], state.now)})."
        )
    digest = state.last_digest
    if digest is None:
        lines.append("Дайджестов ещё нет.")
    else:
        today = digest.date == tashkent_date(state.now)
        lines.append(
            f"{'Сегодняшний' if today else 'Последний'} дайджест — за {digest.date:%d.%m.%Y}: "
            f"{articles_text(digest.articles)}, правовых {digest.legal}."
        )
        problems = []
        if digest.no_llm:
            problems.append(f"без обработки Claude — {digest.no_llm}")
        if digest.no_translation:
            problems.append(f"без перевода — {digest.no_translation}")
        if digest.qa_flagged:
            problems.append(f"с пометкой «проверить» — {digest.qa_flagged}")
        if problems:
            lines.append("В нём статьи " + ", ".join(problems) + ".")
    if state.failed_build:
        reason = state.failed_build.detail.get("error", "причина в журнале")
        lines.append(
            f"Последняя сборка не удалась ({_when(state.failed_build.finished_at, state.now)}): {reason}."
        )
    if state.last_collect:
        result = COLLECT_RESULTS.get(state.last_collect.status, "с ошибками части источников")
        lines.append(
            f"Последний сбор новостей — {_when(state.last_collect.finished_at, state.now)}, {result}."
        )
    if state.failing_sources:
        lines.append(f"Не отвечают: {_source_names([code for code, _ in state.failing_sources])}.")
    if not state.claude_token:
        lines.append("Claude не подключён: нет токена подписки (см. docs/operations.md).")
    elif state.claude_stopped:
        lines.append(f"Claude в последней сборке остановился: {state.claude_stopped}.")
    elif state.claude_failed:
        lines.append(f"Claude в последней сборке: неудачных вызовов {state.claude_failed}.")
    elif state.claude_limit and state.claude_limit.get("status") != "allowed":
        lines.append("Лимит подписки Claude близок к исчерпанию.")
    elif state.claude_used:
        lines.append("В последней сборке Claude отвечал без сбоев.")
    lines.append(f"Постов сегодня: {state.posts_today} из {state.posts_limit}.")
    lines.append(
        f"Следующий дайджест — {_when(state.next_digest, state.now)}, "
        f"следующий сбор — {_when(state.next_collect, state.now)}."
    )
    return "\n".join(lines)


def describe_run(result: RunResult, now: datetime | None = None) -> str:
    """Итог запуска одной-двумя фразами — для бота и уведомлений."""
    now = now or result.finished_at
    if result.status == "busy":
        return f"Сбор уже идёт (начат {_when(result.busy_since, now)}), пришлю, как будет готово."
    if result.status == "failed":
        return f"Запуск не удался: {result.error}. Подробности — в logs/uzdigest.log."
    parts = []
    digest = result.digest
    if digest is not None:
        if digest.status == "exists":
            parts.append(f"Дайджест за {digest.date:%d.%m.%Y} уже готов: {articles_text(digest.articles)}.")
        elif not digest.articles:
            parts.append(f"Дайджест за {digest.date:%d.%m.%Y} собран, но статей в нём нет.")
        else:
            parts.append(
                f"Дайджест за {digest.date:%d.%m.%Y} готов: {articles_text(digest.articles)}, "
                f"правовых {digest.legal}."
            )
        if digest.failed_sources:
            parts.append(f"Не ответили: {_source_names([code for code, _ in digest.failed_sources])}.")
        if digest.no_llm or digest.no_translation:
            parts.append(f"Без обработки Claude: {digest.no_llm}, без перевода: {digest.no_translation}.")
        if digest.trimmed:
            parts.append("Простой был слишком долгим: самые старые новости в дайджест не вошли.")
    elif result.collect is not None:
        failed = [s.code for s in result.collect.sources if s.status == "error"]
        new = sum(s.new for s in result.collect.sources)  # догруженные уже внутри new
        parts.append(f"Сбор новостей: {new} новых материалов.")
        if failed:
            parts.append(f"Не ответили: {_source_names(failed)}.")
    return " ".join(parts)


def _lock_path(settings: Settings) -> Path:
    return settings.db_path.parent / "run.lock"


def categories_ru() -> dict[str, str]:
    return load_labels()["ru"].categories
