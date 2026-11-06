"""Сборка дайджеста дня (docs/plan/stage-6-render.md).

Окно → префильтр и кластеры (этап 3) → сверка с lex.uz (этап 4) → проходы Claude (этап 5) →
модель дайджеста → файлы в outputs/ГГГГ-ММ-ДД/ → digests и digest_entries. Дайджест за день
собирается один раз; пересборка (force) берёт то же начало окна и новости, вышедшие после утра,
а кэш Claude не тратит лимит на уже обработанные сюжеты.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError

from uzdigest import __version__
from uzdigest.config import DigestSettings, Settings, Source
from uzdigest.db import repo
from uzdigest.dedup.clustering import Candidate
from uzdigest.dedup.stage import prepare_window
from uzdigest.extract.normalize import TRACKING_PARAMS
from uzdigest.filter.prefilter import Keywords
from uzdigest.legal.doc_key import split_doc_key
from uzdigest.legal.lex_doc import act_link
from uzdigest.legal.verify import verify_window
from uzdigest.llm import summarize, translate, triage
from uzdigest.llm.client import LlmBudget
from uzdigest.llm.schemas import CATEGORIES
from uzdigest.llm.stage import LlmOutcome, cluster_verdict, process_window
from uzdigest.render.markdown import Labels, load_labels, render_files, write_files
from uzdigest.render.model import (
    LANGS,
    Article,
    Digest,
    Doc,
    Failures,
    Generator,
    LexLink,
    Rate,
    SourceFailure,
    SourceRef,
    Text,
    Update,
    Window,
)
from uzdigest.timeutil import from_iso, tashkent_date, utc_now
from uzdigest.web.http import HttpClient

log = logging.getLogger(__name__)

# Источник без успешного опроса дольше этого к концу окна попадает в «Не ответили»:
# сбор по расписанию идёт каждые 2 часа, утренний — прямо перед сборкой
STALE_AFTER_H = 6


@dataclass(frozen=True)
class DigestWindow:
    since: datetime
    until: datetime
    trimmed: bool  # пробел после прошлого дайджеста длиннее max_window_h: начало сдвинуто


@dataclass
class BuildResult:
    date: date
    out_dir: Path
    status: str  # ok | partial | exists (дайджест дня уже собран, ничего не делалось)
    digest: Digest | None = None
    window: DigestWindow | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    budget: LlmBudget | None = None


def digest_window(
    conn: sqlite3.Connection, settings: DigestSettings, now: datetime, rebuild: sqlite3.Row | None = None
) -> DigestWindow:
    """Окно с конца прошлого дайджеста до now, не короче min_window_h и не длиннее max_window_h.

    Пересборка дня (rebuild — строка digests этого дня) начинает окно там же, где утренняя сборка.
    """
    if rebuild is not None and rebuild["window_from"]:
        since = from_iso(rebuild["window_from"])
    else:
        since = repo.last_digest_end(conn) or now
    since = min(since, now - timedelta(hours=settings.min_window_h))
    trimmed = now - since > timedelta(hours=settings.max_window_h)
    if trimmed:
        since = now - timedelta(hours=settings.max_window_h)
    return DigestWindow(since, now, trimmed)


def build_digest(
    conn: sqlite3.Connection,
    settings: Settings,
    http: HttpClient,
    sources: list[Source],
    now: datetime | None = None,
    *,
    force: bool = False,
    use_llm: bool = True,
    budget: LlmBudget | None = None,
    labels: dict[str, Labels] | None = None,
) -> BuildResult:
    now = now or utc_now()
    day = tashkent_date(now)
    out_dir = settings.outputs_dir / day.isoformat()
    existing = repo.digest_by_date(conn, day.isoformat())
    if existing is not None and not force:
        return BuildResult(day, out_dir, "exists")
    labels = labels or load_labels()  # ошибка в i18n.yaml — до похода в сеть и к Claude
    window = digest_window(conn, settings.digest, now, existing)
    if window.trimmed:
        log.warning(
            "Прошлый дайджест больше %s ч назад: окно начинается с %s",
            settings.digest.max_window_h,
            window.since,
        )

    run_id = repo.start_run(conn, "digest")
    started = time.monotonic()
    late_from = window.until - timedelta(hours=settings.digest.max_window_h)
    try:
        repo.sync_sources(conn, sources)
        prepared = prepare_window(
            conn, sources, Keywords.load(), settings.dedup, window.since, window.until, late_from=late_from
        )
        lex = verify_window(conn, http, settings.lex, window.since, window.until, late_from)
        if budget is None:
            llm = settings.llm if use_llm else settings.llm.model_copy(update={"max_calls_per_run": 0})
            budget = LlmBudget(llm)
        outcome = process_window(conn, settings, prepared, budget, window.until)
        digest = assemble(conn, settings, sources, outcome, day, window)
        write_files(out_dir, render_files(digest, labels))
        repo.save_digest(
            conn,
            day.isoformat(),
            str(out_dir),
            run_id,
            digest.to_json(),
            [a.cluster_id for a in digest.articles],
        )
    except BaseException as exc:
        repo.finish_run(conn, run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise

    failures = digest.failures
    relevant_stories = {
        n
        for n, cluster in enumerate(outcome.result.clusters)
        if cluster_verdict(cluster, outcome.verdicts).relevant
    }
    fallbacks = sum(v.origin == "fallback" for v in outcome.verdicts.values())
    # Без Claude часть новостей не отобрана: окно не закрываем, они войдут в следующий дайджест
    complete = not (budget.stopped or failures.no_llm or fallbacks or not digest.articles)
    stats = {
        "window": {
            "from": window.since.isoformat(),
            "to": window.until.isoformat(),
            "trimmed": window.trimmed,
            "late_from": late_from.isoformat(),
        },
        "items": len(prepared.candidates),
        "relevant": sum(v.relevant for v in outcome.verdicts.values()),
        "clusters": len(outcome.result.clusters),
        "stories": {
            "relevant": len(relevant_stories),
            "repeats": len(relevant_stories & set(outcome.repeats)),
        },
        "articles": len(digest.articles),
        "legal": sum(a.doc is not None for a in digest.articles),
        "updates": len(digest.updates),
        "no_llm": failures.no_llm,
        "no_translation": failures.no_translation,
        "qa_flagged": sum(bool(a.flags) for a in digest.articles),
        "sources_failed": [s.code for s in failures.sources],
        "lex": {"requests": lex.requests, "errors": len(lex.errors)},
        "llm": budget.stats(),
        "fallback_verdicts": fallbacks,
        "complete": complete,
        "elapsed_s": round(time.monotonic() - started),
    }
    status = "partial" if failures.no_translation or lex.errors or not complete else "ok"
    repo.finish_run(conn, run_id, status, stats, (window.since, window.until))
    return BuildResult(day, out_dir, status, digest, window, stats, budget)


def rerender(
    conn: sqlite3.Connection, settings: Settings, day: date, labels: dict[str, Labels] | None = None
) -> Path:
    """Файлы дня заново из модели в БД: без сети и без Claude (правка шаблона или подписей)."""
    row = repo.digest_by_date(conn, day.isoformat())
    if row is None or not row["model_json"]:
        raise LookupError(f"Дайджеста за {day.isoformat()} нет в БД")
    out_dir = settings.outputs_dir / day.isoformat()
    write_files(out_dir, render_files(Digest.from_json(row["model_json"]), labels or load_labels()))
    return out_dir


# --- Модель дайджеста ---


def assemble(
    conn: sqlite3.Connection,
    settings: Settings,
    sources: list[Source],
    outcome: LlmOutcome,
    day: date,
    window: DigestWindow,
) -> Digest:
    by_code = {s.code: s for s in sources}
    order = {category: n for n, category in enumerate(CATEGORIES)}
    # Разделы в порядке PLAN.md; внутри — важнее выше, при равной важности — о чём писало больше изданий
    chosen = sorted(
        outcome.articles,
        key=lambda a: (
            order.get(a.category, len(order)),
            -a.importance,
            -len(a.others),
            a.primary.published_at,
        ),
    )
    precision = repo.items_precision(conn, [c.id for a in chosen for c in (a.primary, *a.others)])

    def ref(candidate: Candidate) -> SourceRef:
        return SourceRef(
            code=candidate.source,
            name=site_name(candidate.url, by_code.get(candidate.source)),
            url=link_url(candidate.url),
            published_at=candidate.published_at,
            time_known=precision.get(candidate.id, "time") == "time",
        )

    articles = [
        Article(
            id=f"{day.isoformat()}-{n:03d}",
            cluster_id=a.cluster_id,
            category=a.category,
            importance=a.importance,
            doc=make_doc(conn, a.doc_key, a.doc_status) if a.doc_key else None,
            source=ref(a.primary),
            also=[ref(c) for c in a.others],
            texts={lang: Text(**vars(text)) if text else None for lang, text in a.texts.items()},
            by_llm=a.by_llm,
            flags=a.flags,
        )
        for n, a in enumerate(chosen, 1)
    ]
    rates = (
        [Rate(**dict(row)) for row in repo.fx_rates_on(conn, day.isoformat(), settings.rates.currencies)]
        if settings.rates
        else []
    )
    failed, total = source_failures(conn, by_code, window.until)
    return Digest(
        date=day,
        window=Window(since=window.since, until=window.until),
        generator=Generator(
            version=__version__,
            model=settings.llm.model,
            prompts=[triage.PROMPT, summarize.PROMPT, translate.PROMPT],
        ),
        sources_ok=total - len(failed),
        sources_total=total,
        rates=rates,
        articles=articles,
        updates=find_updates(conn, settings, day, articles),
        failures=Failures(
            sources=failed,
            no_llm=sum(not a.by_llm for a in articles),
            no_translation=sum(any(a.texts.get(lang) is None for lang in LANGS) for a in articles),
        ),
    )


def make_doc(conn: sqlite3.Connection, doc_key: str, status: str | None) -> Doc:
    """Реквизиты акта для строки «Документ»: из справочника, а если акта там нет — из самого ключа."""
    act = repo.legal_act(conn, doc_key)
    doc_type, number, _ = split_doc_key(doc_key)
    lex: dict[str, LexLink] = {}
    if act is not None and status == "verified":
        for lang in LANGS:
            link = act_link(act, lang)
            if link:
                lex[lang] = LexLink(url=link[0], lang=link[1])
    return Doc(
        doc_key=doc_key,
        doc_type=act["doc_type"] if act else doc_type,
        number=act["number"] if act else number,
        adopted_on=act["adopted_on"] if act else None,
        effective_on=act["effective_on"] if act else None,
        status=status,
        lex=lex,
    )


def link_url(url: str) -> str:
    """Адрес материала для дайджеста без трекинговых параметров (utm_* и др.); остальное как в источнике."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def site_name(url: str, source: Source | None) -> str:
    """Подпись ссылки на материал: домен (norma.uz), у ведомств с общего gov.uz — gov.uz/soliq."""
    if source is not None and source.kind == "govuz_api":
        return f"gov.uz/{source.params.get('authority', source.code)}"
    host = urlsplit(url).hostname or url
    return host.removeprefix("www.")


