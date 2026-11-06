"""Показатели UzDigest: что складывается из журнала запусков, постов и замечаний.

    uzdigest metrics              за 30 дней понятным текстом
    uzdigest metrics --days 7
    uzdigest metrics --json       те же числа для разбора

Отдельно показатели нигде не хранятся: всё считается из того, что и так пишет конвейер (runs.stats_json,
items, digests.model_json, posts, feedback), и из журналов в logs/. Короткий текст для админа — кнопка
«📊 Показатели» в боте. База при расчёте не меняется, Claude не вызывается.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from uzdigest.config import ScheduleSettings, Settings
from uzdigest.db import repo
from uzdigest.ops import LOCK_STALE_H
from uzdigest.render.model import Digest
from uzdigest.timeutil import TASHKENT, from_iso, tashkent_date, tashkent_to_utc, utc_now

ON_TIME_MINUTES = 30  # дайджест готов позже 09:30 плюс столько минут — опоздание
SILENT_DAYS = 2  # нового дольше стольких дней — источник «молчит», хотя отвечает
MIN_REPEATS = 2  # случилось один раз — ещё не повторяется
MAX_PROBLEMS = 10
MAX_TEXT = 110  # столько знаков примера достаточно, чтобы узнать проблему
SLOT_EARLY = timedelta(minutes=5)
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d{3} (?:WARNING|ERROR|CRITICAL) (\S+): (.*)$")
# Их проблемы считает журнал запусков: из логов они попали бы второй раз
NOT_FROM_LOGS = ("uzdigest.collect", "uzdigest.service")
ARTICLE_FLAGS = {
    "факт не из источника": "число или дата не из источника",
    "в тексте не назван акт": "в тексте не назван акт",
    "реквизиты акта": "реквизиты акта не совпали с темой",
}
POST_FLAGS = {
    "нет в статье": "число или дата не из статьи",
    "длина": "длиннее лимита площадки",
    "ссылка не из статьи": "ссылка не из статьи",
    "нет ссылки": "нет ссылки на источник",
    "в посте для": "ссылка там, где её быть не должно",
    "хэштег": "хэштеги",
    "пост на языке": "не тот язык",
}
MODES_RU = {"shorter": "короче", "longer": "длиннее", "variant": "другой вариант"}
FEEDBACK_RU = {
    "missed": "пропущено",
    "duplicate": "дубль",
    "extra": "лишнее",
    "error": "ошибка",
    "translation": "перевод",
    "post": "пост",
    "other": "прочее",
}


class DayResult(BaseModel):
    date: date
    status: Literal["on_time", "late", "missed"]
    ready_at: datetime | None = None


class Reliability(BaseModel):
    days: list[DayResult] = Field(default_factory=list)  # будние дни по расписанию
    digest_runs: int = 0
    digest_partial: int = 0
    digest_failed: int = 0
    digest_unfinished: int = 0  # процесс оборвался, итога в журнале нет
    median_build_minutes: float | None = None
    collect_runs: int = 0
    collect_partial: int = 0
    collect_failed: int = 0
    collect_unfinished: int = 0
    rates_failed: int = 0
    missed_collects: int = 0  # сборы по расписанию, которых не было: ноутбук спал или был выключен
    longest_gap_hours: float | None = None  # самый долгий перерыв, в котором пропущен сбор

    def count(self, status: str) -> int:
        return sum(day.status == status for day in self.days)


class FunnelDay(BaseModel):
    date: date
    collected: int  # вышло за окно дайджеста
    no_title: int  # страница не скачалась: без заголовка материал в отбор не идёт
    filtered: int  # отсеял префильтр
    candidates: int  # дошли до отбора Claude
    relevant: int  # подошли бизнесу
    clusters: int  # все сюжеты окна
    relevant_stories: int | None  # сюжеты, подошедшие бизнесу; None — запуск старше этого показателя
    repeats: int | None  # из них уже выходили в прошлых дайджестах
    articles: int
    legal: int
    no_llm: int
    no_translation: int
    age_hours: float | None  # медиана от публикации до конца окна


class Funnel(BaseModel):
    days: list[FunnelDay] = Field(default_factory=list)
    unseen: int = 0  # материалы внутри окон, которых не видела ни одна сборка
    unseen_examples: list[str] = Field(default_factory=list)


class SourceHealth(BaseModel):
    code: str
    name: str
    tier: str
    polls: int = 0
    errors: int = 0
    last_error: str | None = None
    new: int = 0
    no_text: int = 0  # без полного текста или без заголовка
    warnings: int = 0
    gaps: int = 0  # опросы, когда лента не достала до прошлого опроса, а догрузки нет
    last_new_at: datetime | None = None
    silent_days: float | None = None  # от последнего нового до последнего опроса


class ClaudeUse(BaseModel):
    digest_calls: int = 0
    digest_failed: int = 0
    post_calls: int = 0
    tokens_in: int = 0
    tokens_cached: int = 0  # из них прочитано из кэша
    tokens_out: int = 0
    seconds: int = 0
    posts_without_usage: int = 0  # посты до учёта токенов: их вызовы посчитаны, токены нет
    stops: list[str] = Field(default_factory=list)  # почему Claude останавливался, новые в конце
    by_purpose: dict[str, dict[str, int]] = Field(default_factory=dict)
    last_limit: dict[str, Any] | None = None


class FactCheck(BaseModel):
    articles: int = 0
    articles_flagged: int = 0
    article_kinds: dict[str, int] = Field(default_factory=dict)
    posts: int = 0
    posts_flagged: int = 0
    post_kinds: dict[str, int] = Field(default_factory=dict)


class PostsUse(BaseModel):
    total: int = 0
    articles: int = 0
    redone: int = 0  # переделки: короче, длиннее, другой вариант
    by_platform: dict[str, int] = Field(default_factory=dict)
    by_lang: dict[str, int] = Field(default_factory=dict)
    by_mode: dict[str, int] = Field(default_factory=dict)
    by_author: dict[str, int] = Field(default_factory=dict)


class TeamFeedback(BaseModel):
    total: int = 0
    by_kind: dict[str, int] = Field(default_factory=dict)
    recent: list[str] = Field(default_factory=list)  # последние замечания, новые первыми


class Problem(BaseModel):
    what: str
    count: int
    last_at: date
    example: str


class Metrics(BaseModel):
    days: int
    since: datetime
    until: datetime
    reliability: Reliability
    funnel: Funnel
    sources: list[SourceHealth]
    claude: ClaudeUse
    facts: FactCheck
    posts: PostsUse
    feedback: TeamFeedback
    problems: list[Problem]


@dataclass(frozen=True)
class _Run:
    kind: str
    status: str | None
    started_at: datetime
    finished_at: datetime | None
    stats: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> _Run:
        return cls(
            row["kind"],
            row["status"],
            from_iso(row["started_at"]),
            from_iso(row["finished_at"]) if row["finished_at"] else None,
            repo.json_field(row, "stats_json") or {},
        )

    @property
    def built(self) -> bool:
        return self.kind == "digest" and self.status in ("ok", "partial")


def collect_metrics(
    conn: sqlite3.Connection, settings: Settings, days: int = 30, now: datetime | None = None
) -> Metrics:
    now = now or utc_now()
    since = now - timedelta(days=days)
    runs = [_Run.from_row(row) for row in repo.runs_since(conn, since)]
    collects = [r for r in runs if r.kind == "collect"]
    digests = repo.digests_since(conn, tashkent_date(since).isoformat())
    posts = repo.posts_since(conn, since)
    feedback = repo.feedback_since(conn, since)
    first_digest = repo.first_digest_date(conn)
    first_day = date.fromisoformat(first_digest) if first_digest else None
    sources = _sources(conn, collects, since)
    facts = _facts(digests, posts)
    return Metrics(
        days=days,
        since=since,
        until=now,
        reliability=_reliability(runs, first_day, settings.schedule, since, now),
        funnel=_funnel(conn, digests),
        sources=sources,
        claude=_claude(runs, posts),
        facts=facts,
        posts=_posts(posts),
        feedback=_feedback(feedback),
        problems=_problems(
            runs, facts, feedback, settings.logs_dir, tashkent_date(since), tashkent_date(now)
        ),
    )


# --- Надёжность ---


def _reliability(
    runs: list[_Run], first_day: date | None, schedule: ScheduleSettings, since: datetime, now: datetime
) -> Reliability:
    digest_runs = [r for r in runs if r.kind == "digest" and r.status != "draft"]
    collects = [r for r in runs if r.kind == "collect"]
    minutes = [
        (r.finished_at - r.started_at).total_seconds() / 60 for r in digest_runs if r.built and r.finished_at
    ]
    missed, gap = _collect_gaps(collects, first_day, schedule, since, now)
    return Reliability(
        days=_mornings(digest_runs, first_day, schedule, since, now),
        digest_runs=len(digest_runs),
        digest_partial=sum(r.status == "partial" for r in digest_runs),
        digest_failed=sum(r.status == "failed" for r in digest_runs),
        digest_unfinished=_unfinished(digest_runs, now),
        median_build_minutes=round(statistics.median(minutes), 1) if minutes else None,
        collect_runs=len(collects),
        collect_partial=sum(r.status == "partial" for r in collects),
        collect_failed=sum(r.status == "failed" for r in collects),
        collect_unfinished=_unfinished(collects, now),
        rates_failed=sum(bool((r.stats.get("rates") or {}).get("error")) for r in collects),
        missed_collects=missed,
        longest_gap_hours=gap,
    )


def _unfinished(runs: list[_Run], now: datetime) -> int:
    return sum(r.finished_at is None and now - r.started_at > timedelta(hours=LOCK_STALE_H) for r in runs)


def _weekdays(first: date, last: date) -> Iterator[date]:
    for n in range((last - first).days + 1):
        day = first + timedelta(days=n)
        if day.weekday() < 5:
            yield day


def _at(day: date, value: str) -> datetime:
    return tashkent_to_utc(datetime.combine(day, time.fromisoformat(value)))


def _mornings(
    digest_runs: list[_Run],
    first_day: date | None,
    schedule: ScheduleSettings,
    since: datetime,
    now: datetime,
) -> list[DayResult]:
    """Будние утра с первого дайджеста: вовремя, с опозданием или вовсе без дайджеста."""
    if first_day is None:
        return []
    ready: dict[date, datetime] = {}
    for run in digest_runs:
        if run.built and run.finished_at:
            day = tashkent_date(run.started_at)
            ready[day] = min(ready.get(day, run.finished_at), run.finished_at)
    results = []
    for day in _weekdays(max(first_day, tashkent_date(since)), tashkent_date(now)):
        deadline = _at(day, schedule.digest_at) + timedelta(minutes=ON_TIME_MINUTES)
        moment = ready.get(day)
        if moment is not None:
            results.append(
                DayResult(date=day, status="on_time" if moment <= deadline else "late", ready_at=moment)
            )
        elif now > deadline:
            results.append(DayResult(date=day, status="missed"))
    return results


def _collect_gaps(
    collects: list[_Run], first_day: date | None, schedule: ScheduleSettings, since: datetime, now: datetime
) -> tuple[int, float | None]:
    """Сборы по расписанию, которых не было. Считаем со дня после первого дайджеста: раньше задач не было."""
    if first_day is None:
        return 0, None
    started = sorted(r.started_at for r in collects)
    step = timedelta(hours=schedule.collect_every_h)
    slots = [
        _at(day, slot)
        for day in _weekdays(max(first_day + timedelta(days=1), tashkent_date(since)), tashkent_date(now))
        for slot in schedule.collect_slots()
    ]
    # Запуск, отложенный до пробуждения ноутбука, тоже считается: новости он забрал
    missed = [
        s for s in slots if s + step <= now and not any(s - SLOT_EARLY <= t < s + step for t in started)
    ]
    gaps = [
        (later - earlier).total_seconds() / 3600
        for earlier, later in zip(started, started[1:], strict=False)
        if any(earlier < s < later for s in missed)
    ]
    return len(missed), round(max(gaps), 1) if gaps else None


# --- Путь материалов ---


def _funnel(conn: sqlite3.Connection, digests: list[sqlite3.Row]) -> Funnel:
    days = []
    windows = []
    for row in digests:
        stats = repo.json_field(row, "stats_json") or {}
        if not row["window_from"] or not row["window_to"]:
            continue
        since, until = from_iso(row["window_from"]), from_iso(row["window_to"])
        windows.append((since, until))
        late_from = (stats.get("window") or {}).get("late_from")  # старые сборки опоздавших не брали
        counts = repo.window_counts(conn, since, until, from_iso(late_from) if late_from else None)
        stories = stats.get("stories") or {}
        digest = _digest(row)
        days.append(
            FunnelDay(
                date=date.fromisoformat(row["date"]),
                collected=counts["collected"],
                no_title=counts["no_title"],
                filtered=counts["filtered"],
                candidates=stats.get("items", 0),
                relevant=stats.get("relevant", 0),
                clusters=stats.get("clusters", 0),
                relevant_stories=stories.get("relevant"),
                repeats=stories.get("repeats"),
                articles=stats.get("articles", 0),
                legal=stats.get("legal", 0),
                no_llm=stats.get("no_llm", 0),
                no_translation=stats.get("no_translation", 0),
                age_hours=_median_age(digest, until),
            )
        )
    funnel = Funnel(days=days)
    if windows:
        unseen = repo.unseen_items(conn, min(w[0] for w in windows), max(w[1] for w in windows))
        funnel.unseen = len(unseen)
        funnel.unseen_examples = [f"{row['source']}: {_cut(row['title'])}" for row in unseen[:3]]
    return funnel


def _digest(row: sqlite3.Row) -> Digest | None:
    try:
        return Digest.from_json(row["model_json"])
    except ValidationError:
        return None  # дайджест старого формата: показатели по файлу не считаем


def _median_age(digest: Digest | None, until: datetime) -> float | None:
    if digest is None or not digest.articles:
        return None
    hours = [(until - a.source.published_at).total_seconds() / 3600 for a in digest.articles]
    return round(statistics.median(hours), 1)


# --- Источники ---


def _sources(conn: sqlite3.Connection, collects: list[_Run], since: datetime) -> list[SourceHealth]:
    blocks: dict[str, SourceHealth] = {}
    last_poll: dict[str, datetime] = {}
    last_new: dict[str, datetime] = {}
    for row in repo.source_overview(conn, since):
        blocks[row["code"]] = SourceHealth(
            code=row["code"], name=row["name"], tier=row["tier"], no_text=row["no_text"]
        )
        if row["last_item_at"]:
            last_new[row["code"]] = from_iso(row["last_item_at"])
    for run in collects:
        for result in run.stats.get("sources", []):
            block = blocks.get(result["code"])
            if block is None:
                continue  # источник выключен или убран из реестра
            block.polls += 1
            block.new += result.get("new", 0)
            block.warnings += result.get("warnings", 0)
            block.gaps += bool(result.get("gap_h"))
            last_poll[block.code] = run.started_at
            if result.get("status") == "error":
                block.errors += 1
                block.last_error = result.get("error")
            elif result.get("new"):
                last_new[block.code] = max(run.started_at, last_new.get(block.code, run.started_at))
    for code, block in blocks.items():
        block.last_new_at = last_new.get(code)
        if block.last_new_at and code in last_poll:
            block.silent_days = round((last_poll[code] - block.last_new_at).total_seconds() / 86400, 1)
    return list(blocks.values())


# --- Claude ---


def _claude(runs: list[_Run], posts: list[sqlite3.Row]) -> ClaudeUse:
    block = ClaudeUse()
    for run in runs:
        llm = run.stats.get("llm")
        if not llm:
            continue
        block.digest_calls += llm.get("calls", 0)
        block.digest_failed += llm.get("failed", 0)
        block.seconds += llm.get("duration_ms", 0) // 1000
        _add_usage(block, llm.get("usage") or {})
        if llm.get("stopped"):
            block.stops.append(f"{tashkent_date(run.started_at):%d.%m}: {llm['stopped']}")
        if llm.get("rate_limit"):
            block.last_limit = llm["rate_limit"]
        for purpose, spent in (llm.get("by_purpose") or {}).items():
            total = block.by_purpose.setdefault(purpose, Counter())
            total.update(spent)
    for post in posts:
        block.post_calls += post["calls"]
        usage = repo.json_field(post, "usage_json")
        if usage is None:
            block.posts_without_usage += 1
            continue
        block.seconds += (post["duration_ms"] or 0) // 1000
        _add_usage(block, usage)
    block.by_purpose = {purpose: dict(spent) for purpose, spent in block.by_purpose.items()}
    return block


def _add_usage(block: ClaudeUse, usage: dict[str, int]) -> None:
    cached = usage.get("cache_read_input_tokens", 0)
    block.tokens_in += usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + cached
    block.tokens_cached += cached
    block.tokens_out += usage.get("output_tokens", 0)


# --- Проверка фактов, посты, замечания ---


def _flag_kind(flag: str, kinds: dict[str, str]) -> str:
    if flag.startswith(("en:", "uz:")):
        return "перевод разошёлся с русским"
    return next((name for start, name in kinds.items() if flag.startswith(start)), "прочее")


def _facts(digests: list[sqlite3.Row], posts: list[sqlite3.Row]) -> FactCheck:
    article_kinds: Counter[str] = Counter()
    post_kinds: Counter[str] = Counter()
    block = FactCheck()
    for row in digests:
        digest = _digest(row)
        for article in digest.articles if digest else []:
            block.articles += 1
            block.articles_flagged += bool(article.flags)
            article_kinds.update(_flag_kind(flag, ARTICLE_FLAGS) for flag in article.flags)
    for post in posts:
        flags = repo.json_field(post, "qa_flags") or []
        block.posts += 1
        block.posts_flagged += bool(flags)
        post_kinds.update(_flag_kind(flag, POST_FLAGS) for flag in flags)
    block.article_kinds, block.post_kinds = dict(article_kinds), dict(post_kinds)
    return block


def _posts(posts: list[sqlite3.Row]) -> PostsUse:
    def count(key: str) -> dict[str, int]:
        return dict(Counter(post[key] or "командная строка" for post in posts).most_common())

    return PostsUse(
        total=len(posts),
        articles=len({post["article_id"] for post in posts}),
        redone=sum(post["mode"] != "new" for post in posts),
        by_platform=count("platform"),
        by_lang=count("lang"),
        by_mode=count("mode"),
        by_author=count("author"),
    )


def _feedback(rows: list[sqlite3.Row]) -> TeamFeedback:
    return TeamFeedback(
        total=len(rows),
        by_kind=dict(Counter(row["kind"] for row in rows).most_common()),
        recent=[_cut(row["text"]) for row in reversed(rows[-3:])],
    )


# --- Что повторяется ---


def _problems(
    runs: list[_Run],
    facts: FactCheck,
    feedback: list[sqlite3.Row],
    logs_dir: Path,
    since_day: date,
    today: date,
) -> list[Problem]:
    found: dict[str, Problem] = {}

    def add(what: str, day: date, example: str, count: int = 1) -> None:
        problem = found.get(what)
        if problem is None:
            found[what] = Problem(what=what, count=count, last_at=day, example=_cut(example))
            return
        problem.count += count
        problem.last_at = max(problem.last_at, day)

    for run in runs:
        day = tashkent_date(run.started_at)
        if run.status == "failed" and run.kind == "digest":
            error = str(run.stats.get("error") or "причина не записана")
            add(f"сборка дайджеста упала: {error.split(':')[0]}", day, error)
        for result in run.stats.get("sources", []):
            if result.get("status") == "error":
                error = str(result.get("error") or "")
                add(f"источник {result['code']}: {error.split(':')[0]}", day, error)
    for what, day, example in _log_problems(logs_dir, since_day):
        add(what, day, example)
    for kind, count in facts.article_kinds.items():
        add(f"проверка фактов в статьях: {kind}", today, kind, count)
    for kind, count in facts.post_kinds.items():
        add(f"проверка постов: {kind}", today, kind, count)
    for row in feedback:
        day = tashkent_date(from_iso(row["created_at"]))
        add(f"замечание коллег: {FEEDBACK_RU.get(row['kind'], row['kind'])}", day, row["text"])
    repeated = [problem for problem in found.values() if problem.count >= MIN_REPEATS]
    return sorted(repeated, key=lambda p: (-p.count, p.what))[:MAX_PROBLEMS]


def _log_problems(logs_dir: Path, since_day: date) -> Iterator[tuple[str, date, str]]:
    """Предупреждения и ошибки журналов конвейера и бота: числа заменены на N, чтобы одинаковое сложилось."""
    for path in sorted(logs_dir.glob("*.log*")):
        if path.name.startswith("scheduler"):
            continue
        with path.open(encoding="utf-8", errors="replace") as file:
            for line in file:
                match = LOG_LINE.match(line)
                if match is None or match[2] in NOT_FROM_LOGS:
                    continue
                day = date.fromisoformat(match[1])
                if day >= since_day:
                    yield _cut(re.sub(r"\d+", "N", match[3])).rstrip(": "), day, match[3]


# --- Текст ---


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else f"{text[: MAX_TEXT - 1].rstrip()}…"


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _decimal(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _join(counts: dict[str, int], names: dict[str, str] | None = None) -> str:
    return ", ".join(f"{(names or {}).get(key, key)} {count}" for key, count in counts.items())


def _local(moment: datetime) -> str:
    return moment.astimezone(TASHKENT).strftime("%d.%m %H:%M")


def _mornings_line(r: Reliability) -> str:
    if not r.days:
        return "Дайджестов ещё не было."
    late = [f"{d.date:%d.%m} в {d.ready_at.astimezone(TASHKENT):%H:%M}" for d in r.days if d.status == "late"]
    line = (
        f"Будних утр: {len(r.days)}. Дайджест вовремя: {r.count('on_time')}, "
        f"с опозданием: {r.count('late')}, пропущено: {r.count('missed')}."
    )
    if late:
        line += f" Опоздания: {', '.join(late)}."
    missed = [f"{d.date:%d.%m}" for d in r.days if d.status == "missed"]
    if missed:
        line += f" Пропущены: {', '.join(missed)}."
    return line


def _collect_line(r: Reliability) -> str:
    line = (
        f"Сборов новостей: {r.collect_runs}, с ошибками части источников: {r.collect_partial}, "
        f"сбоев: {r.collect_failed}."
    )
    if r.missed_collects:
        line += f" Пропущено по расписанию: {r.missed_collects}"
        line += f", самый долгий перерыв {_decimal(r.longest_gap_hours)} ч." if r.longest_gap_hours else "."
    if r.rates_failed:
        line += f" Курсы ЦБ не получены: {r.rates_failed} раз."
    return line


def _funnel_line(day: FunnelDay) -> str:
    stories = f" → сюжетов для бизнеса {day.relevant_stories}" if day.relevant_stories is not None else ""
    repeats = f" (уже выходили {day.repeats})" if day.repeats else ""
    return (
        f"{day.date:%d.%m}: собрано {day.collected} → прошли фильтр {day.candidates} → подошли бизнесу "
        f"{day.relevant}{stories}{repeats} → в дайджесте {day.articles} (правовых {day.legal})"
    )


def _facts_line(f: FactCheck) -> str:
    line = f"Статьи: пометки у {f.articles_flagged} из {f.articles}"
    line += f" ({_join(f.article_kinds)})." if f.article_kinds else "."
    line += f" Посты: пометки у {f.posts_flagged} из {f.posts}"
    return line + (f" ({_join(f.post_kinds)})." if f.post_kinds else ".")


def _claude_line(c: ClaudeUse) -> str:
    return (
        f"Запросов на дайджесты: {c.digest_calls} (неудачных {c.digest_failed}), на посты: {c.post_calls}. "
        f"Токены: на вход {_number(c.tokens_in)} (из них из кэша {_number(c.tokens_cached)}), "
        f"на выход {_number(c.tokens_out)}."
    )


def _posts_line(p: PostsUse) -> str:
    if not p.total:
        return "Постов пока нет."
    line = f"Постов: {p.total} по {p.articles} темам ({_join(p.by_platform)})."
    redone = {mode: count for mode, count in p.by_mode.items() if mode != "new"}
    if redone:
        line += f" Переделки: {p.redone} ({_join(redone, MODES_RU)})."
    return line


def _feedback_line(f: TeamFeedback) -> str:
    if not f.total:
        return "Замечаний нет."
    return f"Замечаний: {f.total} ({_join(f.by_kind, FEEDBACK_RU)})."


def _troubled(m: Metrics) -> tuple[list[SourceHealth], list[SourceHealth]]:
    failing = sorted((s for s in m.sources if s.errors), key=lambda s: -s.errors)
    silent = sorted(
        (s for s in m.sources if not s.errors and (s.silent_days or 0) >= SILENT_DAYS),
        key=lambda s: -(s.silent_days or 0),
    )
    return failing, silent


def render_text(m: Metrics) -> str:
    """Полный отчёт понятными словами: для консоли и для следующего чата с ИИ."""
    r = m.reliability
    lines = [
        f"Показатели UzDigest за {m.days} дней: с {_local(m.since)} по {_local(m.until)}",
        "",
        "НАДЁЖНОСТЬ",
    ]
    lines.append(_mornings_line(r))
    if r.digest_runs:
        build = (
            f" Обычная длительность: {_decimal(r.median_build_minutes)} мин."
            if r.median_build_minutes
            else ""
        )
        lines.append(
            f"Сборок дайджеста: {r.digest_runs}, с оговорками: {r.digest_partial}, "
            f"сбоев: {r.digest_failed}, оборвались: {r.digest_unfinished}.{build}"
        )
    lines.append(_collect_line(r))
    if r.collect_unfinished:
        lines.append(f"Сборы, оборвавшиеся без итога: {r.collect_unfinished}.")

    lines += ["", "ПУТЬ МАТЕРИАЛОВ ОТ СБОРА ДО ДАЙДЖЕСТА"]
    if not m.funnel.days:
        lines.append("Дайджестов ещё не было.")
    for day in m.funnel.days[-7:]:
        extra = []
        if day.filtered:
            extra.append(f"отсеял фильтр {day.filtered}")
        if day.no_title:
            extra.append(f"без заголовка {day.no_title}")
        if day.no_llm:
            extra.append(f"без обработки Claude {day.no_llm}")
        if day.no_translation:
            extra.append(f"без перевода {day.no_translation}")
        if day.age_hours is not None:
            extra.append(f"от публикации до выхода {_decimal(day.age_hours)} ч")
        lines.append(_funnel_line(day) + (f"; {', '.join(extra)}." if extra else "."))
    if m.funnel.unseen:
        lines.append(
            f"Не попали ни в одну сборку: {m.funnel.unseen}. Например, {m.funnel.unseen_examples[0]}"
        )

    lines += ["", "ИСТОЧНИКИ"]
    failing, silent = _troubled(m)
    lines.append(f"Всего включено: {len(m.sources)}, со сбоями: {len(failing)}, молчат: {len(silent)}.")
    for s in failing:
        lines.append(
            f"  сбой, {s.code}: {s.errors} из {s.polls} опросов. Последний: {_cut(s.last_error or '')}"
        )
    for s in silent:
        lines.append(f"  молчит, {s.code}: нового нет {_decimal(s.silent_days)} дн., но отвечает")
    no_text = [f"{s.code} {s.no_text}" for s in m.sources if s.no_text]
    if no_text:
        lines.append(f"Без полного текста: {', '.join(no_text)}.")
    gaps = [f"{s.code} {s.gaps}" for s in m.sources if s.gaps]
    if gaps:
        lines.append(f"Лента короче пробела, часть новостей могла пройти мимо: {', '.join(gaps)}.")

    c = m.claude
    lines += ["", "CLAUDE", _claude_line(c), f"Время ответов: {_number(c.seconds)} с."]
    if c.by_purpose:
        parts = [f"{purpose} {spent.get('calls', 0)}" for purpose, spent in c.by_purpose.items()]
        lines.append(f"По задачам: {', '.join(parts)}.")
    if c.posts_without_usage:
        lines.append(f"Токены не учтены у {c.posts_without_usage} старых постов.")
    if c.stops:
        lines.append(f"Останавливался: {'; '.join(c.stops[-3:])}.")

    lines += ["", "ПРОВЕРКА ФАКТОВ", _facts_line(m.facts)]
    lines += ["", "ПОСТЫ И ЗАМЕЧАНИЯ КОЛЛЕГ", _posts_line(m.posts), _feedback_line(m.feedback)]
    lines += [f"  «{text}»" for text in m.feedback.recent]

    lines += ["", "ЧТО ПОВТОРЯЕТСЯ"]
    if not m.problems:
        lines.append("Пока ничего.")
    for p in m.problems:
        lines.append(f"  {p.count} раз, последний {p.last_at:%d.%m}: {p.what}")
    return "\n".join(lines)


def render_brief(m: Metrics) -> str:
    """Короткий отчёт для админа в боте: одно сообщение Telegram, без технических слов."""
    r = m.reliability
    lines = [f"📊 Показатели за {m.days} дней", "", "Дайджест", _mornings_line(r)]
    if r.digest_failed or r.digest_unfinished:
        lines.append(f"Сборка падала: {r.digest_failed + r.digest_unfinished} раз.")
    lines += ["", "Сбор новостей", _collect_line(r)]
    if m.funnel.days:
        day = m.funnel.days[-1]
        lines += ["", "Путь новостей", _funnel_line(day) + "."]
        if day.age_hours is not None:
            lines.append(f"От публикации до выхода обычно {_decimal(day.age_hours)} ч.")
    failing, silent = _troubled(m)
    lines += ["", "Источники"]
    if failing:
        lines.append(f"Сбои: {', '.join(f'{s.code} {s.errors} из {s.polls}' for s in failing[:6])}.")
    if silent:
        lines.append(f"Молчат, хотя отвечают: {', '.join(s.code for s in silent[:8])}.")
    if not failing and not silent:
        lines.append(f"Все {len(m.sources)} отвечают, новое есть.")
    lines += ["", "Claude", _claude_line(m.claude)]
    if m.claude.stops:
        lines.append(f"Останавливался: {m.claude.stops[-1]}.")
    lines += ["", "Проверка фактов", _facts_line(m.facts)]
    lines += ["", "Посты и замечания", _posts_line(m.posts), _feedback_line(m.feedback)]
    if m.problems:
        lines += ["", "Что повторяется"]
        lines += [f"• {p.what[:90]}: {p.count} раз" for p in m.problems[:5]]
    return "\n".join(lines)