def source_failures(
    conn: sqlite3.Connection, by_code: dict[str, Source], until: datetime
) -> tuple[list[SourceFailure], int]:
    """Включённые источники с ошибкой или без успешного опроса за STALE_AFTER_H; и сколько их всего."""
    rows = repo.source_health(conn)
    stale_before = until - timedelta(hours=STALE_AFTER_H)
    failed = []
    for row in rows:
        last_ok = from_iso(row["last_ok_at"]) if row["last_ok_at"] else None
        if row["last_error"] or last_ok is None or last_ok < stale_before:
            failed.append(
                SourceFailure(
                    code=row["code"],
                    name=site_name(row["url"], by_code.get(row["code"])),
                    error=row["last_error"],
                    last_ok_at=last_ok,
                )
            )
    return failed, len(rows)


def find_updates(
    conn: sqlite3.Connection, settings: Settings, day: date, articles: list[Article]
) -> list[Update]:
    """Акты из прошлых дайджестов со статусом «ожидает публикации», которые теперь есть на lex.uz.

    Каждое обновление выходит один раз: акт, уже объявленный в «Обновлениях», больше не попадает.
    """
    since = (day - timedelta(days=settings.dedup.repeat_days)).isoformat()
    pending: dict[str, tuple[date, Article]] = {}
    announced: set[str] = set()
    for row in repo.digest_models(conn, since, day.isoformat()):
        try:
            model = Digest.from_json(row["model_json"])
        except ValidationError as exc:
            log.warning("Дайджест за %s не прочитан для «Обновлений»: %s", row["date"], exc)
            continue
        for article in model.articles:
            if article.doc and article.doc.status == "pending":
                pending.setdefault(article.doc.doc_key, (model.date, article))
        announced.update(u.doc.doc_key for u in model.updates)
    in_today = {a.doc.doc_key for a in articles if a.doc}
    keys = [key for key in pending if key not in announced and key not in in_today]
    statuses = repo.key_statuses(conn, keys)
    updates = []
    for key in keys:
        if "verified" not in statuses.get(key, set()):
            continue
        previous_date, article = pending[key]
        headlines = {lang: text.headline for lang, text in article.texts.items() if text}
        updates.append(
            Update(doc=make_doc(conn, key, "verified"), previous_date=previous_date, headlines=headlines)
        )
    return updates
